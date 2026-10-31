"""D5 "stay invested" (docs/RULEBOOK.md Strategy v2): 50% metals floor in GOLDBEES, stop moves to gold while gold's trend is ON,
brakes keep the floor at level 1 and go flat (floor included) at level 2."""
from __future__ import annotations

import pytest

import test_engine_v2 as T
import test_engine_v2_fixes as F
from conftest import d5_params
from simgold import backtest, trend
from simgold.broker import Portfolio
from simgold.engine import run_tick

ST = d5_params()["strategy"]


def W(g, s, c, mult=1.0, ineligible=()):
    return trend.target_weights_ex({"GOLDBEES": g, "SILVERBEES": s, "HINDCOPPER": c}, ST, mult, ineligible)


def test_floor_holds_half_in_gold_even_when_every_trend_is_off():
    w, _, d = W((False, 0.15), (False, 0.35), (False, 0.35))
    assert w == pytest.approx({"GOLDBEES": 0.5, "SILVERBEES": 0.0, "HINDCOPPER": 0.0})
    assert d["floor"] and d["metals_before"] == 0.0 and d["to_gold"] == 0.0 and not d["gold_trend"]


def test_floor_tops_up_only_what_is_missing():
    w, _, d = W((False, 0.15), (True, 0.35), (False, 0.35))          # silver ON: 0.14/0.35 = 0.4
    assert w["SILVERBEES"] == pytest.approx(0.4) and w["GOLDBEES"] == pytest.approx(0.1) and d["floor"]
    w, _, d = W((True, 0.15), (True, 0.35), (True, 0.35))            # plenty of metals: no floor, v2 scaling only
    assert not d["floor"] and sum(w.values()) == pytest.approx(1.0)


def test_stop_moves_to_gold_only_while_gold_trend_is_on():
    w, _, d = W((True, 0.28), (False, 0.35), (False, 0.35))          # gold 0.5 + silver room 0.4 + copper room 0.35 (capped), gold ON
    assert d["to_gold"] == pytest.approx(0.4 + 0.35) and sorted(d["to_gold_from"]) == ["HINDCOPPER", "SILVERBEES"]
    assert w["GOLDBEES"] == pytest.approx(1.0) and w["SILVERBEES"] == 0.0           # 0.5 + 0.75 = 1.25, scaled to 100%
    w, _, d = W((False, 0.28), (False, 0.35), (False, 0.35))         # same stops, gold OFF: nothing is handed over, the floor holds
    assert d["to_gold"] == 0.0 and w["GOLDBEES"] == pytest.approx(0.5)
    st = dict(ST, stop_to_gold_requires_gold_trend=False)
    w, _, d = trend.target_weights_ex({"GOLDBEES": (False, 0.28), "SILVERBEES": (False, 0.35), "HINDCOPPER": (False, 0.35)}, st)
    assert d["to_gold"] == pytest.approx(0.75) and w["GOLDBEES"] == pytest.approx(0.75)    # handed over even though gold is OFF


def test_never_levered_and_floor_off_gives_plain_v2():
    w, _, _ = W((True, 0.1), (True, 0.1), (True, 0.1))
    assert sum(w.values()) <= 1.0 + 1e-9
    pure = dict(ST, min_invested=0.0, stop_to_gold=False)
    w, _ = trend.target_weights({"GOLDBEES": (False, 0.15), "SILVERBEES": (False, 0.35), "HINDCOPPER": (False, 0.35)}, pure)
    assert sum(w.values()) == 0.0


def test_level_one_halves_but_keeps_the_floor_and_level_two_is_flat_including_the_floor():
    w, _, _ = W((False, 0.15), (False, 0.35), (False, 0.35), mult=0.5)
    assert w["GOLDBEES"] == pytest.approx(0.5)                                      # floor survives the -15% brake
    w, _, _ = W((True, 0.15), (True, 0.35), (True, 0.35), mult=0.5)                 # gold 0.93, silver 0.4, copper 0.35 -> scaled to 1
    full, _, _ = W((True, 0.15), (True, 0.35), (True, 0.35))
    assert w["SILVERBEES"] == pytest.approx(full["SILVERBEES"] * 0.5)
    assert w["GOLDBEES"] == pytest.approx(max(full["GOLDBEES"] * 0.5, min(full["GOLDBEES"], 0.5)))
    w, _, _ = W((False, 0.15), (False, 0.35), (False, 0.35), mult=0.0)
    assert sum(w.values()) == 0.0


def test_without_rsi_timing_entries_and_adds_go_straight_to_target():
    kw = dict(on=True, target=0.5, rsi_val=99.0, bias="neutral", st=ST)
    assert trend.decide(cur=0.0, flat=True, **kw).action == "BUY"                  # overbought does not wait
    assert trend.decide(cur=0.3, flat=False, **kw).action == "BUY"                 # no dip needed for an add
    assert trend.decide(cur=0.3, flat=False, **dict(kw, bias="block")).action == "SKIP"
    assert trend.decide(cur=0.47, flat=False, **kw).action == "HOLD"               # still the 5-point band


