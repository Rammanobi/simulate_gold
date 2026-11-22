"""Shadow paper twins (simgold/twins.py, tools/twins.py): the rules of each twin, cost parity with the engine's fill code, determinism,
persistence, a missing NIFTYBEES price, size caps, the invariants of a random path, the engine hook's gates and the read-only tool.
Decision identity with the engine is tests/test_twins_identical.py."""
from __future__ import annotations

import copy
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import v3_replay as R  # noqa: E402
from simgold import allocation, broker, config, trend, twins  # noqa: E402
from simgold.market import IST  # noqa: E402

G, S, C, N = "GOLDBEES", "SILVERBEES", "HINDCOPPER", "NIFTYBEES"
P = R.shipped_params()
ST, ACFG = P["strategy"], allocation.settings(P)
CFG = twins.settings(dict(P, twins={"enabled": True, "t1_mode": "simple"}))        # the older, fully-invested-on-day-1 shape
CFG3 = twins.settings(dict(P, twins={"enabled": True}))                           # shipped t1_mode: v3 (the merged default pipeline)
PX = {G: 100.0, S: 150.0, C: 400.0}
KEY = {G: "GOLD", S: "SILVER", C: "COPPER"}
ON = {G: (True, 0.16), S: (True, 0.40), C: (False, 0.60)}


def day_n(i: int) -> str:
    return (datetime(2026, 1, 5) + __import__("datetime").timedelta(days=i)).date().isoformat()


def inputs(day="2026-11-13", prices=None, nifty=270.0, infos=None, cash=50_000.0, units=None, z=None, st=None, acfg=None):
    return twins.DayInputs(day=day, prices=dict(prices or PX), nifty=nifty, infos=dict(infos or ON), real_cash=cash,
                           real_units=dict({G: 500} if units is None else units), gsr_z=z, st=st or ST, acfg=acfg or ACFG)


def book_w(state, tid):
    return twins.weights_of(state["books"][tid], state["px"])


# ---------------------------------------------------------------- settings and dormancy
def test_the_shipped_block_is_dormant_and_parses():
    assert P["twins"]["enabled"] is False
    cfg = twins.settings(P)
    assert cfg["enabled"] is False and cfg["cost_model"] == "engine" and cfg["gsr_cap"] == 0.10 and cfg["nifty_ticker"] == "NIFTYBEES.NS"


@pytest.mark.parametrize("val,expected", [(True, True), (False, False), ("true", False), (1, False), (None, False), ("yes", False)])
def test_only_a_literal_true_switches_the_twins_on(val, expected):
    assert twins.settings({"twins": {"enabled": val}})["enabled"] is expected
    assert twins.settings({})["enabled"] is False and twins.settings(None)["enabled"] is False


def test_settings_bounds_and_garbage():
    cfg = twins.settings({"twins": {"gsr_cap": 0.9, "gsr_k": "x", "nifty_share": 7, "silver_sink_share": -1, "cost_model": "FLAT",
                                    "flat_cost_per_side": {"GOLDBEES": "a", "HINDCOPPER": 0.004}, "nifty_ticker": "  "}})
    assert cfg["gsr_cap"] == 0.10 and cfg["gsr_k"] == twins.DEFAULTS["gsr_k"] and cfg["nifty_share"] == 1.0
    assert cfg["silver_sink_share"] == 0.0 and cfg["cost_model"] == "flat" and cfg["nifty_ticker"] == "NIFTYBEES.NS"
    assert cfg["flat_cost_per_side"]["GOLDBEES"] == 0.0015 and cfg["flat_cost_per_side"]["HINDCOPPER"] == 0.004


def test_niftybees_is_price_only_never_in_the_trading_universe():
    assert N not in P["engine"]["tickers"] and N not in P["instruments"] and "NIFTYBEES.NS" not in P["engine"]["tickers"].values()
    assert N not in allocation.SYMS and N not in {a["ticker"].split(".")[0] for a in config.ASSETS.values()}
    assert twins.SYMS == allocation.SYMS


# ---------------------------------------------------------------- cost model parity with the engine's fill code
@pytest.mark.parametrize("sym", [G, S, C])
def test_buy_and_sell_match_Portfolio_execute(sym):
    rng = np.random.default_rng(5)
    for _ in range(60):
        px, cash = float(rng.uniform(50, 600)), float(rng.uniform(100, 120_000))
        qty = int(rng.integers(1, 3000))
        pf = broker.Portfolio()
        pf.cash = cash
        t = pf.execute(KEY[sym], "BUY", qty, px, "2026-11-13", "x")
        b = {"cash": cash, "units": {}, "costs": 0.0, "traded": 0.0}
        got = twins.buy(b, sym, qty, px, CFG)
        assert got == (t["qty"] if t else 0)
        assert b["cash"] == pytest.approx(pf.cash, abs=1e-9) and b["units"].get(sym, 0) == pf.qty(KEY[sym])
        if got:
            sq = int(rng.integers(1, got + 1))
            t2 = pf.execute(KEY[sym], "SELL", sq, px * 1.01, "2026-11-14", "x")
            assert twins.sell(b, sym, sq, px * 1.01, CFG) == t2["qty"]
            assert b["cash"] == pytest.approx(pf.cash, abs=1e-9)
            assert b["costs"] == pytest.approx(pf.total_costs, abs=1e-9)


def test_the_cost_model_is_the_engines_with_equity_stt():
    assert twins.fill_price(100.0, "BUY", CFG) == pytest.approx(100.0 * (1 + config.COSTS.slippage_pct))
    assert twins.fill_price(100.0, "SELL", CFG) == pytest.approx(100.0 * (1 - config.COSTS.slippage_pct))
    assert twins.order_cost(10_000, "SELL", C, CFG) == broker.trade_costs(10_000, "SELL", "equity")
    assert twins.order_cost(10_000, "BUY", N, CFG) == broker.trade_costs(10_000, "BUY", "etf")      # NIFTYBEES is an ETF


def test_flat_cost_model_is_the_backtest_per_side_cost():
    flat = twins.settings({"twins": {"cost_model": "flat"}})
    b = {"cash": 100_000.0, "units": {}, "costs": 0.0, "traded": 0.0}
    n = twins.buy(b, G, 500, 100.0, flat)
    assert n == 500 and b["cash"] == pytest.approx(100_000 - 50_000 * 1.0015) and twins.fill_price(100.0, "BUY", flat) == 100.0
    assert twins.order_cost(1000, "SELL", C, flat) == pytest.approx(3.0)


def test_buy_never_overspends_and_sell_never_oversells():
    b = {"cash": 1_000.0, "units": {G: 3}, "costs": 0.0, "traded": 0.0}
    assert twins.buy(b, G, 10**6, 100.0, CFG) <= 9 and b["cash"] >= 0
    held = b["units"].get(G, 0)
    assert twins.sell(b, G, 10**6, 100.0, CFG) == held and G not in b["units"]
    assert twins.sell({"cash": 0.0, "units": {}, "costs": 0.0, "traded": 0.0}, G, 5, 100.0, CFG) == 0
    assert twins.buy({"cash": 5.0, "units": {}, "costs": 0.0, "traded": 0.0}, G, 10, 100.0, CFG) == 0


