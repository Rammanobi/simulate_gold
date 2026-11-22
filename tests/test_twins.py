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
CFG = twins.settings(dict(P, twins={"enabled": True}))
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
    w0, _ = twins.target_weights("T1", base, CFG, 0, [], False)
    w1, note1 = twins.target_weights("T1", base, CFG, 1, [], False)
    w2, _ = twins.target_weights("T1", base, CFG, 2, [], False)
    wh, noteh = twins.target_weights("T1", base, CFG, 0, [], True)
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
    assert s["px"][N] == 270.0 and s["nifty"]["status"] == "n/a (bad print)"
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
@pytest.mark.parametrize("seed", range(8))
def test_random_paths_never_break_the_invariants(seed):
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
        s, rows = twins.run_day(s, inp, CFG)
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