# ------------------------------------------------------------------ engine
def d5_env(tmp_path):
    return T.make_env(tmp_path, d5_params())


def test_falling_market_holds_the_gold_floor_with_the_documented_reason(tmp_path):
    closes = {s: T.series("down", s) for s in T.SYMS}
    env = d5_env(tmp_path)
    pf = T.fresh_pf()
    for i in range(260, 300):
        F.run_tick(pf, F.mk(closes, i, 15, 10), env)
    w, v = T.weights(pf)
    assert w["GOLDBEES"] == pytest.approx(0.5, abs=0.03) and w["SILVERBEES"] == 0 and w["HINDCOPPER"] == 0
    buy = next(d for d in T.all_decisions(env) if d["action"] == "BUY")
    assert buy["reason"].startswith("GOLDBEES floor: metals 0% < min_invested 50% → target 50% gold (trend OFF, floor holding")


def test_stop_to_gold_in_the_engine_and_the_floor_survives_a_trend_off(tmp_path):
    closes = {s: T.series("up", s) for s in T.SYMS}
    env = d5_env(tmp_path)
    pf = T.fresh_pf()
    F.run_tick(pf, F.mk(closes, 300, 15, 10), env)
    assert set(pf.positions) == set(T.SYMS)
    cr = F.crash(closes, 301, 0.85)                                                # everything turns OFF
    F.run_tick(pf, F.mk(cr, 301, 15, 10), env)
    assert set(pf.positions) == {"GOLDBEES"}
    w, _ = T.weights(pf)
    assert w["GOLDBEES"] == pytest.approx(0.5, abs=0.06)
    assert any("floor holding" in d["reason"] for d in T.read_decisions(env, 301) if d["action"] in ("SELL", "TARGET", "BUY"))


def test_brake_interaction_in_the_engine(tmp_path):
    closes = {s: T.series("down", s) for s in T.SYMS}
    env = d5_env(tmp_path)
    pf = T.fresh_pf()
    F.run_tick(pf, F.mk(closes, 300, 15, 10), env)
    assert "GOLDBEES" in pf.positions
    pf.runtime["v2"]["brake"] = {"peak": 100000.0 / 0.84, "level": 0, "reconfirm": [], "days": 0}     # -16%: level 1
    F.run_tick(pf, F.mk(closes, 301, 15, 10), env)
    assert pf.runtime["v2"]["brake"]["level"] == 1
    w, _ = T.weights(pf)
    assert w["GOLDBEES"] == pytest.approx(0.5, abs=0.06)                           # the floor survives level 1
    pf.runtime["v2"]["brake"] = {"peak": 100000.0 / 0.70, "level": 1, "reconfirm": [], "days": 0}     # -30%: level 2
    F.run_tick(pf, F.mk(closes, 302, 15, 10), env)
    assert pf.runtime["v2"]["brake"]["level"] == 2 and not pf.positions            # flat, floor included
    assert any(d["trigger"] == "Drawdown brake" and d["action"] == "SELL" for d in T.read_decisions(env, 302))


def test_account_stop_still_sells_everything_under_d5(tmp_path):
    closes = {s: T.series("down", s) for s in T.SYMS}
    env = d5_env(tmp_path)
    pf = T.fresh_pf(0.0)
    from simgold.broker import Position
    pf.positions["GOLDBEES"] = Position(qty=1000, avg_cost=100.0, opened="2025-01-01", last_price=100.0, high_water=100.0)
    pf.runtime["strategy_mode"] = "v2_trend"
    low = {s: float(closes[s][300]) for s in T.SYMS}
    low["GOLDBEES"] = 74.0
    F.run_tick(pf, F.mk(closes, 300, 11, 0, prices=low), env)
    assert pf.positions                                    # one tick under the stop only counts (2026-10-31)
    F.run_tick(pf, F.mk(closes, 300, 11, 1, prices=low), env)
    assert not pf.positions


def test_backtest_d5_stays_invested_in_a_falling_market():
    p = d5_params()
    r = backtest.run_v2(_falling_frames(), p, "2013-06-01")
    assert r["gross"].mean() > 0.4 and r["gross"].iloc[-1] > 0.3 and r["brake"].max() == 1       # held at the floor through the brake


def _falling_frames():
    import numpy as np
    import pandas as pd
    n = 900
    idx = pd.bdate_range("2013-01-01", periods=n)
    rng = np.random.default_rng(5)
    c = pd.DataFrame({s: 100 * np.cumprod(1 - 0.0005 + rng.normal(0, 0.006, n)) for s in T.SYMS}, index=idx)
    return {"close": c, "high": c * 1.004, "low": c * 0.996}


def test_shipped_yaml_has_the_d5_parameters():
    import yaml
    from simgold import config
    st = yaml.safe_load(open(config.PARAMS_PATH, encoding="utf-8"))["strategy"]
    assert (st["min_invested"], st["floor_symbol"], st["stop_to_gold"], st["stop_to_gold_requires_gold_trend"]) == (0.50, "GOLDBEES", True, True)
    assert st["vol_target"] == 0.14 and st["entry"]["rsi_timing"] is False