# ---------------------------------------------------------------- the rules of each twin
def test_t0_mirrors_the_real_book_and_never_trades():
    s, rows = twins.run_day(None, inputs(cash=12_345.67, units={G: 300, S: 20}), CFG)
    b = s["books"]["T0"]
    assert b["units"] == {G: 300, S: 20} and b["cash"] == pytest.approx(12_345.67)
    assert b["value"] == pytest.approx(12_345.67 + 300 * 100 + 20 * 150) and b["costs"] == 0.0 and b["traded"] == 0.0
    s2, _ = twins.run_day(s, inputs(day="2026-11-16", cash=5.0, units={G: 10}), CFG)
    assert s2["books"]["T0"]["units"] == {G: 10} and s2["books"]["T0"]["cash"] == 5.0          # follows the real book, whatever it did


def test_every_twin_starts_from_the_real_mark_to_market_value():
    s, rows = twins.run_day(None, inputs(cash=40_000.0, units={G: 600}), CFG)
    real = 40_000 + 600 * 100
    assert {r["twin"] for r in rows} == set(twins.TWINS)
    for tid in twins.TWINS:
        assert s["books"][tid]["start_value"] == pytest.approx(real) and s["books"][tid]["real_start_value"] == pytest.approx(real)


def test_t1_is_the_d5fi_default_fully_invested():
    s, _ = twins.run_day(None, inputs(), CFG)
    want, _ = allocation.default_d5fi({k: v for k, v in ON.items()}, ST, ACFG)
    w = book_w(s, "T1")
    for sym in (G, S, C):
        assert w[sym] == pytest.approx(want[sym], abs=0.01)
    assert w["cash"] < 0.01 and sum(want.values()) == pytest.approx(1.0)            # fully invested, gold is the sink


def test_t2_puts_half_of_the_gold_sink_in_niftybees():
    s, _ = twins.run_day(None, inputs(), CFG)
    t1, t2 = book_w(s, "T1"), book_w(s, "T2")
    assert t2[N] == pytest.approx(t1[G] / 2, abs=0.01) and t2[G] == pytest.approx(t1[G] / 2, abs=0.01)
    assert t2[S] == pytest.approx(t1[S], abs=0.01) and t2["cash"] < 0.02


def test_t3_has_no_silver_trend_rule_silver_is_a_share_of_the_sink():
    a, _ = twins.run_day(None, inputs(infos={**ON, S: (True, 0.40)}), CFG)
    b, _ = twins.run_day(None, inputs(infos={**ON, S: (False, 0.40)}), CFG)
    wa, wb = book_w(a, "T3"), book_w(b, "T3")
    assert wa[S] == pytest.approx(wb[S], abs=0.005) and wa[S] == pytest.approx(0.15 * (wa[G] + wa[S]), abs=0.01)    # the trend state is irrelevant
    assert book_w(a, "T1")[S] > 0.2 > wa[S]                                                                     # while T1 follows the trend
    c, _ = twins.run_day(None, inputs(infos={G: ON[G], S: None, C: ON[C]}), CFG)           # no silver data at all: still the static share
    assert book_w(c, "T3")[S] > 0.1


def test_t4_tilt_is_capped_at_ten_points_in_both_directions():
    base, _ = twins.run_day(None, inputs(z=None), CFG)
    up, _ = twins.run_day(None, inputs(z=50.0), CFG)
    dn, _ = twins.run_day(None, inputs(z=-50.0), CFG)
    b, u, d = book_w(base, "T4"), book_w(up, "T4"), book_w(dn, "T4")
    assert book_w(base, "T4")[S] == pytest.approx(book_w(base, "T1")[S], abs=0.01)           # no ratio data: T4 == T1
    assert u[S] - b[S] <= 0.10 + 0.01 and u[S] > b[S] and u[G] < b[G]
    assert b[S] - d[S] <= 0.10 + 0.01 and d[S] < b[S] and d[G] > b[G]
    assert twins.gsr_tilt(1e9, CFG) == 0.10 and twins.gsr_tilt(-1e9, CFG) == -0.10 and twins.gsr_tilt(None, CFG) == 0.0
    assert twins.gsr_tilt(float("nan"), CFG) == 0.0 and twins.gsr_tilt(1.0, CFG) == pytest.approx(0.05)


def test_t4_tilt_respects_the_off_trend_silver_cap():
    off = {**ON, S: (False, 0.40)}
    s, _ = twins.run_day(None, inputs(infos=off, z=50.0), CFG)
    assert book_w(s, "T4")[S] <= ACFG["off_trend_caps"][S] + 0.01


def test_gsr_z_known_values():
    d = ["2026-01-%02d" % i for i in range(1, 31)]
    g = {k: 100.0 for k in d}
    sv = {k: 100.0 for k in d}
    assert twins.gsr_z(g, sv, 504, 10) is None                                  # flat ratio
    sv2 = {k: 100.0 * (1 + 0.01 * i) for i, k in enumerate(d)}                   # silver rising: gold/silver falling -> z negative
    assert twins.gsr_z(g, sv2, 504, 10) < -1.0
    sv3 = dict(sv2)
    sv3[d[-1]] = 50.0                                                           # silver crashes on the last day: ratio high -> z positive
    assert twins.gsr_z(g, sv3, 504, 10) > 1.0
    assert twins.gsr_z(g, sv2, 504, 31) is None                                 # fewer observations than the minimum
    assert twins.gsr_z(g, {**sv2, d[3]: -1.0}, 504, 10) is None                 # a bad close


def test_buy_and_hold_twins_buy_once_and_never_rebalance():
    s = None
    for i, px in enumerate([100.0, 80.0, 130.0, 60.0]):
        s, _ = twins.run_day(s, inputs(day=day_n(i), prices={G: px, S: 150.0, C: 400.0}, nifty=270.0 * px / 100), CFG)
        if i == 0:
            u5, u6 = dict(s["books"]["T5"]["units"]), dict(s["books"]["T6"]["units"])
    assert s["books"]["T5"]["units"] == u5 and s["books"]["T6"]["units"] == u6 and set(u5) == {G} and set(u6) == {G, N}
    assert s["books"]["T5"]["traded"] == pytest.approx(u5[G] * 100.0 * (1 + config.COSTS.slippage_pct))
    assert u6[N] * 270.0 == pytest.approx(u6[G] * 100.0, rel=0.02)


def test_rebalance_band_trades_only_beyond_three_points():
    s, _ = twins.run_day(None, inputs(), CFG)
    units = dict(s["books"]["T1"]["units"])
    s2, _ = twins.run_day(s, inputs(day="2026-11-16", prices={G: 100.5, S: 150.0, C: 400.0}), CFG)       # tiny drift: no sells, units only grow by the sweep
    for sym in (S, C):
        assert s2["books"]["T1"]["units"].get(sym, 0) == units.get(sym, 0)
    assert s2["books"]["T1"]["units"][G] >= units[G]


def test_brake_levels_and_the_account_stop_shape_the_target():
    base = inputs()
    w0, _, _ = twins.target_weights("T1", base, CFG, 0, [], False)
    w1, note1, _ = twins.target_weights("T1", base, CFG, 1, [], False)
    w2, _, _ = twins.target_weights("T1", base, CFG, 2, [], False)
    wh, noteh, _ = twins.target_weights("T1", base, CFG, 0, [], True)
    assert w1[S] == pytest.approx(w0[S] * 0.5) and w1[G] == pytest.approx(w0[G] + w0[S] * 0.5 + w0[C] * 0.5)      # rotates to the gold sink
    assert sum(w1.values()) == pytest.approx(1.0) and "brake L1" in note1
    assert sum(w2.values()) == 0.0 and sum(wh.values()) == 0.0 and "latched" in noteh


