"""Strategy v3 in the engine (simgold/engine.py), DORMANT by default.

  * strategy.mode v2_trend (the shipped value): NOTHING changes. The stored minute ticks of 13, 16 and 17 Nov 2026 are replayed
    through the real tick and the decisions and runtime state are compared byte for byte with a golden file written by the
    engine as it was on origin/main BEFORE the v3 change (tests/fixtures/v3_dormant/, tests/v3_replay.py).
  * mode v3_alloc with allocation.shadow true: the v2 decision still trades (same trades, byte for byte, once the ALLOC records
    are set aside); the v3 target is only logged.
  * mode v3_alloc with shadow false: D5-FI (and, in phase live, the AI tilt) trades; leftover cash is reinvested; the brakes:
    level 1 rotates to GOLDBEES and never creates cash, level 2 and the account stop go to cash.
"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from simgold import trend
from simgold.broker import Portfolio, Position
from simgold.engine import Env, Market, run_tick
from simgold.market import IST
import v3_replay as R

SYMS = R.SYMS
FIX = R.FIXTURES


def golden(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ dormant: byte-identical decisions
def test_shipped_mode_is_v2_trend_and_the_golden_has_real_trades():
    assert R.shipped_params()["strategy"]["mode"] == "v2_trend"
    g = golden("decisions.jsonl")
    acts = [json.loads(x)["action"] for x in g.splitlines()]
    assert "BUY" in acts and "TARGET" in acts and "ALLOC" not in acts          # the golden is a real v2 run, no v3 anywhere


def test_v2_trend_is_byte_identical_to_the_engine_before_v3(tmp_path):
    pf, env = R.replay(tmp_path, R.shipped_params())
    assert R.decisions_text(env) == golden("decisions.jsonl")
    assert R.runtime_text(pf) == golden("runtime.json")
    assert "v3" not in pf.runtime and "ALLOC" not in R.decisions_text(env)


def test_shadow_v3_logs_but_does_not_trade(tmp_path):
    p = R.shipped_params("v3_alloc")                                            # shadow true (shipped)
    assert p["allocation"]["shadow"] is True
    pf, env = R.replay(tmp_path, p)
    text = R.decisions_text(env)
    recs = [json.loads(x) for x in text.splitlines()]
    allocs = [r for r in recs if r["action"] == "ALLOC"]
    assert len(allocs) == 3 and [a["ts"][:10] for a in allocs] == R.DAYS          # one per daily check
    # everything else, byte for byte, is what v2 did
    assert R.decisions_text(env, drop_actions=("ALLOC",)) == golden("decisions.jsonl")
    assert R.runtime_text(pf, drop_keys=("v3",)) == golden("runtime.json")
    a = allocs[0]
    assert a["alloc_mode"] == "shadow_v2_trades" and a["trigger"] == "Allocation"
    al = a["alloc"]
    assert set(al["default"]) == set(SYMS) and abs(sum(al["default"].values()) - 1.0) < 1e-6      # fully invested default
    assert al["k"] == 0.0 and al["advice"] == "none" and set(a["v2_targets"]) == set(SYMS)
    assert any(c["rule"] == "no_proposal" for c in al["clamps"])
    assert al["clamps"] and all({"rule", "sym", "from", "to"} <= set(c) for c in al["clamps"])
    assert a["executed"] and a["executed"][0]["action"] == "BUY"                # what v2 really did at that check
    assert "shadow_ledger" in pf.runtime["v3"] and "ledger" not in pf.runtime["v3"]


# ------------------------------------------------------------------ live (shadow false): the default trades fully invested
def live_params(**alloc):
    return R.shipped_params("v3_alloc", shadow=False, **alloc)


def test_live_default_ramps_in_and_logs(tmp_path):
    pf, env = R.replay(tmp_path, live_params())
    recs = [json.loads(x) for x in R.decisions_text(env).splitlines()]
    allocs = [r for r in recs if r["action"] == "ALLOC"]
    assert len(allocs) == 3 and allocs[0]["alloc_mode"] == "d5fi_ai_logged_only"
    buys = [r for r in recs if r["action"] == "BUY"]
    assert buys, "the default must buy"
    first = allocs[0]["alloc"]
    # day 1: the whole book is cash, the turnover limit lets 20 points in
    assert first["final"]["GOLDBEES"] == pytest.approx(0.20, abs=1e-3) and first["turnover"]["discretionary"] == pytest.approx(0.20, abs=1e-3)
    assert any(c["rule"] == "turnover_limit" for c in first["clamps"])
    # the ledger is bounded by the week budget and holds the three days
    led = pf.runtime["v3"]["ledger"]
    assert sum(led.values()) <= 0.40 + 3 * 0.03 and len(led) == 3        # the week budget plus the 3-point dust sweep a day
    assert "would_have" in allocs[0]
    v = pf.cash + sum(p.qty * p.last_price for p in pf.positions.values())
    # 20 points on 13 and 16 Nov, then the 40-point week budget is used up: the ramp-in from an all-cash book is deliberately slow
    # (only the small dust sweep, at most 3 points a day, goes beyond the budget)
    assert 0.35 < 1 - pf.cash / v < 0.50 and pf.positions
    assert allocs[2]["alloc"]["turnover"]["discretionary"] == pytest.approx(0.0, abs=1e-6)


def test_ai_proposal_is_tilted_then_carried_half_then_dropped(tmp_path):
    prop = {"for_date": "2026-11-13", "written": "2026-11-13T09:00:00+05:30", "by": "allocation", "phase": "final",
            "weights": {"GOLDBEES": 0.70, "SILVERBEES": 0.20, "HINDCOPPER": 0.10}, "confidence": 0.8,
            "scenarios": [{"name": "base", "p": 0.6, "GOLD": "flat"}, {"name": "bear", "p": 0.4, "GOLD": "down"}],
            "rationale": {"GOLD": "DXY 104"}, "invalidate": []}
    p = live_params(phase="live", ai_trust=1.0)
    pf, env = R.replay(tmp_path, p, state_files={"allocation.json": json.dumps(prop)})
    allocs = [json.loads(x) for x in R.decisions_text(env).splitlines() if json.loads(x)["action"] == "ALLOC"]
    assert [a["alloc"]["advice"] for a in allocs] == ["final", "carry", "none"]       # 13 Nov, +1 trading day, then default only
    assert allocs[0]["alloc"]["k"] == pytest.approx(1.0) and allocs[1]["alloc"]["k"] == pytest.approx(0.2)      # confidence 0.8 x 0.5 = 0.4
    assert allocs[2]["alloc"]["k"] == 0.0
    a0 = allocs[0]["alloc"]
    assert a0["after_tilt"]["GOLDBEES"] < a0["default"]["GOLDBEES"] and a0["proposal"]["SILVERBEES"] == 0.2
    assert allocs[0]["alloc_mode"] == "v3_live" and "would_have" not in allocs[0]
    assert any(c["rule"] == "carry_yesterday" for c in allocs[1]["alloc"]["clamps"])
    assert any(c["rule"] == "stale_proposal" for c in allocs[2]["alloc"]["clamps"])


def test_invalid_hand_edited_file_never_breaks_the_tick(tmp_path):
    for bad in ('{"weights": {"GOLDBEES": 2.0}}', "{not json", '{"for_date": "2026-11-13", "qty": 5}', "[]"):
        d = tmp_path / str(abs(hash(bad)))
        d.mkdir()
        pf, env = R.replay(d, live_params(phase="live", ai_trust=1.0), days=R.DAYS[:1], state_files={"allocation.json": bad})
        allocs = [json.loads(x) for x in R.decisions_text(env, days=R.DAYS[:1]).splitlines() if json.loads(x)["action"] == "ALLOC"]
        assert allocs and allocs[0]["alloc"]["advice"] == "none" and allocs[0]["alloc"]["k"] == 0.0
        assert any(c["rule"] == "invalid_proposal" for c in allocs[0]["alloc"]["clamps"])
        assert pf.positions                                    # the default still bought


# ------------------------------------------------------------------ synthetic paths: cash dust and the brakes
DAYS = pd.bdate_range("2025-01-01", periods=520)
START = {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 400.0}


def make_path(rng, kind: str, n: int = 520):
    out = {}
    for k, s in enumerate(SYMS):
        vol = 0.004 + 0.002 * k
        if kind == "up":
            r = 0.0010 + rng.normal(0, vol, n)
        elif kind == "mixed":
            r = np.where((np.arange(n) // 60) % 2 == 0, 0.0012, -0.0010) + rng.normal(0, vol, n)
        else:                                                   # up, then a long crash
            r = np.where(np.arange(n) < 360, 0.0012, -0.012) + rng.normal(0, vol, n)
        out[s] = START[s] * np.cumprod(1 + r)
    return out


def v3_tick(pf, env, closes, i, hh=15, mm=10):
    day = DAYS[i].to_pydatetime()
    now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    prices = {s: float(closes[s][i]) for s in SYMS}
    hist = {}
    for s in SYMS:
        c = closes[s][:i]
        hist[s] = trend.clean_bars(c.tolist(), (c * 1.004).tolist(), (c * 0.996).tolist(), [d.date().isoformat() for d in DAYS[:i]])
    mkt = Market(now=now, prices=prices, price_ts={s: now for s in SYMS}, scores={}, prev_close={s: float(closes[s][i - 1]) for s in SYMS},
                 hist=hist)
    return run_tick(pf, mkt, env)


def v3_env(tmp_path, **alloc):
    p = R.shipped_params("v3_alloc", shadow=False, **alloc)
    (tmp_path / "state").mkdir(exist_ok=True)
    return Env(state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", params=p, environ={}, match_headline=None)


def book(pf, closes, i):
    px = {s: float(closes[s][i]) for s in SYMS}
    v = pf.cash + pf.liquidbees + sum(p.qty * px[s] for s, p in pf.positions.items())
    return px, v


def invested_book(closes, i, split=(0.6, 0.25, 0.15), total=100000.0) -> Portfolio:
    """A book that is already fully invested (no ramp-in): `split` of `total` in the three ETFs, the rounding left as cash."""
    pf = Portfolio()
    pf.cash, pf.liquidbees = total, 0.0
    for s, w in zip(SYMS, split):
        px = float(closes[s][i])
        q = int(w * total / px)
        pf.positions[s] = Position(qty=q, avg_cost=px, last_price=px, opened="2026-01-01", high_water=px)
        pf.cash -= q * px
    return pf


def brake_level(pf) -> int:
    return int(((pf.runtime.get("v2") or {}).get("brake") or {}).get("level") or 0)


@pytest.mark.parametrize("seed,kind", [(1, "up"), (2, "up"), (3, "mixed"), (4, "mixed"), (5, "mixed"), (6, "mixed"), (7, "crash"),
                                       (8, "crash"), (9, "up"), (10, "mixed"), (11, "crash"), (12, "mixed")])
def test_cash_is_only_dust_after_a_rebalance_and_level1_never_creates_cash(tmp_path, seed, kind):
    """Owner decision 18 Nov: nothing idles in cash. After the daily check, cash <= about one unit of the cheapest ETF plus the 2%
    buffer, in every state except brake level 2 and the account stop (those go to cash by design; level 1 rotates to gold)."""
    rng = np.random.default_rng(seed)
    closes = make_path(rng, kind)
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299)
    checked = l1_days = l2_days = 0
    for i in range(300, 519):
        v3_tick(pf, env, closes, i)
        lvl = brake_level(pf)
        halted = bool(pf.cooldowns.get("account_stop_latched"))
        px, v = book(pf, closes, i)
        if halted:
            continue
        if lvl >= 2:
            l2_days += 1
            continue
        if lvl == 1:
            l1_days += 1
        cheapest = min(px.values())
        assert pf.cash <= 0.02 * v + cheapest + 1e-6, (i, lvl, pf.cash, v, {s: p.qty for s, p in pf.positions.items()})
        checked += 1
    assert checked > (30 if kind == "crash" else 100)
    if kind == "crash":
        assert l1_days + l2_days > 0


def test_brake_level_2_and_account_stop_go_to_cash(tmp_path):
    rng = np.random.default_rng(7)
    closes = make_path(rng, "crash")
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299)
    seen2 = False
    for i in range(300, 519):
        v3_tick(pf, env, closes, i)
        if brake_level(pf) >= 2 and not seen2:
            seen2 = True
            px, v = book(pf, closes, i)
            assert not pf.positions and pf.cash / v > 0.97     # everything sold, nothing re-bought, the book is cash
            rec = [json.loads(x) for x in (env.logs_dir / f"decisions-{DAYS[i].date().isoformat()}.jsonl").read_text().splitlines()]
            al = [r for r in rec if r["action"] == "ALLOC"]
            assert al and al[0]["alloc"]["brake_level"] == 2 and all(w == 0 for w in al[0]["alloc"]["final"].values())
    assert seen2
    # the hard account stop: a book under 75,000 is flat and no cash sweep buys it back
    pf2 = Portfolio()
    pf2.cash, pf2.liquidbees = 0.0, 0.0
    pf2.positions["GOLDBEES"] = Position(qty=int(70000 / closes["GOLDBEES"][400]), avg_cost=float(closes["GOLDBEES"][400]),
                                         last_price=float(closes["GOLDBEES"][400]), opened="2026-01-01")
    pf2.cash = 1000.0
    (tmp_path / "stop").mkdir()
    env2 = v3_env(tmp_path / "stop")
    v3_tick(pf2, env2, closes, 400, hh=10, mm=0)                       # the stop needs two consecutive fresh ticks under 75,000
    v3_tick(pf2, env2, closes, 400, hh=10, mm=1)
    assert not pf2.positions and pf2.cooldowns.get("account_stop_latched")
    v3_tick(pf2, env2, closes, 401)
    v3_tick(pf2, env2, closes, 402)
    v3_tick(pf2, env2, closes, 403)
    assert not pf2.positions


def test_level1_rotates_into_gold(tmp_path):
    """Force brake level 1 and look at the ALLOC record: silver/copper cut, gold takes the freed weight, total unchanged."""
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299)
    for i in range(300, 330):
        v3_tick(pf, env, closes, i)
    pf.runtime["v2"]["brake"].update({"level": 1, "peak": 200000.0, "days": 5, "rebase_value": None, "brake_day": None})
    pf.runtime["v2"]["brake_day"] = None
    pf.runtime["v2"]["brake"]["peak"] = 1e9                 # value is far under its peak: level 1 or 2? keep level 1 by a peak 16% up
    px, v = book(pf, closes, 330)
    pf.runtime["v2"]["brake"]["peak"] = v / 0.84
    v3_tick(pf, env, closes, 330)
    rec = [json.loads(x) for x in (env.logs_dir / f"decisions-{DAYS[330].date().isoformat()}.jsonl").read_text().splitlines()]
    al = [r for r in rec if r["action"] == "ALLOC"][0]["alloc"]
    assert al["brake_level"] == 1
    assert sum(al["after_brake"].values()) == pytest.approx(sum(al["after_vol"].values()), abs=1e-3)       # no cash weight
    assert any(c["rule"] == "brake_level1" for c in al["clamps"]) or al["after_brake"] == al["after_vol"]


def test_intraday_invalidation_sells_the_excess_once_two_fresh_prices_confirm(tmp_path):
    rng = np.random.default_rng(3)
    closes = make_path(rng, "mixed")
    i = 330
    env = v3_env(tmp_path, phase="live", ai_trust=1.0)
    pf = invested_book(closes, i - 1, split=(0.2, 0.7, 0.1))                    # 70% in silver, far above any default
    day = DAYS[i].date().isoformat()
    level = float(closes["SILVERBEES"][i]) * 1.2                                 # the price is well under the invalidation level
    prop = {"for_date": day, "written": f"{day}T08:55:00+05:30", "by": "allocation", "phase": "final",
            "weights": {"GOLDBEES": 0.4, "SILVERBEES": 0.5, "HINDCOPPER": 0.1}, "confidence": 0.8,
            "scenarios": [{"name": "base", "p": 0.6}, {"name": "bear", "p": 0.4}], "invalidate": [f"close_below:SILVERBEES:{level:.2f}"]}
    (env.state_dir).mkdir(parents=True, exist_ok=True)
    (env.state_dir / "allocation.json").write_text(json.dumps(prop), encoding="utf-8")
    v3_tick(pf, env, closes, i, hh=10, mm=0)
    assert pf.positions["SILVERBEES"].qty == invested_book(closes, i - 1, split=(0.2, 0.7, 0.1)).positions["SILVERBEES"].qty   # one tick: nothing
    v3_tick(pf, env, closes, i, hh=10, mm=1)                                     # the second consecutive fresh tick
    recs = [json.loads(x) for x in (env.logs_dir / f"decisions-{day}.jsonl").read_text().splitlines()]
    sells = [r for r in recs if r["action"] == "SELL" and r["symbol"] == "SILVERBEES"]
    assert len(sells) == 1 and sells[0]["trigger"] == "Allocation" and "invalidation level" in sells[0]["reason"]
    assert not [r for r in recs if r["action"] == "BUY"]                          # nothing is bought intraday
    before = pf.positions["SILVERBEES"].qty
    v3_tick(pf, env, closes, i, hh=10, mm=2)
    assert pf.positions["SILVERBEES"].qty == before                              # once per day
    assert pf.cash > 0.2 * 100000                                                # the proceeds wait for the daily check


def test_event_rebalance_is_off_in_shadow_and_without_a_proposal(tmp_path):
    rng = np.random.default_rng(3)
    closes = make_path(rng, "mixed")
    i = 330
    pf = invested_book(closes, i - 1, split=(0.2, 0.7, 0.1))
    q0 = pf.positions["SILVERBEES"].qty
    env = v3_env(tmp_path, phase="live", ai_trust=1.0)
    v3_tick(pf, env, closes, i, hh=10, mm=0)
    v3_tick(pf, env, closes, i, hh=10, mm=1)
    assert pf.positions["SILVERBEES"].qty == q0                                  # no proposal: nothing happens before 15:10
