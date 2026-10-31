"""Owner approval 2026-10-31: (1) the 75,000 account stop needs `account_confirm_ticks` consecutive FRESH ticks at or under it;
(2) the engine re-checks every guidance bias against the current alignment ceiling."""
from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

import pytest

import evalkit as K
from simgold import memstore
from simgold.broker import Position
from simgold.engine import Engine, Market

pytestmark = pytest.mark.scenario
D1 = "2026-11-04"
G = K.SYMS[0]


def stressed(tmp, n_ticks_cfg=None, nominal=79000):
    hist = K.synth_hist({G: "up"})
    px = {s: K.last_close(hist, s) for s in K.SYMS}
    pf = K.floor_pf(cash=0.0)
    q = int(nominal / px[G])
    pf.positions[G] = Position(qty=q, avg_cost=px[G], opened="2026-11-02", last_price=px[G], high_water=px[G])
    pf.cash = 80000 - q * px[G]
    K.seed_v2(pf, "2026-11-02")
    params = K.shipped_params()
    if n_ticks_cfg is not None:
        params["strategy"]["stops"]["account_confirm_ticks"] = n_ticks_cfg
    sim = K.Sim(tmp, params=params, hist=hist, pf=pf, expected_interest=-20000.0)
    sim.prev_close = dict(px)
    return sim, px


def low(px, f=0.8):
    return {s: v * f for s, v in px.items()}


def stops(sim):
    return [d for d in sim.decisions() if d["trigger"] == "Account stop"]


def test_config_value_is_two():
    assert K.shipped_params()["strategy"]["stops"]["account_confirm_ticks"] == 2


def test_one_bad_tick_no_sale(tmp_path):
    sim, px = stressed(tmp_path)
    t = K.at(D1, 11, 0)
    sim.tick(t, px)
    sim.tick(t + timedelta(minutes=1), low(px))
    assert not stops(sim) and G in sim.pf.positions
    assert not sim.pf.cooldowns.get("account_stop_latched")


def test_two_consecutive_real_ticks_sell_all_and_latch(tmp_path):
    sim, px = stressed(tmp_path)
    t = K.at(D1, 11, 0)
    sim.tick(t, px)
    sim.tick(t + timedelta(minutes=1), low(px))
    sim.tick(t + timedelta(minutes=2), low(px, 0.79))
    assert stops(sim) and G not in sim.pf.positions
    assert sim.pf.cooldowns.get("account_stop_latched")
    sim.tick(t + timedelta(minutes=3), px)                  # latched: stays halted, no buys
    assert not sim.trades("BUY")


def test_tick_recover_tick_no_sale(tmp_path):
    sim, px = stressed(tmp_path)
    t = K.at(D1, 11, 0)
    sim.tick(t, px)
    sim.tick(t + timedelta(minutes=1), low(px))
    sim.tick(t + timedelta(minutes=2), px)
    sim.tick(t + timedelta(minutes=3), low(px, 0.79))
    assert not stops(sim) and G in sim.pf.positions


def test_stale_repeated_price_does_not_count(tmp_path):
    sim, px = stressed(tmp_path)
    t = K.at(D1, 11, 0)
    sim.tick(t, px)
    lo = low(px)
    sim.tick(t + timedelta(minutes=1), lo)
    # the same print again, now stale (its timestamp stays that of minute 1): never a second count
    for k in range(2, 12):
        sim.tick(t + timedelta(minutes=k), lo, ages={s: k - 1 for s in K.SYMS})
    assert not stops(sim) and G in sim.pf.positions


def test_confirm_ticks_one_restores_single_tick_sale(tmp_path):
    sim, px = stressed(tmp_path, n_ticks_cfg=1)
    t = K.at(D1, 11, 0)
    sim.tick(t, px)
    sim.tick(t + timedelta(minutes=1), low(px))
    assert stops(sim) and G not in sim.pf.positions


# ---------------------------------------------------------------- engine ceiling
def engine_with(tmp_path, bias, alignment):
    sim = K.Sim(tmp_path, pf=K.floor_pf())
    now = K.at(D1, 11, 0)
    K.write_json(sim.env.state_dir / "guidance.json", {"updated": now.isoformat(), "bias": bias, "note": "t"})
    mkt = Market(now=now, prices={}, price_ts={}, scores={}, prev_close={}, hist={})
    eng = Engine(sim.pf, mkt, sim.env)
    return sim, eng, alignment


def ceil_lines(eng):
    return [r for r in eng.log if "alignment" in r["reason"]]


def al(g="favour", s="neutral", c="block"):
    return {m: {"ceiling": v, "state": "T"} for m, v in (("GOLD", g), ("SILVER", s), ("COPPER", c))}


def test_favour_above_ceiling_is_lowered_and_logged_once(tmp_path):
    sim, eng, a = engine_with(tmp_path, {"GOLD": "favour", "SILVER": "favour", "COPPER": "favour"}, al())
    with mock.patch.object(memstore, "current_alignment", return_value=a):
        bias, _, _ = eng.guidance()
        assert bias["GOLDBEES"] == "favour" and bias["SILVERBEES"] == "neutral" and bias["HINDCOPPER"] == "block"
        eng.guidance()
        eng.guidance()
    assert len(ceil_lines(eng)) == 2                          # silver and copper, once each


def test_neutral_and_block_untouched(tmp_path):
    sim, eng, a = engine_with(tmp_path, {"GOLD": "neutral", "SILVER": "block", "COPPER": "favour"}, al("block", "neutral", "favour"))
    with mock.patch.object(memstore, "current_alignment", return_value=a):
        bias, _, _ = eng.guidance()
    assert bias["GOLDBEES"] == "neutral" and bias["SILVERBEES"] == "block" and bias["HINDCOPPER"] == "favour"
    assert not ceil_lines(eng)


@pytest.mark.parametrize("bad", [{}, {"GOLD": None}, {"GOLD": "garbage"}, {"GOLD": {"ceiling": "bogus"}}, {"GOLD": {}}])
def test_missing_or_garbled_alignment_favour_becomes_neutral(tmp_path, bad):
    sim, eng, _ = engine_with(tmp_path, {"GOLD": "favour"}, bad)
    with mock.patch.object(memstore, "current_alignment", return_value=bad):
        bias, _, _ = eng.guidance()
    assert bias["GOLDBEES"] == "neutral"


def test_alignment_raising_is_failsafe(tmp_path):
    sim, eng, _ = engine_with(tmp_path, {"GOLD": "favour"}, None)
    with mock.patch.object(memstore, "current_alignment", side_effect=RuntimeError("boom")):
        bias, _, _ = eng.guidance()
    assert bias["GOLDBEES"] == "neutral"


def test_logged_again_when_state_changes(tmp_path):
    sim, eng, _ = engine_with(tmp_path, {"SILVER": "favour"}, None)
    with mock.patch.object(memstore, "current_alignment", return_value=al(s="neutral")):
        eng.guidance()
    with mock.patch.object(memstore, "current_alignment", return_value=al(s="block")):
        eng.guidance()
    assert len(ceil_lines(eng)) == 2