def test_account_stop_latches_and_the_twin_goes_flat_for_good():
    s, _ = twins.run_day(None, inputs(cash=0.0, units={G: 1000}), CFG)                      # real value 100,000
    crash = {G: 40.0, S: 100.0, C: 400.0}                                                   # the gold-heavy twins fall under 75,000
    s, _ = twins.run_day(s, inputs(day="2026-11-16", prices=crash, cash=0.0, units={G: 1000}), CFG)
    b = s["books"]["T1"]
    assert b["latched"] is True and not b["units"] and b["cash"] < 75_000
    s, rows = twins.run_day(s, inputs(day="2026-11-17", prices={G: 200.0, S: 150.0, C: 400.0}), CFG)     # a rally does not un-latch it
    assert s["books"]["T1"]["units"] == {} and s["books"]["T1"]["latched"] is True
    assert twins.weights_of(s["books"]["T1"], s["px"])["cash"] == pytest.approx(1.0)
    assert s["books"]["T5"]["units"]                                                         # buy and hold is not stopped


def test_no_trend_data_holds_instead_of_trading():
    s, _ = twins.run_day(None, inputs(), CFG)
    units = dict(s["books"]["T1"]["units"])
    s2, rows = twins.run_day(s, inputs(day="2026-11-16", infos={G: None, S: None, C: None}), CFG)
    assert s2["books"]["T1"]["units"] == units
    assert "no trend data" in next(r for r in rows if r["twin"] == "T1")["note"]


# ---------------------------------------------------------------- NIFTYBEES missing
def test_missing_niftybees_never_fails_and_nifty_twins_wait_then_start():
    s, rows = twins.run_day(None, inputs(nifty=None), CFG)
    assert "T2" not in s["books"] and "T6" not in s["books"] and {r["twin"] for r in rows} == {"T0", "T1", "T3", "T4", "T5"}
    assert s["nifty"]["status"] == "pending" and s["nifty"]["missing_days"] == 1
    s, rows = twins.run_day(s, inputs(day="2026-11-16", nifty=270.0, prices={G: 110.0, S: 150.0, C: 400.0}), CFG)
    assert {"T2", "T6"} <= set(s["books"]) and s["books"]["T2"]["start_day"] == "2026-11-16"
    assert s["books"]["T2"]["start_value"] == pytest.approx(s["books"]["T0"]["value"])        # starts from that day's real value
    assert s["books"]["T6"]["units"].get(N, 0) > 0 and s["nifty"]["status"] == "ok"


def test_a_missing_price_later_carries_the_last_price_and_does_not_trade_t2():
    s, _ = twins.run_day(None, inputs(), CFG)
    u2, u6 = dict(s["books"]["T2"]["units"]), dict(s["books"]["T6"]["units"])
    s, rows = twins.run_day(s, inputs(day="2026-11-16", nifty=None, prices={G: 90.0, S: 120.0, C: 300.0}), CFG)
    assert s["books"]["T2"]["units"] == u2 and s["books"]["T6"]["units"] == u6                # no trades without a NIFTYBEES price
    notes = {r["twin"]: r["note"] for r in rows}
    assert "n/a" in notes["T2"] and "n/a" in notes["T6"] and s["nifty"]["status"] == "n/a" and s["px"][N] == 270.0
    assert s["books"]["T2"]["value"] > 0 and all(r["value"] > 0 for r in rows)
    assert next(r for r in rows if r["twin"] == "T1")["note"] == ""                           # the other twins are unaffected


def test_a_bad_niftybees_print_is_treated_as_missing():
    s, _ = twins.run_day(None, inputs(), CFG)
    s, rows = twins.run_day(s, inputs(day="2026-11-16", nifty=27_000.0), CFG)               # a 100x print
    assert s["px"][N] == 270.0 and s["nifty"]["status"].startswith("n/a (bad print")
    s, _ = twins.run_day(s, inputs(day="2026-11-17", nifty=275.0), CFG)
    assert s["px"][N] == 275.0 and s["nifty"]["status"] == "ok"
    for bad in (float("nan"), -1.0, 0.0, "x", True):
        s2, _ = twins.run_day(s, inputs(day="2026-11-18", nifty=bad), CFG)
        assert s2["px"][N] == 275.0


def test_a_missing_real_price_is_an_error_for_the_caller_to_catch():
    with pytest.raises(ValueError):
        twins.run_day(None, inputs(prices={G: 100.0, S: float("nan"), C: 400.0}), CFG)


# ---------------------------------------------------------------- determinism, idempotence, persistence
def path_days(n=40, seed=3):
    rng = np.random.default_rng(seed)
    px = {G: 100.0, S: 150.0, C: 400.0}
    out = []
    for i in range(n):
        px = {k: v * float(np.exp(rng.normal(0.0005, 0.012 * (1 + j)))) for j, (k, v) in enumerate(px.items())}
        on = {k: bool(rng.random() < 0.6) for k in px}
        infos = {k: (on[k], float(rng.uniform(0.1, 0.6))) for k in px}
        out.append(inputs(day=day_n(i), prices=px, nifty=(None if i % 7 == 3 else 270.0 * (1 + 0.001 * i)), infos=infos,
                          cash=30_000.0, units={G: 400, S: 100}, z=float(rng.normal(0, 1.5))))
    return out


def run_all(days, roundtrip=None):
    s, allrows = None, []
    for inp in days:
        s, rows = twins.run_day(s, inp, CFG)
        allrows += rows
        if roundtrip:
            twins.save_state(roundtrip, s)
            s = twins.load_state(roundtrip)
    return s, allrows


def test_runs_are_deterministic_and_do_not_mutate_their_input():
    days = path_days()
    a, ra = run_all(days)
    b, rb = run_all(days)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True) and ra == rb
    snap, inp = copy.deepcopy(a), days[0]
    twins.run_day(a, twins.DayInputs(**{**inp.__dict__, "day": "2027-12-31"}), CFG)
    assert a == snap


def test_restart_in_the_middle_gives_the_identical_ledger(tmp_path):
    days = path_days()
    straight, rows_a = run_all(days)
    restarted, rows_b = run_all(days, roundtrip=tmp_path / "twins.json")           # saved and re-read after EVERY day
    assert json.dumps(straight, sort_keys=True) == json.dumps(restarted, sort_keys=True) and rows_a == rows_b


def test_the_same_day_twice_changes_nothing():
    s, rows = twins.run_day(None, inputs(), CFG)
    again, rows2 = twins.run_day(s, inputs(prices={G: 50.0, S: 50.0, C: 50.0}), CFG)
    assert rows2 == [] and again == s
    older, rows3 = twins.run_day(s, inputs(day="2026-11-01"), CFG)
    assert rows3 == [] and older == s


def test_state_round_trip_is_atomic_and_compact(tmp_path):
    s, _ = run_all(path_days(10))
    path = tmp_path / "state" / "twins.json"
    assert twins.save_state(path, s) is True and twins.load_state(path) == s
    assert not list(path.parent.glob(".*.tmp")) and path.read_text().count("\n") == 1 and path.stat().st_size < twins.MAX_STATE_BYTES


# ---------------------------------------------------------------- caps, bad files, csv
def test_state_size_is_capped(tmp_path):
    s, _ = run_all(path_days(5))
    path = tmp_path / "twins.json"
    twins.save_state(path, s)
    before = path.read_bytes()
    big = dict(s, notes=["x" * 100] * 600)
    assert twins.save_state(path, big) is True and json.loads(path.read_text())["notes"] == []       # notes dropped first
    huge = dict(s)
    huge["junk"] = "y" * (twins.MAX_STATE_BYTES + 10)
    twins.save_state(path, s)
    assert twins.save_state(path, huge) is False and path.read_bytes() == before        # the refused write left the previous good file


