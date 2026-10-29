"""Stop-loss package D5-S (owner approval 2026-10-29; strategy.stops in config/params.yaml, docs/RULEBOOK.md V2.3/V2.4):
the silver/copper trend exit enforced every minute with 2 fresh prices, the protected gold floor, the floor top-up that pauses
cannot skip, and the backtest option. Real minute ticks through simgold.engine.run_tick."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import test_engine_v2 as T
import test_engine_v2_fixes as F
from conftest import d5_params, v2_params
from simgold import backtest, trend
from simgold.broker import Portfolio, Position
from simgold.engine import Market, run_tick
from simgold.market import IST

SYMS = T.SYMS
UP = F.UP
I = 301                                    # the test day (index into T.DAYS)


def tick(pf, env, prices, hh, mm, hist=None, ages=None):
    day = T.DAYS[I].to_pydatetime()
    now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    ages = ages or {}
    return run_tick(pf, Market(now=now, prices=prices, price_ts={s: now - timedelta(minutes=ages.get(s, 0)) for s in prices}, scores={},
                               prev_close={s: p for s, p in prices.items()}, hist=hist or {}), env)


def bars(kind: str, start: float, n: int = 300):
    """Synthetic completed daily bars: 'up' rises 40%, 'down' falls from +15% to -2% (trend OFF everywhere)."""
    rng = np.random.default_rng(int(start))
    a, b = (1.0, 1.4) if kind == "up" else (1.15, 0.98)
    c = start * np.linspace(a, b, n) * (1 + rng.normal(0, 0.002, n))
    return trend.clean_bars(c.tolist(), (c * 1.003).tolist(), (c * 0.997).tolist(), [d.date().isoformat() for d in T.DAYS[:n]])


def d5_env(tmp_path, kinds: dict, cash=100000.0, positions=None):
    """A D5 (50% gold floor) portfolio on day I with idle cash, plus the bars per symbol (kinds: {sym: 'up'|'down'})."""
    p = d5_params()
    env = T.make_env(tmp_path, p)
    pf = Portfolio()
    pf.cash, pf.liquidbees = cash, 0.0
    for sym, qty, px in (positions or []):
        pf.positions[sym] = Position(qty=qty, avg_cost=px, opened="2026-10-21", last_price=px, high_water=px)
    pf.runtime["strategy_mode"] = "v2_trend"
    pf.runtime["v2"] = {"last_full": T.DAYS[I - 1].date().isoformat()}
    hist = {s: bars(kinds[s], T.START[s]) for s in SYMS}
    return pf, env, hist


def last(hist, sym):
    return hist[sym].close[-1]


def reasons(env, action, sym):
    return [d for d in F.decs(env, I) if d["action"] == action and d["symbol"] == sym]


# ---------------------------------------------------------------- 1, 2, 8: silver / copper trend exit every minute
def silver_held(tmp_path, params=None):
    pf, env = F.fresh(tmp_path, params=params)
    run_tick(pf, F.mk(UP, 300, 15, 10), env)
    assert "SILVERBEES" in pf.positions
    run_tick(pf, F.mk(UP, I, 9, 15), env)                              # first tick of the day freezes the level
    lvl = pf.runtime["v2"]["xlvl"]["SILVERBEES"]
    assert 0 < lvl < float(UP["SILVERBEES"][I])
    return pf, env, lvl


def px_with(sym, value):
    p = {s: float(UP[s][I]) for s in SYMS}
    p[sym] = value
    return p


def test_1_two_fresh_prices_under_the_level_sell_everything_and_no_rebuy_that_day(tmp_path):
    pf, env, lvl = silver_held(tmp_path)
    qty = pf.positions["SILVERBEES"].qty
    run_tick(pf, F.mk(UP, I, 9, 30, prices=px_with("SILVERBEES", lvl - 0.1)), env)
    run_tick(pf, F.mk(UP, I, 9, 31, prices=px_with("SILVERBEES", lvl + 1.0)), env)      # back above: the count resets
    assert pf.positions["SILVERBEES"].qty == qty and "SILVERBEES" not in pf.runtime["v2"]["below"]
    run_tick(pf, F.mk(UP, I, 9, 32, prices=px_with("SILVERBEES", lvl - 0.1)), env)
    assert pf.positions["SILVERBEES"].qty == qty
    run_tick(pf, F.mk(UP, I, 9, 33, prices=px_with("SILVERBEES", lvl - 0.2)), env)
    assert "SILVERBEES" not in pf.positions
    s = reasons(env, "SELL", "SILVERBEES")
    assert len(s) == 1 and s[0]["trigger"] == "Trend exit" and s[0]["time"] == "09:33" and s[0]["qty"] == qty
    r = s[0]["reason"]
    fill = (lvl - 0.2) * (1 - env.params["valuation"]["paper_slippage"]["SILVERBEES"])
    assert f"₹{lvl - 0.1:,.2f} then ₹{lvl - 0.2:,.2f}" in r and f"level ₹{lvl:,.2f}" in r        # the level and the two prices
    assert f"{qty} × ₹{fill:,.2f} = ₹{qty * fill:,.2f}" in r and "max(peak close" in r         # fill units x price = amount
    run_tick(pf, F.mk(UP, I, 15, 10), env)                              # the daily check, price back to normal: not bought back today
    assert "SILVERBEES" not in pf.positions
    tg = [d for d in F.decs(env, I) if d["action"] == "TARGET" and d["symbol"] == "SILVERBEES"]
    assert tg and "stopped out intraday" in tg[0]["reason"] and not reasons(env, "BUY", "SILVERBEES")


def test_2_a_gap_open_under_the_level_sells_at_the_second_tick(tmp_path):
    pf, env, lvl = silver_held(tmp_path)
    pf.runtime["v2"]["xlvl"] = {"date": "1999-01-01"}                   # as if the next day's first tick is the gap open
    run_tick(pf, F.mk(UP, I, 9, 15, prices=px_with("SILVERBEES", lvl * 0.96)), env)
    assert "SILVERBEES" in pf.positions
    run_tick(pf, F.mk(UP, I, 9, 16, prices=px_with("SILVERBEES", lvl * 0.97)), env)
    assert "SILVERBEES" not in pf.positions
    assert reasons(env, "SELL", "SILVERBEES")[0]["time"] == "09:16"


def test_8_one_bad_tick_then_normal_ticks_sell_nothing(tmp_path):
    pf, env, lvl = silver_held(tmp_path)
    run_tick(pf, F.mk(UP, I, 9, 30, prices=px_with("SILVERBEES", lvl - 1.0)), env)
    for m in range(31, 40):
        run_tick(pf, F.mk(UP, I, 9, m), env)
    assert "SILVERBEES" in pf.positions and not reasons(env, "SELL", "SILVERBEES")


def test_a_stale_price_neither_counts_nor_sells(tmp_path):
    pf, env, lvl = silver_held(tmp_path)
    for m in (30, 31, 32):
        run_tick(pf, F.mk(UP, I, 9, m, prices=px_with("SILVERBEES", lvl - 1.0), ages={"SILVERBEES": 10}), env)
    assert "SILVERBEES" in pf.positions


def test_a_stopped_symbol_is_not_re_entered_without_a_new_55_day_high(tmp_path):
    pf, env, lvl = silver_held(tmp_path)
    for m in (30, 31):
        run_tick(pf, F.mk(UP, I, 9, m, prices=px_with("SILVERBEES", lvl - 1.0)), env)
    assert "SILVERBEES" not in pf.positions and pf.runtime["v2"]["xstop"]["SILVERBEES"] == T.DAYS[I].date().isoformat()
    # next day: the price is back above the level and the ordinary trend state is still ON, but there is no new 55-day high
    day = T.DAYS[I + 1].to_pydatetime()
    now = datetime(day.year, day.month, day.day, 15, 10, tzinfo=IST)
    prices = {s: float(UP[s][I + 1]) for s in SYMS}
    hist = {s: trend.clean_bars(UP[s][:I + 1].tolist(), (UP[s][:I + 1] * 1.004).tolist(), (UP[s][:I + 1] * 0.996).tolist()) for s in SYMS}
    prices["SILVERBEES"] = float(UP["SILVERBEES"][I]) * 0.99                         # above the exit level, under the prior 55-day high
    run_tick(pf, Market(now=now, prices=prices, price_ts={s: now for s in prices}, scores={}, prev_close=prices, hist=hist), env)
    assert "SILVERBEES" not in pf.positions
    assert "stopped out intraday" in [d for d in T.read_decisions(env, I + 1) if d["action"] == "TARGET" and d["symbol"] == "SILVERBEES"][0]["reason"]


def test_intraday_exit_empty_and_confirm_one_keep_the_older_behaviour(tmp_path):
    p = v2_params()
    p["strategy"]["stops"] = {"intraday_exit": [], "confirm_ticks": 1, "protect_floor": False}
    pf, env = F.fresh(tmp_path, params=p)
    run_tick(pf, F.mk(UP, 300, 15, 10), env)
    run_tick(pf, F.mk(UP, I, 9, 15), env)
    assert "xlvl" not in pf.runtime["v2"]
    low = px_with("SILVERBEES", float(UP["SILVERBEES"][I]) * 0.8)
    run_tick(pf, F.mk(UP, I, 9, 30, prices=low), env)
    assert "SILVERBEES" in pf.positions                                 # trend exits are the daily check's job again


# ---------------------------------------------------------------- 3, 4, 7: the gold floor is not stoppable by the AI
def test_3_regression_23_oct_tighten_stop_on_a_floor_holding_is_refused_and_the_gap_open_sells_nothing(tmp_path):
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS}, cash=49979.0, positions=[("GOLDBEES", 423, 118.19)])
    px = {"SILVERBEES": last(hist, "SILVERBEES"), "HINDCOPPER": last(hist, "HINDCOPPER"), "GOLDBEES": 117.50}
    F.fastlane_file(env, I, {"GOLDBEES": {"tighten_stop": 116.40}}, 11, 0)
    tick(pf, env, px, 11, 0, hist)
    assert pf.positions["GOLDBEES"].ai_stop == 0.0 and pf.positions["GOLDBEES"].qty == 423
    assert any("floor" in d["reason"] and "refused" in d["reason"] for d in reasons(env, "HOLD", "GOLDBEES"))
    for m in (1, 2, 3):
        tick(pf, env, dict(px, GOLDBEES=115.60), 11, m, hist)                 # the gap open under 116.40
    assert pf.positions["GOLDBEES"].qty == 423 and not reasons(env, "SELL", "GOLDBEES")


def test_a_sell_fraction_on_an_all_floor_holding_is_refused(tmp_path):
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS}, cash=49979.0, positions=[("GOLDBEES", 423, 118.19)])
    px = {s: last(hist, s) for s in SYMS}
    px["GOLDBEES"] = 117.50
    F.fastlane_file(env, I, {"GOLDBEES": {"sell_fraction": 1.0}}, 11, 0)
    tick(pf, env, px, 11, 0, hist)
    assert pf.positions["GOLDBEES"].qty == 423 and not reasons(env, "SELL", "GOLDBEES")


def test_4_an_ai_stop_on_trend_on_gold_sells_only_the_units_above_the_floor(tmp_path):
    kinds = {"GOLDBEES": "up", "SILVERBEES": "down", "HINDCOPPER": "down"}
    base = bars("up", T.START["GOLDBEES"])
    px0 = base.close[-1] * 1.01
    pf, env, hist = d5_env(tmp_path, kinds, cash=20000.0, positions=[("GOLDBEES", int(80000 / px0), px0)])
    qty0 = pf.positions["GOLDBEES"].qty
    px = {"SILVERBEES": last(hist, "SILVERBEES"), "HINDCOPPER": last(hist, "HINDCOPPER"), "GOLDBEES": px0}
    F.fastlane_file(env, I, {"GOLDBEES": {"tighten_stop": round(px0 * 0.99, 2)}}, 11, 0)
    tick(pf, env, px, 11, 0, hist)
    stop = pf.positions["GOLDBEES"].ai_stop
    assert stop > 0
    value = pf.cash + qty0 * px0 * 0.99
    for m in (1, 2):
        tick(pf, env, dict(px, GOLDBEES=stop - 0.1), 11, m, hist)
    pos = pf.positions["GOLDBEES"]
    assert 0 < pos.qty < qty0 and pos.ai_stop == 0.0
    assert pos.qty * (stop - 0.1) / value == pytest.approx(0.5, abs=0.02)                     # exactly the floor is kept
    s = reasons(env, "SELL", "GOLDBEES")
    assert len(s) == 1 and s[0]["qty"] == qty0 - pos.qty and "floor" in s[0]["reason"] and "×" in s[0]["reason"]


def test_7_gold_under_its_sma_is_not_sold_intraday(tmp_path):
    kinds = {"GOLDBEES": "up", "SILVERBEES": "down", "HINDCOPPER": "down"}
    px0 = bars("up", T.START["GOLDBEES"]).close[-1]
    pf, env, hist = d5_env(tmp_path, kinds, cash=20000.0, positions=[("GOLDBEES", int(80000 / px0), px0)])
    px = {"SILVERBEES": last(hist, "SILVERBEES"), "HINDCOPPER": last(hist, "HINDCOPPER")}
    for m in range(0, 6):
        tick(pf, env, dict(px, GOLDBEES=px0 * 0.80), 10, m, hist)                         # far under SMA200 x 0.995
    assert pf.positions["GOLDBEES"].qty == int(80000 / px0) and not reasons(env, "SELL", "GOLDBEES")


# ---------------------------------------------------------------- 5, 6: the floor buy cannot be skipped
def bias_block(env, who="GOLDBEES", hh=14, mm=59):
    F.fastlane_file(env, I, {who: {"bias": "block"}}, hh, mm)


def test_5_block_bias_on_trend_on_gold_buys_up_to_the_floor_and_no_further(tmp_path):
    kinds = {"GOLDBEES": "up", "SILVERBEES": "down", "HINDCOPPER": "down"}
    pf, env, hist = d5_env(tmp_path, kinds)
    px = {s: last(hist, s) * 1.01 for s in SYMS}
    bias_block(env)
    tick(pf, env, px, 15, 10, hist)
    w = pf.positions["GOLDBEES"].qty * px["GOLDBEES"] / (pf.cash + pf.positions["GOLDBEES"].qty * px["GOLDBEES"])
    assert w == pytest.approx(0.5, abs=0.02), w                                                # the floor, not the 100% trend target


def test_regression_28_oct_block_bias_with_no_metals_still_buys_the_floor(tmp_path):
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS})
    px = {s: last(hist, s) for s in SYMS}
    bias_block(env)
    tick(pf, env, px, 15, 10, hist)
    assert "GOLDBEES" in pf.positions
    w = pf.positions["GOLDBEES"].qty * px["GOLDBEES"] / (pf.cash + pf.positions["GOLDBEES"].qty * px["GOLDBEES"])
    assert w == pytest.approx(0.5, abs=0.02), w


def test_6_a_news_pause_still_on_at_the_end_of_the_window_does_not_skip_the_floor_buy(tmp_path):
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS})
    px = {s: last(hist, s) for s in SYMS}
    until = datetime(T.DAYS[I].year, T.DAYS[I].month, T.DAYS[I].day, 15, 50, tzinfo=IST)
    F.fastlane_file(env, I, {"GOLDBEES": {"block_until": until.isoformat()}}, 15, 9)
    tick(pf, env, px, 15, 10, hist)
    tick(pf, env, px, 15, 20, hist)
    assert "GOLDBEES" not in pf.positions                                                      # delayed while the window is open
    assert any(d["action"] == "BLOCK" and "paused" in d["reason"] for d in reasons(env, "BLOCK", "GOLDBEES"))
    tick(pf, env, px, 15, 25, hist)
    assert "GOLDBEES" in pf.positions                                                          # made at check_until
    assert any("restore the gold floor" in d["reason"] or "units to reach target" in d["reason"] for d in reasons(env, "BUY", "GOLDBEES"))


def test_the_floor_buy_is_still_blocked_in_safe_mode_and_by_the_account_stop(tmp_path):
    from simgold import alerts
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS})
    px = {s: last(hist, s) for s in SYMS}
    env.state_dir.mkdir(parents=True, exist_ok=True)
    alerts.safe_path(env.state_dir).write_text('{"on": true}')
    assert alerts.safe_mode_on(env.state_dir)
    tick(pf, env, px, 15, 25, hist)                                                            # safe mode, at check_until
    assert "GOLDBEES" not in pf.positions
    alerts.safe_path(env.state_dir).unlink()
    pf2, env2, hist2 = d5_env(tmp_path / "b", {s: "down" for s in SYMS}, cash=70000.0)         # under the 75,000 account stop
    tick(pf2, env2, px, 15, 25, hist2)
    assert "GOLDBEES" not in pf2.positions


# ---------------------------------------------------------------- 9: pure functions
def test_9_next_exit_level_equals_the_next_bar_stop_from_frame_and_floor_weight_follows_the_rules():
    st = d5_params()["strategy"]
    c = 100 + np.arange(300) * 1.0
    h, lo = c + 0.5, c - 0.5                                                                   # constant true range 1.5
    lvl = trend.next_exit_level(c.tolist(), h.tolist(), lo.tolist(), st)
    nxt = trend.frame(np.append(c, c[-1]), np.append(h, c[-1] + 0.75), np.append(lo, c[-1] - 0.75), "donchian", st)   # same true range, no new peak
    assert nxt["on"].iloc[-1] == 1 and lvl == pytest.approx(nxt["stop"].iloc[-1], rel=1e-9)
    parts = trend.next_exit_parts(c.tolist(), h.tolist(), lo.tolist(), st)
    assert parts["low"] == lo[-20:].min() and parts["level"] == max(parts["peak"] - 3 * parts["atr"], parts["low"])
    off = np.concatenate([c, c[-1] - np.arange(1, 60) * 3.0])                                   # a crash: the state is OFF, no level
    assert trend.next_exit_level(off.tolist(), (off + 0.5).tolist(), (off - 0.5).tolist(), st) is None
    assert trend.next_exit_level(c[:30].tolist(), h[:30].tolist(), lo[:30].tolist(), st) is None
    w = {"GOLDBEES": 0.9, "SILVERBEES": 0.3, "HINDCOPPER": 0.1}
    assert trend.floor_weight(None, w, st, 0) == pytest.approx(0.1) and trend.floor_weight(None, w, st, 1) == pytest.approx(0.1)
    assert trend.floor_weight(None, w, st, 2) == 0.0 and trend.floor_weight(None, {"GOLDBEES": 1.0}, st, 0) == pytest.approx(0.5)
    assert trend.floor_weight(None, {"GOLDBEES": 0.0, "SILVERBEES": 0.6}, st, 0) == 0.0
    assert trend.floor_weight(None, w, dict(st, min_invested=0.0), 0) == 0.0


# ---------------------------------------------------------------- 10: the backtest option
def bt_frames(n=900, seed=5):
    idx = pd.bdate_range("2013-01-01", periods=n)
    rng = np.random.default_rng(seed)
    c = pd.DataFrame({s: 100 * np.cumprod(1 + 0.0012 + rng.normal(0, 0.012, n)) for s in SYMS}, index=idx)
    return {"close": c, "high": c * 1.01, "low": c * 0.99, "open": c.shift(1).fillna(c.iloc[0])}


def test_10_empty_intraday_exit_reproduces_the_older_backtest_exactly():
    f = bt_frames()
    old = d5_params()
    old["strategy"].pop("stops")
    off = copy.deepcopy(old)
    off["strategy"]["stops"] = {"intraday_exit": [], "confirm_ticks": 2, "gap": "sell", "protect_floor": True}
    a = backtest.run_v2(f, old, "2013-06-01")
    b = backtest.run_v2(f, off, "2013-06-01")
    pd.testing.assert_series_equal(a["equity"], b["equity"], check_exact=True)
    pd.testing.assert_frame_equal(a["trades"], b["trades"], check_exact=True)
    assert "stop" not in set(b["trades"]["kind"])
    assert float(a["equity"].iloc[-1]) == pytest.approx(272300.6341158664, rel=1e-12) and len(a["trades"]) == 70   # numbers of origin/main before D5-S


def test_backtest_intraday_stop_fills_at_min_of_open_and_level_and_waits_for_a_new_high():
    f = bt_frames()
    p = d5_params()
    on = backtest.run_v2(f, p, "2013-06-01")
    stops = on["trades"][on["trades"]["kind"] == "stop"]
    assert len(stops) > 0 and set(stops["sym"]) <= {"SILVERBEES", "HINDCOPPER"} and (stops["dv"] < 0).all()
    assert (on["gross"] <= 1.0 + 1e-9).all()
    # a hand-made gap: silver holds a position, then opens far under the frozen level -> sold at the open less the extra slippage
    g = bt_frames()
    sc = g["close"]["SILVERBEES"]
    d0, sym = stops["date"].iloc[0], stops["sym"].iloc[0]
    k = on["weights"].index.get_loc(d0)                                                        # position in the result
    assert on["weights"][sym].iloc[k - 1] > 0
    g["open"].iloc[sc.index.get_loc(d0), g["open"].columns.get_loc(sym)] = 1.0                                    # absurd gap open
    r = backtest.run_v2(g, p, "2013-06-01")
    assert r["equity"].iloc[k] < on["equity"].iloc[k]                                          # filled at the gap open, a worse price than the level
    assert r["weights"][sym].iloc[k] == 0.0