def test_the_real_state_stays_far_under_its_cap_after_a_year(tmp_path):
    s, _ = run_all(path_days(260, seed=9))
    path = tmp_path / "twins.json"
    assert twins.save_state(path, s) and path.stat().st_size < twins.MAX_STATE_BYTES / 3


def test_corrupt_state_is_moved_aside_and_a_newer_schema_is_left_alone(tmp_path):
    path = tmp_path / "twins.json"
    path.write_text("{not json")
    assert twins.load_state(path) == {} and (tmp_path / "twins.json.bad").exists() and not path.exists()
    path.write_text(json.dumps({"schema": 1, "books": {"T9": {}}, "px": {}, "nifty": {}}))
    assert twins.load_state(path) == {}
    newer = json.dumps({"schema": 99, "books": {}})
    path.write_text(newer)
    with pytest.raises(twins.TwinsError):
        twins.load_state(path)
    assert path.read_text() == newer
    assert twins.load_state(tmp_path / "missing.json") == {}


def test_csv_rows_are_appended_deduplicated_per_month_and_capped(tmp_path, monkeypatch):
    s, rows = twins.run_day(None, inputs(), CFG)
    assert twins.append_rows(tmp_path, rows) == 7 and twins.append_rows(tmp_path, rows) == 0
    f = tmp_path / "twins-2026-11.csv"
    lines = f.read_text().splitlines()
    assert lines[0] == ",".join(twins.CSV_COLUMNS) and len(lines) == 8
    s, rows2 = twins.run_day(s, inputs(day="2026-12-01"), CFG)
    assert twins.append_rows(tmp_path, rows2) == 7 and (tmp_path / "twins-2026-12.csv").exists()
    monkeypatch.setattr(twins, "MAX_CSV_BYTES", 10)
    s, rows3 = twins.run_day(s, inputs(day="2026-12-02"), CFG)
    assert twins.append_rows(tmp_path, rows3) == 0
    read = twins.read_rows(tmp_path)
    assert len(read) == 14 and read[0]["twin"] == "T0" and isinstance(read[0]["value"], float)
    assert {r["date"] for r in twins.read_rows(tmp_path, last_days=1)} == {"2026-12-01"}


# ---------------------------------------------------------------- invariants (property test)
@pytest.mark.parametrize("seed,cfg", [(i, CFG) for i in range(5)] + [(i, CFG3) for i in range(5)], ids=lambda x: x if isinstance(x, int) else x["t1_mode"])
def test_random_paths_never_break_the_invariants(seed, cfg):
    rng = np.random.default_rng(100 + seed)
    px = {G: 100.0, S: 150.0, C: 400.0}
    s, nif = None, 270.0
    for i in range(160):
        px = {k: max(1.0, v * float(np.exp(rng.normal(0.0, 0.03 * (1 + j))))) for j, (k, v) in enumerate(px.items())}   # fat, violent moves
        nif = nif * float(np.exp(rng.normal(0, 0.02)))
        infos = {k: ((bool(rng.random() < 0.5), float(rng.uniform(0.05, 1.5))) if rng.random() < 0.95 else None) for k in px}
        units = {k: int(rng.integers(0, 300)) for k in px if rng.random() < 0.7}
        inp = inputs(day=day_n(i), prices=px, nifty=(None if rng.random() < 0.2 else nif), infos=infos,
                     cash=float(rng.uniform(0, 60_000)), units=units, z=float(rng.normal(0, 8)))
        s, rows = twins.run_day(s, inp, cfg)
        assert len(rows) in (5, 7)
        for tid, b in s["books"].items():
            w = twins.weights_of(b, s["px"])
            assert b["cash"] >= 0 and b["value"] >= 0 and all(isinstance(u, int) and u > 0 for u in b["units"].values())
            assert sum(w.values()) == pytest.approx(1.0, abs=1e-6) and all(0.0 <= x <= 1.0 + 1e-9 for x in w.values())
            assert sum(w[k] for k in twins.BOOK_SYMS) <= 1.0 + 1e-9                     # no leverage
            assert set(b["units"]) <= set(twins.BOOK_SYMS)
            if tid in ("T1", "T3", "T4", "T0", "T5"):
                assert w[N] == 0.0                                                      # NIFTYBEES only ever in T2 and T6
            assert b["peak"] >= b["value"] - 1e-6 and -1.0 <= b["max_dd"] <= 0.0 and 0 <= b["days_green"] <= b["days"]
            if tid in twins.RULE_TWINS + ("T5", "T6"):
                assert b["costs"] >= 0 and b["traded"] >= 0
        t4 = s["books"]["T4"]
        assert twins.weights_of(t4, s["px"])[S] <= 0.60                                  # D3 cap 0.50 plus whole-unit slack


# ---------------------------------------------------------------- the engine entry point
def hook_args(tmp_path, now, **kw):
    d = dict(params=dict(P, twins={"enabled": True}), state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", now=now, prices=dict(PX),
             fresh=lambda s: True, hist={}, pf=SimpleNamespace(cash=60_000.0, liquidbees=0.0, positions={G: SimpleNamespace(qty=400)}),
             checks_complete=True, nifty_feed=lambda t: 271.0)
    d.update(kw)
    return d


def bars(n=300, seed=1, drift=0.0004):
    rng = np.random.default_rng(seed)
    import pandas as pd
    idx = [d.date().isoformat() for d in pd.bdate_range(end="2026-11-12", periods=n)]
    c = (100 * np.cumprod(1 + rng.normal(drift, 0.008, n))).tolist()
    return trend.clean_bars(c, [x * 1.003 for x in c], [x * 0.997 for x in c], idx)


def test_hook_gates(tmp_path):
    t = lambda h, m: datetime(2026, 11, 13, h, m, tzinfo=IST)           # noqa: E731
    hist = {G: bars(300, 1), S: bars(300, 2), C: bars(300, 3)}
    off = hook_args(tmp_path, t(15, 12), hist=hist, params=dict(P, twins={"enabled": False}))
    assert twins.daily_hook(**off) is None and not (tmp_path / "state").exists()
    assert twins.daily_hook(**hook_args(tmp_path, t(15, 5), hist=hist)) is None                                   # before the check
    assert twins.daily_hook(**hook_args(tmp_path, t(15, 12), hist=hist, checks_complete=False)) is None           # engine checks not finished
    assert twins.daily_hook(**hook_args(tmp_path, t(15, 12), hist=hist, fresh=lambda s: s != S)) is None          # a stale price
    assert twins.daily_hook(**hook_args(tmp_path, t(15, 12), hist=hist, prices={G: 100.0, S: 150.0})) is None
    assert not (tmp_path / "state").exists()
    rows = twins.daily_hook(**hook_args(tmp_path, t(15, 26), hist=hist, checks_complete=False))                   # past check_until: goes anyway
    assert rows and len(rows) == 7
    assert twins.daily_hook(**hook_args(tmp_path, t(15, 27), hist=hist)) is None                                   # once a day
    assert (tmp_path / "state" / "twins.json").exists() and (tmp_path / "logs" / "twins-2026-11.csv").exists()
    st = json.loads((tmp_path / "state" / "twins.json").read_text())
    assert st["last_day"] == "2026-11-13" and st["days"] == 1 and st["schema"] == 1


def test_hook_survives_a_broken_feed_and_missing_history(tmp_path):
    def boom(t):
        raise RuntimeError("yahoo down")
    rows = twins.daily_hook(**hook_args(tmp_path, datetime(2026, 11, 13, 15, 12, tzinfo=IST), nifty_feed=boom))
    assert {r["twin"] for r in rows} == {"T0", "T1", "T3", "T4", "T5"}
    st = json.loads((tmp_path / "state" / "twins.json").read_text())
    assert st["nifty"]["status"] == "pending" and "T2" not in st["books"]
    # no history at all: nothing to compute trends from, so the rule twins hold (and say so); T0 and the buy-and-hold twins still record
    assert all("no trend data" in r["note"] for r in rows if r["twin"] in ("T1", "T3", "T4"))


def test_hook_uses_the_gold_silver_ratio_from_the_daily_history(tmp_path):
    hist = {G: bars(600, 1), S: bars(600, 2), C: bars(600, 3)}
    rows = twins.daily_hook(**hook_args(tmp_path, datetime(2026, 11, 13, 15, 12, tzinfo=IST), hist=hist))
    t4 = next(r for r in rows if r["twin"] == "T4")
    assert "z=" in t4["note"] and "z=n/a" not in t4["note"]
    hist2 = {G: bars(100, 1), S: bars(100, 2), C: bars(100, 3)}                          # too short for min_obs: no tilt, no crash
    rows2 = twins.daily_hook(**hook_args(tmp_path / "b", datetime(2026, 11, 13, 15, 12, tzinfo=IST), hist=hist2))
    assert rows2 is not None


# ---------------------------------------------------------------- the read-only tool
def populate(tmp_path, n=12):
    s, allrows = run_all(path_days(n))
    twins.save_state(tmp_path / "state" / "twins.json", s)
    twins.append_rows(tmp_path / "logs", allrows)
    return s


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


def test_tool_show_and_summary_are_read_only_and_print_a_compact_table(tmp_path, capsys):
    import twins as tool                                                                  # tools/twins.py
    populate(tmp_path)
    before = tree(tmp_path)
    assert tool.main(["--root", str(tmp_path), "show"]) == 0
    out = capsys.readouterr().out
    assert "T0" in out and "T6" in out and "maxDD%" in out and "green%" in out and "vs real pp" in out and "Shadow twins" in out
    assert tool.main(["--root", str(tmp_path), "summary", "--days", "5"]) == 0
    out2 = capsys.readouterr().out
    assert "latest 5 dated snapshots" in out2 and len([x for x in out2.splitlines() if x.startswith("T")]) >= 5
    assert tool.main(["--root", str(tmp_path), "show", "--days", "3"]) == 0
    assert tree(tmp_path) == before                                                       # nothing written, nothing touched


def test_tool_with_no_data_says_so(tmp_path, capsys):
    import twins as tool
    assert tool.main(["--root", str(tmp_path), "show"]) == 0 and "No twin data yet" in capsys.readouterr().out
    assert tool.main(["--root", str(tmp_path), "summary"]) == 0 and "No twin snapshots yet" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_summary_numbers():
    rows = []
    for i, (v0, v1) in enumerate([(100.0, 100.0), (110.0, 99.0), (99.0, 120.0)]):
        for tid, v in (("T0", v0), ("T1", v1)):
            rows.append({"date": day_n(i), "twin": tid, "value": v, "green": 1 if v >= 100 else 0, "w_GOLDBEES": 1.0, "w_SILVERBEES": 0.0,
                         "w_HINDCOPPER": 0.0, "w_NIFTYBEES": 0.0, "w_cash": 0.0, "note": ""})
    sm = twins.summarize(rows)
    assert sm["T1"]["ret"] == pytest.approx(0.20) and sm["T0"]["ret"] == pytest.approx(-0.01)
    assert sm["T1"]["max_dd"] == pytest.approx(99 / 100 - 1) and sm["T1"]["green"] == pytest.approx(2 / 3)
    assert sm["T1"]["vs_real"] == pytest.approx(0.21) and sm["T0"]["vs_real"] is None
    w = twins.summarize(rows, days=1)                                              # base = the value before the window
    assert w["T1"]["ret"] == pytest.approx(120 / 99 - 1)


# ================================================================= review fixes (PR #78 review)
import threading  # noqa: E402
import time  # noqa: E402


def _cov(vol=0.5):
    names = (G, S, C)
    return {a: {b: (vol ** 2 if a == b else 0.5 * vol ** 2) for b in names} for a in names}


# ---- (a) T1-T4 follow the merged v3 default pipeline: ramp-in, turnover limits, dust, vol guard
def test_v3_mode_ramps_in_like_the_real_book_and_simple_mode_does_not():
    simple, _ = twins.run_day(None, inputs(), CFG)
    v3, _ = twins.run_day(None, inputs(), CFG3)
    assert book_w(simple, "T1")["cash"] < 0.01 and book_w(v3, "T1")["cash"] > 0.7                  # day 1: 20% turnover limit, not 100% invested
    assert book_w(v3, "T5")["cash"] < 0.01                                                          # buy and hold is fully invested at once
    b = v3["books"]["T1"]
    assert 0.19 <= b["ledger"]["2026-11-13"] <= 0.21 and b["dust"]["2026-11-13"] <= ACFG["dust_day"] + 1e-6


def test_v3_mode_respects_the_day_and_week_turnover_limits_and_ends_fully_invested():
    s, led, invested = None, {}, []
    for i, d in enumerate(("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26",
                           "2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02")):
        s, _ = twins.run_day(s, inputs(day=d), CFG3)
        b = s["books"]["T1"]
        led = b["ledger"]
        invested.append(1 - book_w(s, "T1")["cash"])
        assert led[d] <= ACFG["max_turnover_day"] + ACFG["dust_day"] + 1e-6
        days = sorted(led)[-5:]
        assert sum(led[k] for k in days) <= ACFG["max_turnover_week"] + ACFG["dust_week"] + 1e-4
    assert invested[0] < 0.25 and invested[-1] > 0.95 and invested == sorted(invested)               # about two weeks to fully invested
    assert 0 < len(s["books"]["T1"]["ledger"]) <= 10


def test_v3_mode_runs_the_portfolio_volatility_guard_and_marks_the_note():
    hot = inputs(infos={G: (True, 0.16), S: (True, 0.30), C: (True, 0.30)})
    w, note, res = twins.target_weights("T1", twins.DayInputs(**{**hot.__dict__, "cov": _cov(0.6)}), CFG3, 0, [], False,
                                        {"cash": 100_000.0, "units": {G: 300, S: 100, C: 50}, "ledger": {}}, PX)
    assert res is not None and res.vol_before is not None and res.vol_before > ACFG["max_port_vol"] - 1e-9
    assert sum(w.values()) <= 1.0 + 1e-9 and w[N] == 0.0
    w2, note2, res2 = twins.target_weights("T1", hot, CFG, 0, [], False)                              # simple mode: no pipeline at all
    assert res2 is None


def test_v3_mode_applies_to_the_whole_family_and_t2_still_splits_the_sink():
    s = None
    for i, d in enumerate(("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24")):
        s, _ = twins.run_day(s, inputs(day=d, z=3.0), CFG3)
    for tid in ("T1", "T2", "T3", "T4"):
        assert 0.7 < 1 - book_w(s, tid)["cash"] <= 1.0 + 1e-9, tid
    assert book_w(s, "T2")[N] > 0.15 and book_w(s, "T1")[N] == 0.0


def test_the_account_stop_latches_in_v3_mode_too():
    s = None
    for d in ("2026-11-16", "2026-11-17", "2026-11-18"):
        s, _ = twins.run_day(s, inputs(day=d, cash=0.0, units={G: 1000}), CFG3)
    s, _ = twins.run_day(s, inputs(day="2026-11-19", prices={G: 30.0, S: 40.0, C: 100.0}, cash=0.0, units={G: 1000}), CFG3)
    assert s["books"]["T1"]["latched"] is True or s["books"]["T1"]["value"] > 75_000


# ---- (b) T4 k is a config key
def test_gsr_k_is_a_config_key_and_the_cap_still_binds():
    k10 = twins.settings({"twins": {"gsr_k": 0.10}})
    assert twins.gsr_tilt(0.5, k10) == pytest.approx(0.05) and twins.gsr_tilt(3.0, k10) == 0.10 and twins.gsr_tilt(1.0, CFG) == pytest.approx(0.05)
    assert P["twins"]["gsr_k"] == 0.05 and P["twins"]["gsr_cap"] == 0.10


# ---- (c) T0 measured from the same start as each twin
def test_a_late_twin_is_compared_with_t0_from_its_own_start_day():
    flat = twins.settings({"twins": {"cost_model": "flat", "flat_cost_per_side": {"GOLDBEES": 0, "SILVERBEES": 0, "HINDCOPPER": 0, "NIFTYBEES": 0},
                                     "nifty_share": 0.5, "t1_mode": "simple"}})
    s, rows = None, []
    gold = [100.0, 110.0, 121.0, 133.1, 146.41]
    for i, g in enumerate(gold):
        nifty = None if i == 0 else 2.7 * g                                  # NIFTYBEES tracks gold exactly; absent on day 1 so T6 starts on day 2
        s, r = twins.run_day(s, inputs(day=day_n(i), prices={G: g, S: 150.0, C: 400.0}, nifty=nifty, cash=0.0, units={G: int(100_000 / 100)}), flat)
        rows += r
    assert s["books"]["T6"]["start_day"] == day_n(1)
    sm = twins.summarize(rows)
    assert sm["T6"]["vs_real"] == pytest.approx(0.0, abs=2e-3) and sm["T5"]["vs_real"] == pytest.approx(0.0, abs=2e-3)
    t0 = [r["value"] for r in rows if r["twin"] == "T0"]
    wrong = sm["T6"]["ret"] - (t0[-1] / t0[0] - 1.0)                        # T0 measured from DAY 1 instead: a clearly non-zero error
    assert abs(wrong) > 0.05


# ---- (d) green% in a window is measured from the window start
def test_green_share_in_a_window_is_relative_to_the_window_start():
    rows = []
    for i, v in enumerate([100.0, 90.0, 95.0, 97.0, 99.0]):                  # always under its own start of 100, but above the window-start value 90
        rows.append({"date": day_n(i), "twin": "T1", "value": v, "green": 0, "w_GOLDBEES": 1.0, "w_SILVERBEES": 0.0, "w_HINDCOPPER": 0.0,
                     "w_NIFTYBEES": 0.0, "w_cash": 0.0, "note": ""})
    sm = twins.summarize(rows, days=3)                                       # window = the last 3 rows; base = the close before it (90); the whole history is measured from 100
    assert sm["T1"]["green"] == pytest.approx(1.0) and twins.summarize(rows)["T1"]["green"] == pytest.approx(1 / 5)


# ---- (e) the Rs 75,000 stop as a share of each twin's start
def test_show_reports_the_account_stop_as_a_percent_of_each_twins_start(tmp_path, capsys):
    import twins as tool
    s, rows = twins.run_day(None, inputs(cash=0.0, units={G: 1000}), CFG)
    s, rows2 = twins.run_day(s, inputs(day="2026-11-16", nifty=272.0, cash=0.0, units={G: 1000}), CFG)
    twins.save_state(tmp_path / "state" / "twins.json", s)
    assert s["account_stop"] == 75_000.0
    assert tool.main(["--root", str(tmp_path), "show"]) == 0
    out = capsys.readouterr().out
    assert "stop%" in out and "-25" in out                                    # 75,000 against a 100,000 start


# ---- (f) NIFTYBEES split / bad print
def test_a_real_split_seen_on_two_days_is_applied_to_the_holdings_without_a_phantom_loss(caplog):
    s, _ = twins.run_day(None, inputs(nifty=270.0), CFG)
    units0 = s["books"]["T6"]["units"][N]
    v0 = s["books"]["T6"]["value"]
    with caplog.at_level("WARNING"):
        s, _ = twins.run_day(s, inputs(day="2026-11-16", nifty=27.2), CFG)         # first sight: a bad print, holdings keep the last price
        assert s["px"][N] == 270.0 and s["books"]["T6"]["units"][N] == units0 and "bad print" in s["nifty"]["status"]
        assert any("bad print" in r.message for r in caplog.records)
        s, _ = twins.run_day(s, inputs(day="2026-11-17", nifty=27.1), CFG)         # the same new level again, ratio 10: a split
    assert s["nifty"]["status"] == "split x10" and s["books"]["T6"]["units"][N] == units0 * 10 and s["px"][N] == 27.1
    assert s["books"]["T6"]["value"] == pytest.approx(v0, rel=0.02)
    assert "suspect" not in s["nifty"]


def test_a_one_day_spike_is_never_taken_for_a_split():
    s, _ = twins.run_day(None, inputs(nifty=270.0), CFG)
    u0 = s["books"]["T6"]["units"][N]
    s, _ = twins.run_day(s, inputs(day="2026-11-16", nifty=27.0), CFG)
    s, _ = twins.run_day(s, inputs(day="2026-11-17", nifty=271.0), CFG)            # back to normal: the spike is forgotten
    assert s["nifty"]["status"] == "ok" and "suspect" not in s["nifty"] and s["books"]["T6"]["units"][N] == u0
    s, _ = twins.run_day(s, inputs(day="2026-11-18", nifty=27.0), CFG)
    s, _ = twins.run_day(s, inputs(day="2026-11-19", nifty=13.0), CFG)             # a different level, not a clean ratio: still a bad print
    assert s["books"]["T6"]["units"][N] == u0 and s["px"][N] == 271.0


def test_an_owner_set_split_override_is_applied_once():
    cfg = twins.settings({"twins": {"t1_mode": "simple", "split_override": {"date": "2026-11-17", "factor": 10}}})
    assert cfg["split_override"] == {"date": "2026-11-17", "factor": 10}
    assert twins.settings({"twins": {"split_override": {"date": "x", "factor": 10}}})["split_override"] is None
    assert twins.settings({"twins": {"split_override": {"date": "2026-11-17", "factor": 1}}})["split_override"] is None
    s, _ = twins.run_day(None, inputs(nifty=270.0), cfg)
    u0 = s["books"]["T2"]["units"][N]
    s, _ = twins.run_day(s, inputs(day="2026-11-16", nifty=270.0), cfg)
    v = s["books"]["T6"]["value"]
    s, _ = twins.run_day(s, inputs(day="2026-11-17", nifty=27.0), cfg)
    assert s["nifty"]["override_done"] == "2026-11-17" and s["books"]["T6"]["units"][N] > 0 and s["books"]["T6"]["value"] == pytest.approx(v, rel=0.03)
    u = s["books"]["T6"]["units"][N]
    s, _ = twins.run_day(s, inputs(day="2026-11-18", nifty=27.1), cfg)
    assert s["books"]["T6"]["units"][N] == u                                    # not applied a second time


# ---- (h) the quarantined file is never pushed
def test_the_quarantine_file_is_git_ignored():
    import subprocess
    repo = Path(__file__).resolve().parent.parent
    r = subprocess.run(["git", "check-ignore", "-q", "state/twins.json.bad"], cwd=repo)
    assert r.returncode == 0
    r2 = subprocess.run(["git", "check-ignore", "-q", "state/twins.json"], cwd=repo)
    assert r2.returncode == 1                                                    # the real state IS synced


# ---- (B2) size check before reading, read-only filesystems, a failed save never re-asks the feed
def test_a_40mb_state_file_is_set_aside_without_being_read(tmp_path, monkeypatch):
    path = tmp_path / "twins.json"
    with open(path, "wb") as fh:
        fh.truncate(40 * 1024 * 1024)
    real = Path.read_text

    def guard(self, *a, **k):
        assert self != path, "the oversized file must not be read"
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", guard)
    d, why = twins.inspect_state(path)
    assert d == {} and "cap" in why and path.exists()                              # inspect: nothing moved
    assert twins.load_state(path, quarantine=False) == {} and path.exists()
    assert twins.load_state(path) == {} and not path.exists() and (tmp_path / "twins.json.bad").stat().st_size == 40 * 1024 * 1024


def _hook_day(tmp_path, feed, hour=15, minute=12, **kw):
    return twins.daily_hook(**hook_args(tmp_path, datetime(2026, 11, 13, hour, minute, tzinfo=IST), nifty_feed=feed, **kw))


def test_a_failing_save_retries_a_few_times_but_asks_the_feed_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(twins, "save_state", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")))
    for _ in range(8):
        try:
            _hook_day(tmp_path, lambda t: calls.append(t) or 271.0)
        except OSError:
            pass
    assert len(calls) == 1
    assert len(twins.read_rows(tmp_path / "logs")) == 7                             # the CSV day was written first and is not duplicated by retries


def test_os_replace_failing_is_the_same_read_only_case(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(twins.os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only")))
    n_exc = 0
    for _ in range(6):
        try:
            _hook_day(tmp_path, lambda t: calls.append(t) or 271.0)
        except PermissionError:
            n_exc += 1
    assert len(calls) == 1 and n_exc == twins.MAX_TRIES and not (tmp_path / "state" / "twins.json").exists()


def test_a_crash_after_the_csv_cannot_drop_the_day(tmp_path, monkeypatch):
    real = twins.save_state
    monkeypatch.setattr(twins, "save_state", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        _hook_day(tmp_path, lambda t: 271.0)
    assert len(twins.read_rows(tmp_path / "logs")) == 7 and not (tmp_path / "state" / "twins.json").exists()
    monkeypatch.setattr(twins, "save_state", real)
    rows = _hook_day(tmp_path, lambda t: 271.0, minute=13)                            # the next minute: state saved, CSV not doubled
    assert rows and len(twins.read_rows(tmp_path / "logs")) == 7
    assert json.loads((tmp_path / "state" / "twins.json").read_text())["last_day"] == "2026-11-13"


# ---- (B1) a hanging feed
def test_a_hanging_feed_is_abandoned_after_the_timeout(tmp_path):
    release = threading.Event()
    p = dict(P, twins={"enabled": True, "nifty_timeout_s": 0.3})
    t0 = time.time()
    try:
        rows = twins.daily_hook(**hook_args(tmp_path, datetime(2026, 11, 13, 15, 12, tzinfo=IST), params=p,
                                            nifty_feed=lambda t: release.wait(30) or 271.0))
        took = time.time() - t0
    finally:
        release.set()
    assert took < 5 and rows and {r["twin"] for r in rows} == {"T0", "T1", "T3", "T4", "T5"}       # no NIFTYBEES: the nifty twins wait
    assert json.loads((tmp_path / "state" / "twins.json").read_text())["nifty"]["status"] == "pending"


def test_call_with_timeout_returns_raises_and_times_out():
    assert twins.call_with_timeout(lambda x: x * 2, 21, 1.0) == 42
    with pytest.raises(ValueError):
        twins.call_with_timeout(lambda x: (_ for _ in ()).throw(ValueError("x")), 1, 1.0)
    ev = threading.Event()
    with pytest.raises(TimeoutError):
        twins.call_with_timeout(lambda x: ev.wait(10), 1, 0.1)
    ev.set()


def _live(tmp_path, monkeypatch, now, bars):
    import run_live
    for name in ("STATE", "LOGS"):
        monkeypatch.setattr(run_live, name, tmp_path / name.lower())
    monkeypatch.setattr(run_live, "ROOT", tmp_path)
    monkeypatch.setattr(run_live, "PF_PATH", tmp_path / "state" / "portfolio.json")
    monkeypatch.setattr(run_live, "PF_BACKUP", tmp_path / "state" / "portfolio.backup.json")
    monkeypatch.setattr(run_live, "NEWS_PATH", tmp_path / "state" / "news.json")
    monkeypatch.setattr(run_live, "intraday_bars", lambda tickers: bars)
    monkeypatch.setattr(run_live, "now_ist", lambda: now)
    live = run_live.Live(force=False, tick_seconds=60)
    monkeypatch.setattr(live.news, "refresh", lambda **k: 0)
    return run_live, live


def test_live_tick_saves_the_portfolio_before_the_twins_run_and_a_hanging_lookup_cannot_hold_it(tmp_path, monkeypatch):
    import pandas as pd
    from simgold.engine import Market
    now = datetime(2026, 11, 13, 15, 26, tzinfo=IST)
    idx = pd.DatetimeIndex([now])
    bar = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000}, index=idx)
    rl, live = _live(tmp_path, monkeypatch, now, {t: bar for t in live_tickers()})
    monkeypatch.setattr(rl, "daily_history", lambda *a, **k: {"close": pd.DataFrame(), "high": pd.DataFrame(), "low": pd.DataFrame()})
    monkeypatch.setattr(rl, "build_market", lambda ts, bars, daily, **k: Market(now=ts, prices=dict(PX), price_ts={s: ts for s in PX}, scores={}))
    monkeypatch.setattr(rl, "run_tick", lambda pf, mkt, env: [])
    monkeypatch.setitem(live.p["twins"], "enabled", True)
    monkeypatch.setitem(live.p["twins"], "nifty_timeout_s", 0.3)
    monkeypatch.setitem(live.p["strategy"], "mode", "v2_trend")
    release = threading.Event()
    monkeypatch.setattr(rl, "live_prices", lambda tickers: release.wait(30) or ({}, {}))
    seen = {}
    real_after = rl.twins.after_tick

    def spy(pf, mkt, params, state_dir, logs_dir, nifty_feed=None):
        seen["saved_first"] = (tmp_path / "state" / "portfolio.json").exists() and (tmp_path / "state" / "portfolio.backup.json").exists()
        return real_after(pf, mkt, params, state_dir, logs_dir, nifty_feed=nifty_feed)
    monkeypatch.setattr(rl.twins, "after_tick", spy)
    t0 = time.time()
    try:
        live.tick()
    finally:
        release.set()
    assert time.time() - t0 < 10 and seen["saved_first"] is True
    assert (tmp_path / "state" / "twins.json").exists()


def live_tickers():
    return list(P["engine"]["tickers"].values())


def test_the_engine_module_has_no_twins_code():
    src = (Path(__file__).resolve().parent.parent / "simgold" / "engine.py").read_text(encoding="utf-8")
    assert "twins" not in src                                                      # the hook lives in run_live.Live.tick, after the save
    live_src = (Path(__file__).resolve().parent.parent / "run_live.py").read_text(encoding="utf-8")
    assert live_src.index("self._save_pf()", live_src.index("def tick")) < live_src.index("self._twins_after(mkt)")


# ---- (S) schema compatibility with the report reader of PR #76
def test_the_state_carries_the_summary_keys_for_the_reports(tmp_path):
    s, _ = run_all(path_days(12))
    assert s["since"] == s["start_day"] and s["asof"] == s["last_day"] and set(s["twins"]) == set(s["books"])
    t = s["twins"]["T1"]
    assert set(t) == {"value", "return_pct", "max_dd_pct", "days", "label"} and t["label"] == twins.LABELS["T1"]
    b = s["books"]["T1"]
    assert t["return_pct"] == pytest.approx((b["value"] / b["start_value"] - 1) * 100, abs=1e-3) and t["max_dd_pct"] <= 0
    cs = s["common_start"]
    assert cs["date"] == max(b["start_day"] for b in s["books"].values()) and set(cs["return_pct"]) == set(s["books"])
    assert len(twins.TWINS) == 7


def test_common_start_resets_when_a_late_twin_starts_and_is_measured_from_one_close():
    s, _ = twins.run_day(None, inputs(nifty=None), CFG)
    assert s["common_start"]["date"] == "2026-11-13" and "T2" not in s["twins"]
    s, _ = twins.run_day(s, inputs(day="2026-11-16", prices={G: 110.0, S: 150.0, C: 400.0}, nifty=270.0), CFG)
    cs = s["common_start"]
    assert cs["date"] == "2026-11-16" and set(cs["return_pct"]) == set(twins.TWINS) and all(abs(v) < 1e-9 for v in cs["return_pct"].values())
    s, _ = twins.run_day(s, inputs(day="2026-11-17", prices={G: 121.0, S: 150.0, C: 400.0}, nifty=270.0), CFG)
    assert s["common_start"]["return_pct"]["T5"] == pytest.approx(10.0, abs=0.2)


def _load_reader_from_pr76():
    import subprocess
    import types
    try:
        src = subprocess.run(["git", "show", "origin/feat/pricegap-and-v3-reports:simgold/reportblocks.py"], cwd=Path(__file__).resolve().parent.parent,
                             capture_output=True, text=True, timeout=30, check=True).stdout
    except Exception:           # noqa: BLE001 - the branch is not fetched here
        return None
    mod = types.ModuleType("reportblocks_pr76")
    exec(compile(src, "reportblocks.py", "exec"), mod.__dict__)         # noqa: S102 - our own repository's code, read-only reader
    return mod


def test_a_real_twins_json_loads_through_the_pr76_reader(tmp_path):
    mod = _load_reader_from_pr76()
    if mod is None:
        pytest.skip("origin/feat/pricegap-and-v3-reports is not available")
    s, _ = run_all(path_days(15))
    twins.save_state(tmp_path / "state" / "twins.json", s)
    v = mod.twins_view(tmp_path)
    assert v is not None and v["since"] == s["since"] and v["asof"] == s["asof"]
    assert len(v["rows"]) == min(len(twins.TWINS), mod.MAX_TWINS)                  # the reader must allow 7 rows to show T6
    ids = sorted(s["twins"])                                                       # the reader sorts T0..T6 and names them its own way
    for tid, row in zip(ids, v["rows"]):
        assert row["value"] == pytest.approx(s["twins"][tid]["value"]) and row["ret_pct"] == pytest.approx(s["twins"][tid]["return_pct"], abs=1e-3)
        assert row["dd_pct"] == pytest.approx(s["twins"][tid]["max_dd_pct"], abs=1e-3)


# ---- (T) the tool is read-only, even on a bad file
@pytest.mark.parametrize("content", ["{not json", json.dumps({"schema": 1, "books": {"T9": {}}, "px": {}, "nifty": {}}), json.dumps({"schema": 99}), "x" * 40_000])
def test_tool_show_leaves_an_invalid_file_exactly_where_and_as_it_is(tmp_path, capsys, content):
    import twins as tool
    p = tmp_path / "state" / "twins.json"
    p.parent.mkdir(parents=True)
    p.write_text(content)
    before = tree(tmp_path)
    assert tool.main(["--root", str(tmp_path), "show"]) == 0
    out = capsys.readouterr().out
    assert ("left untouched" in out or "left alone" in out) and tree(tmp_path) == before and not (p.parent / "twins.json.bad").exists()
    assert tool.main(["--root", str(tmp_path), "summary"]) == 0 and tree(tmp_path) == before


# ---- (F) fairness tables
def test_paired_stats_known_values_and_a_deterministic_bootstrap():
    rets = {"A": {day_n(i): 0.001 * (1 if i % 2 else 3) for i in range(40)}, "B": {day_n(i): 0.001 for i in range(40)}}
    ps = twins.paired_stats(rets, "A", "B")
    assert ps["n"] == 40 and ps["mean"] == pytest.approx(0.001, abs=1e-9)                                      # alternates 0.002 / 0.000
    d = [rets["A"][k] - rets["B"][k] for k in sorted(rets["A"])]
    m = sum(d) / 40
    sd = (sum((x - m) ** 2 for x in d) / 39) ** 0.5
    assert ps["mean"] == pytest.approx(m) and ps["std"] == pytest.approx(sd) and ps["t"] == pytest.approx(m / (sd / 40 ** 0.5))
    assert ps["ci"] == twins.paired_stats(rets, "A", "B")["ci"] and ps["ci"][0] < m < ps["ci"][1]
    assert ps["mde_year_pp"] == pytest.approx(2 * sd / 40 ** 0.5 * 252 * 100)
    assert twins.paired_stats(rets, "A", "A") is None and twins.paired_stats(rets, "A", "Z") is None
    assert twins.paired_stats({"A": {"d1": 0.1}, "B": {"d1": 0.0}}, "A", "B")["mean"] is None                    # one day: no statistics


def test_daily_returns_skip_each_twins_first_day():
    rows = [{"date": day_n(i), "twin": "T1", "value": v} for i, v in enumerate([100.0, 99.0, 101.0])]
    r = twins.daily_returns(rows)["T1"]
    assert set(r) == {day_n(1), day_n(2)} and r[day_n(1)] == pytest.approx(-0.01)


def test_cost_table_gross_net_turnover_and_day_one_entry_loss():
    s, _ = run_all(path_days(6))
    ct = twins.cost_table(s)
    b = s["books"]["T1"]
    assert ct["T1"]["net"] == pytest.approx(b["value"] / b["start_value"] - 1) and ct["T1"]["gross"] > ct["T1"]["net"]
    assert ct["T1"]["fees"] > 0 and ct["T1"]["traded_x"] > 0 and ct["T1"]["entry_loss"] > 0 and ct["T0"]["fees"] == 0 and ct["T0"]["entry_loss"] == pytest.approx(0, abs=1e-6)
    assert ct["T5"]["entry_loss"] > 0                                                                          # buy and hold paid to get in too


def test_summary_prints_the_fairness_tables_and_changes_nothing(tmp_path, capsys):
    import twins as tool
    populate(tmp_path, n=30)
    before = tree(tmp_path)
    assert tool.main(["--root", str(tmp_path), "summary", "--days", "20"]) == 0
    out = capsys.readouterr().out
    for needle in ("Common start", "Gross vs net", "day-1 entry loss", "Daily paired difference vs T1", "Daily paired difference vs T5", "Power:",
                   "cannot rank twins", "green%(window start)", "stop%"):
        assert needle in out, needle
    assert tree(tmp_path) == before
    assert tool.main(["--root", str(tmp_path), "show"]) == 0 and "green%(own start)" in capsys.readouterr().out
