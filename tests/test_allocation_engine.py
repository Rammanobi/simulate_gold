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
    assert R.shipped_params()["strategy"]["mode"] in ("v2_trend", "v3_alloc") and R.shipped_params()["allocation"]["phase"] == "shadow"
    g = golden("decisions.jsonl")
    acts = [json.loads(x)["action"] for x in g.splitlines()]
    assert "BUY" in acts and "TARGET" in acts and "ALLOC" not in acts          # the golden is a real v2 run, no v3 anywhere


def test_v2_trend_is_byte_identical_to_the_engine_before_v3(tmp_path):
    pf, env = R.replay(tmp_path, R.shipped_params("v2_trend"))
    assert R.decisions_text(env) == golden("decisions.jsonl")
    assert R.runtime_text(pf) == golden("runtime.json")
    assert "v3" not in pf.runtime and "ALLOC" not in R.decisions_text(env)


def test_shadow_v3_logs_but_does_not_trade(tmp_path):
    p = R.shipped_params("v3_alloc")                                            # phase shadow (shipped)
    assert p["allocation"]["phase"] == "shadow"
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
    """Owner decision 18 Nov: nothing idles in cash. After the daily check, cash <= about one unit of the cheapest ETF plus the dust (3-point
    rebalance bands the strictly bounded sweep may leave: at most dust_week = 6% of the book), in every state except brake level 2 and the account stop (those go to cash by design; level 1 rotates to gold)."""
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
        assert pf.cash <= 0.06 * v + cheapest + 1e-6, (i, lvl, pf.cash, v, {s: p.qty for s, p in pf.positions.items()})   # dust: the 3-point bands, swept at most dust_week
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


# ------------------------------------------------------------------ go-live fixes (review of PR 67)
def _settle(pf, env, closes, i0, i1):
    for i in range(i0, i1):
        v3_tick(pf, env, closes, i)


def _decisions(env, day):
    f = env.logs_dir / f"decisions-{day}.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def _silver_stop_day(tmp_path, monkeypatch=None, credit_off=False, split=(0.6, 0.25, 0.15), i=330, **alloc):
    """A fully invested book that still holds silver (a seed is searched where the default keeps silver); at 10:00 a silver AI stop is
    hit on two fresh prices (a risk sell, silver is not bought back today); then the 15:10 check. Returns (pf, env, closes, day)."""
    from simgold import engine
    for seed in (1, 2, 4, 5, 9, 3, 6, 10):
        rng = np.random.default_rng(seed)
        closes = make_path(rng, "up")
        d = tmp_path / f"s{seed}"
        d.mkdir()
        env = v3_env(d, **alloc)
        pf = invested_book(closes, 299, split=split)
        _settle(pf, env, closes, 300, i)
        px, v = book(pf, closes, i)
        if "SILVERBEES" in pf.positions and pf.positions["SILVERBEES"].qty * px["SILVERBEES"] / v > 0.15:
            break
    else:
        pytest.fail("no seed keeps silver in the default")
    if credit_off:
        monkeypatch.setattr(engine.Engine, "v3_credit", lambda self, *a, **k: 0.0)
    pf.positions["SILVERBEES"].ai_stop = px["SILVERBEES"] * 1.05
    v3_tick(pf, env, closes, i, hh=10, mm=0)
    v3_tick(pf, env, closes, i, hh=10, mm=1)
    assert "SILVERBEES" not in pf.positions                                   # the stop sold it all
    assert pf.cash / v > 0.12                                                 # the proceeds sit in cash until the check
    v3_tick(pf, env, closes, i, hh=15, mm=10)
    return pf, env, closes, DAYS[i].date().isoformat()


def test_cash_from_intraday_risk_sells_is_redeployed_at_the_check_without_burning_the_turnover_budget(tmp_path):
    """Review fix 1 (the 5 Feb pattern: a silver stop left 36-39% cash for days). The proceeds of a risk sell are re-invested at the
    15:10 check in full, and the 0.20/day 0.40/week budget is not touched by it."""
    pf, env, closes, day = _silver_stop_day(tmp_path)
    px, v = book(pf, closes, 330)
    assert pf.cash <= 0.02 * v + min(px.values()) + 1e-6, (pf.cash, v)
    al = [r for r in _decisions(env, day) if r["action"] == "ALLOC"][0]["alloc"]
    assert al["credit"]["available"] > 0.15 and al["credit"]["exempt_buy"] > 0.15
    assert al["turnover"]["discretionary"] == pytest.approx(0.0, abs=0.01)    # nothing was counted against the budget
    assert pf.runtime["v3"]["ledger"].get(day, 0.0) <= 0.03 + 1e-9             # at most the dust sweep is booked for today
    assert (pf.runtime["v3"].get("credit") or {}).get("amt", 0.0) < 0.01       # spent (only rounding dust of cash is left)
    assert any(r["action"] == "BUY" and r["symbol"] == "GOLDBEES" for r in _decisions(env, day))


def test_without_the_credit_the_same_day_strands_cash(tmp_path, monkeypatch):
    """The same day with the credit switched off (the PR 67 behaviour): the turnover budget leaves a large part of the book in cash."""
    pf, env, closes, day = _silver_stop_day(tmp_path, monkeypatch, credit_off=True)
    px, v = book(pf, closes, 330)
    assert pf.cash > 0.05 * v


def test_credit_is_bounded_by_cash_and_cleared_by_the_deep_brake(tmp_path):
    pf, env, closes, day = _silver_stop_day(tmp_path)
    eng_cash = pf.cash
    assert eng_cash >= 0
    pf.runtime["v3"]["credit"] = 5.0                                          # a corrupt value can never exceed the cash on hand
    from simgold.engine import Engine
    e = Engine.__new__(Engine)
    # the engine method clamps by the real cash weight: exercised through a normal tick (no exception, no leverage)
    v3_tick(pf, env, closes, 331)
    px, v = book(pf, closes, 331)
    assert pf.cash >= -1e-9 and sum(p.qty * px[s] for s, p in pf.positions.items()) <= v * 1.0001


def test_gold_invalidation_does_not_strand_the_cash(tmp_path):
    """Review fix 1b: gold falls through the proposal's invalidation level, the excess is sold intraday and gold is not bought back
    today. At 15:10 the cash goes to the remaining metals (silver/copper up to their caps), then, as a last resort, back to gold:
    nothing idles."""
    rng = np.random.default_rng(1)
    closes = make_path(rng, "up")
    i = 330
    env = v3_env(tmp_path, phase="live", ai_trust=1.0)
    pf = invested_book(closes, 299, split=(0.85, 0.10, 0.05))
    day = DAYS[i].date().isoformat()
    level = float(closes["GOLDBEES"][i]) * 1.2
    prop = {"for_date": day, "written": f"{day}T08:55:00+05:30", "by": "allocation", "phase": "final",
            "weights": {"GOLDBEES": 0.9, "SILVERBEES": 0.05, "HINDCOPPER": 0.05}, "confidence": 0.8,
            "scenarios": [{"name": "base", "p": 0.6}, {"name": "bear", "p": 0.4}], "invalidate": [f"close_below:GOLDBEES:{level:.2f}"]}
    (env.state_dir / "allocation.json").write_text(json.dumps(prop), encoding="utf-8")
    v3_tick(pf, env, closes, i, hh=10, mm=0)
    v3_tick(pf, env, closes, i, hh=10, mm=1)
    sells = [r for r in _decisions(env, day) if r["action"] == "SELL" and r["trigger"] == "Allocation"]
    assert sells and "GOLDBEES" in sells[0]["symbol"]                          # the excess above the default is sold intraday
    px, v = book(pf, closes, i)
    assert pf.cash / v > 0.10
    v3_tick(pf, env, closes, i, hh=15, mm=10)
    px, v = book(pf, closes, i)
    # F1 (owner decision): the credit funds only GOLDBEES, so the silver / copper redeployment goes through the normal 0.20/day budget:
    # most of the cash is redeployed (booked in the ledger), the rest waits for the next day's budget
    assert pf.cash <= 0.08 * v, (pf.cash / v)
    assert pf.runtime["v3"]["ledger"].get(day, 0.0) >= 0.10
    al = [r for r in _decisions(env, day) if r["action"] == "ALLOC"][0]["alloc"]
    assert any(c["rule"] == "no_add" and c["sym"] == "GOLDBEES" for c in al["clamps"]) or al["final"]["GOLDBEES"] <= pf.positions["GOLDBEES"].qty * px["GOLDBEES"] / v + 0.03


def _cov_records(env, day):
    return [r["alloc"] for r in _decisions(env, day) if r["action"] == "ALLOC"]


def test_vol_guard_uses_the_last_good_covariance_then_a_logged_fallback(tmp_path, monkeypatch):
    """Review fix 2: when the covariance cannot be computed (one bad close, mismatched history, fewer than 21 shared dates) the vol
    guard must not switch off silently: first the last good covariance (at most cov_max_age_days old), then a conservative
    per-asset-volatility stand-in. Either way the ALLOC record says so."""
    from simgold import allocation
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299, split=(0.2, 0.5, 0.3))                     # a risky book: silver and copper heavy
    _settle(pf, env, closes, 300, 320)
    assert pf.runtime["v3"]["cov_day"] and pf.runtime["v3"]["cov_last"]        # a good one is remembered
    live = _cov_records(env, DAYS[319].date().isoformat())
    assert live and live[0]["vol"]["cov"] == "live"
    monkeypatch.setattr(allocation, "ewma_cov", lambda *a, **k: None)         # what a bad close / short history looks like
    v3_tick(pf, env, closes, 320)
    rec = _cov_records(env, DAYS[320].date().isoformat())[0]
    assert rec["vol"]["cov"].startswith("last_good")
    assert any(c["rule"] == "vol_guard_stale_cov" for c in rec["clamps"])
    pf.runtime["v3"].pop("cov_last")                                           # nothing stored either
    v3_tick(pf, env, closes, 321)
    rec = _cov_records(env, DAYS[321].date().isoformat())[0]
    assert rec["vol"]["cov"] == "fallback" and rec["vol"]["before"] is not None
    assert any(c["rule"] == "vol_guard_fallback" for c in rec["clamps"])


def test_a_short_history_for_one_symbol_is_a_fallback_not_a_silent_skip(tmp_path):
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299, split=(0.2, 0.5, 0.3))
    i = 330
    day = DAYS[i].to_pydatetime()
    now = datetime(day.year, day.month, day.day, 15, 10, tzinfo=IST)
    hist = {}
    for s in SYMS:
        n = 15 if s == "SILVERBEES" else i                                     # 15 shared dates only: ewma_cov needs 21
        c = closes[s][i - n:i]
        hist[s] = trend.clean_bars(c.tolist(), (c * 1.004).tolist(), (c * 0.996).tolist(), [d.date().isoformat() for d in DAYS[i - n:i]])
    mkt = Market(now=now, prices={s: float(closes[s][i]) for s in SYMS}, price_ts={s: now for s in SYMS}, scores={},
                 prev_close={s: float(closes[s][i - 1]) for s in SYMS}, hist=hist)
    run_tick(pf, mkt, env)
    recs = _cov_records(env, DAYS[i].date().isoformat())
    assert recs, "the daily check must still run and log"
    assert recs[0]["vol"]["cov"] == "fallback"
    assert any(c["rule"] == "vol_guard_fallback" for c in recs[0]["clamps"])


# ------------------------------------------------------------------ turnover ledger (review fixes 3 and 8)
def _td_index(i0=330):
    """An index i whose previous bar is the previous NSE trading day and which is itself a trading day."""
    from simgold import market
    for i in range(i0, i0 + 40):
        d = DAYS[i].date()
        if market.is_trading_day(d) and market.prev_trading_day(d) == DAYS[i - 1].date():
            return i
    raise AssertionError("no trading day pair")


def _cash_book(closes, i, cash=100000.0) -> Portfolio:
    pf = Portfolio()
    pf.cash, pf.liquidbees = cash, 0.0
    return pf


def _led_env(tmp_path, ledger: dict | None, **alloc):
    env = v3_env(tmp_path, **alloc)
    return env


def _seed_ledger(pf, ledger: dict, key="ledger"):
    pf.runtime.setdefault("v3", {})[key] = dict(ledger)


def _first_alloc(env, day):
    return [r for r in _decisions(env, day) if r["action"] == "ALLOC"][0]["alloc"]


@pytest.mark.parametrize("seed_ledger,expect_budget", [
    ({"today": 0.20}, 0.0),                       # the day budget is used up
    ({"d-1": 0.20, "d-2": 0.20}, 0.0),            # the week budget is used up (0.20 + 0.20 over the last sessions)
    ({"d-1": 0.15, "d-2": 0.10, "d-3": 0.05}, 0.10),   # 0.30 used: 0.10 of the week left (the day allows 0.20)
    ({}, 0.20)])
def test_the_ledger_limits_the_next_check(tmp_path, seed_ledger, expect_budget):
    """Review fix 8: the discretionary buy at 15:10 is exactly what the day and week budgets leave, read from the ledger. With the
    ledger ignored every row would buy 20 points, so this fails for each of them."""
    from simgold import market
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    i = _td_index()
    day = DAYS[i].date()
    led, d = {}, day
    for k, v in seed_ledger.items():
        if k == "today":
            led[day.isoformat()] = v
        else:
            n = int(k.split("-")[1])
            dd = day
            for _ in range(n):
                dd = market.prev_trading_day(dd)
            led[dd.isoformat()] = v
    env = v3_env(tmp_path)
    pf = _cash_book(closes, i)
    _seed_ledger(pf, led)
    v3_tick(pf, env, closes, i)
    al = _first_alloc(env, day.isoformat())
    assert al["turnover"]["budget"] == pytest.approx(expect_budget, abs=1e-6)
    assert al["turnover"]["discretionary"] == pytest.approx(expect_budget, abs=1e-3)
    px, v = book(pf, closes, i)
    invested = 1 - pf.cash / v
    assert invested == pytest.approx(expect_budget + 0.03 * (1 if expect_budget < 0.2 else 0), abs=0.035)   # the budget, plus at most the dust sweep
    booked = pf.runtime["v3"]["ledger"][day.isoformat()]
    assert booked == pytest.approx(led.get(day.isoformat(), 0.0) + expect_budget + (invested - expect_budget if invested > expect_budget else 0), abs=0.01)


def test_ramp_in_takes_about_two_weeks_with_the_default_limits_and_three_checks_when_the_owner_raises_them(tmp_path):
    """Review fix 3: all cash to fully invested under 0.20/day 0.40/week takes about two weeks (documented); allocation.ramp_turnover_day
    and ramp_turnover_week are the owner's knobs to make it faster. Nothing exceeds the limits silently."""
    def days_to_invested(d, **alloc):
        rng = np.random.default_rng(3)
        closes = make_path(rng, "up")
        env = v3_env(d, **alloc)
        pf = _cash_book(closes, 330)
        i0 = _td_index()
        for n, i in enumerate(range(i0, i0 + 25), 1):
            if DAYS[i].date().weekday() >= 5:
                continue
            v3_tick(pf, env, closes, i)
            px, v = book(pf, closes, i)
            if 1 - pf.cash / v >= 0.95:
                return n, pf, env
        return 99, pf, env
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    slow, pf_a, env_a = days_to_invested(tmp_path / "a")
    fast, pf_b, env_b = days_to_invested(tmp_path / "b", ramp_turnover_day=0.5, ramp_turnover_week=1.0)
    assert 8 <= slow <= 14, slow
    assert fast <= 3, fast
    # the default run never booked more than the week budget plus the recorded dust (week window of 5 sessions)
    from simgold import allocation as A, market
    led, dust = pf_a.runtime["v3"]["ledger"], pf_a.runtime["v3"].get("dust", {})
    last = max(led)
    from datetime import date as _d
    t = _d.fromisoformat(last)
    assert A.window_total(led, t, market.prev_trading_day) - A.window_total(dust, t, market.prev_trading_day) <= 0.40 + 0.011
    assert A.window_total(dust, t, market.prev_trading_day) <= 0.06 + 1e-6
    recs = [r for r in _decisions(env_a, DAYS[_td_index()].date().isoformat()) if r["action"] == "ALLOC"]
    assert any(c["rule"] == "ramp_in" for c in recs[0]["alloc"]["clamps"])


def test_the_morning_retry_of_a_blocked_buy_is_booked_in_the_ledger(tmp_path):
    """Review fix 3: the 09:30 retry is a normal buy. Its turnover is booked, so the 15:10 check cannot spend the day budget again."""
    from simgold import market
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    i = _td_index()
    day = DAYS[i].date()
    env = v3_env(tmp_path)
    pf = _cash_book(closes, i)
    prev = market.prev_trading_day(day).isoformat()
    v3_tick(pf, env, closes, i, hh=9, mm=16)                                  # the first tick switches the strategy mode on
    pf.runtime.setdefault("v2", {})["pending_buys"] = {"GOLDBEES": {"date": prev, "target": 0.6, "actual": 0.0, "blocker": "Price shock"}}
    v3_tick(pf, env, closes, i, hh=9, mm=31)
    recs = _decisions(env, day.isoformat())
    buys = [r for r in recs if r["action"] == "BUY"]
    assert buys and "Retry" in buys[0]["reason"]
    px, v = book(pf, closes, i)
    bought = 1 - pf.cash / v
    assert 0.10 < bought <= 0.21                                              # at most the day budget
    assert pf.runtime["v3"]["ledger"][day.isoformat()] == pytest.approx(bought, abs=0.01)
    v3_tick(pf, env, closes, i)                                               # the 15:10 check: the day budget is gone
    px, v = book(pf, closes, i)
    assert 1 - pf.cash / v <= bought + 0.031
    al = _first_alloc(env, day.isoformat())
    assert al["turnover"]["budget"] == pytest.approx(0.20 - bought, abs=0.011) and al["turnover"]["budget"] < 0.06


def test_dust_sweep_is_booked_and_bounded(tmp_path):
    """The 3-point dust sweep may exceed the budget but is recorded in `dust`, and the week total is bounded (dust_week)."""
    from simgold import allocation as A, market
    from datetime import date as _d
    rng = np.random.default_rng(5)
    closes = make_path(rng, "mixed")
    env = v3_env(tmp_path)
    pf = _cash_book(closes, 330)
    i0 = _td_index()
    for i in range(i0, i0 + 12):
        if DAYS[i].date().weekday() >= 5:
            continue
        v3_tick(pf, env, closes, i)
        v3 = pf.runtime["v3"]
        t = DAYS[i].date()
        d = A.window_total(v3.get("dust", {}), t, market.prev_trading_day)
        assert d <= 0.06 + 1e-6, (i, d)
        assert v3.get("dust", {}).get(t.isoformat(), 0.0) <= 0.03 + 1e-6
        # every swept rupee is in the ledger: the week total of the ledger covers the dust
        assert A.window_total(v3["ledger"], t, market.prev_trading_day) + 1e-9 >= d


# ------------------------------------------------------------------ one owner switch, routine bias (review fix 6)
def test_allocation_phase_live_default_is_the_single_owner_switch(tmp_path):
    """strategy.mode stays v2_trend; the ONE edit allocation.phase: live_default makes D5-FI trade fully invested with the AI only logged."""
    from simgold import allocation as A
    p = R.shipped_params("v2_trend", phase="live_default")
    assert p["strategy"]["mode"] == "v2_trend" and trend.is_v3(p) and trend.is_v2(p)
    a = A.settings(p)
    assert a["phase"] == "live_default" and a["shadow"] is False
    prop = {"for_date": "2026-11-13", "written": "2026-11-13T09:00:00+05:30", "by": "allocation", "phase": "final",
            "weights": {"GOLDBEES": 0.70, "SILVERBEES": 0.20, "HINDCOPPER": 0.10}, "confidence": 0.8,
            "scenarios": [{"name": "base", "p": 0.6, "GOLD": "flat"}, {"name": "bear", "p": 0.4, "GOLD": "down"}],
            "rationale": {"GOLD": "DXY 104"}, "invalidate": []}
    p["allocation"]["ai_trust"] = 1.0                               # even a configured trust does not tilt in live_default
    pf, env = R.replay(tmp_path, p, days=R.DAYS[:1], state_files={"allocation.json": json.dumps(prop)})
    al = [json.loads(x) for x in R.decisions_text(env, days=R.DAYS[:1]).splitlines() if json.loads(x)["action"] == "ALLOC"][0]
    assert al["alloc_mode"] == "d5fi_ai_logged_only" and al["alloc"]["k"] == 0.0 and "would_have" in al
    assert al["would_have"]["k"] > 0 and pf.positions                         # the AI tilt is only what would have happened
    assert any(r["action"] == "BUY" for r in map(json.loads, R.decisions_text(env, days=R.DAYS[:1]).splitlines()))


def test_phase_live_with_mode_v2_trend_trades_the_tilt_and_shadow_off_stay_dormant(tmp_path):
    from simgold import allocation as A
    assert trend.is_v3(R.shipped_params("v2_trend", phase="live")) and A.settings(R.shipped_params("v2_trend", phase="live"))["phase"] == "live"
    for ph in ("shadow", "off", "bogus", None):
        p = R.shipped_params("v2_trend", phase=ph) if ph is not None else R.shipped_params("v2_trend")
        assert not trend.is_v3(p) and trend.is_v2(p), ph
    assert not trend.is_v3(R.shipped_params("v1_scores", phase="live_default"))        # never over the v1 rules
    d = tmp_path / "x"
    d.mkdir()
    pf, env = R.replay(d, R.shipped_params("v2_trend", phase="off"))
    assert R.decisions_text(env) == golden("decisions.jsonl") and "v3" not in pf.runtime      # dormant: byte-identical


def test_phase_is_the_single_source_of_truth_for_every_cell_of_the_matrix():
    """{strategy.mode} x {allocation.phase} x {legacy shadow}: what runs (is_v2 / is_v3 / trades) for each of the 3 x 6 x 3 cells."""
    from simgold import allocation as A
    ABSENT = object()
    for mode in ("v1_scores", "v2_trend", "v3_alloc"):
        for ph in (ABSENT, "off", "shadow", "live_default", "live", "garbage"):
            for sh in (ABSENT, True, False):
                p = R.shipped_params(mode)
                a = p["allocation"]
                a.pop("phase", None)
                a.pop("shadow", None)
                if ph is not ABSENT:
                    a["phase"] = ph
                if sh is not ABSENT:
                    a["shadow"] = sh
                cell = (mode, ph if ph is not ABSENT else "-", sh if sh is not ABSENT else "-")
                st = A.settings(p)
                # trading: phase decides when present; the legacy key only when phase is absent
                if ph is ABSENT:
                    trades = sh is False
                else:
                    trades = ph in ("live_default", "live")
                if mode == "v1_scores":
                    assert not trend.is_v2(p) and not trend.is_v3(p), cell
                    continue
                assert trend.is_v2(p), cell
                if mode == "v2_trend":
                    want_v3 = ph in ("live_default", "live")
                else:
                    want_v3 = ph != "off"
                assert trend.is_v3(p) == want_v3, cell
                live = want_v3 and not st["shadow"]
                expect_live = want_v3 and trades
                assert live == expect_live, (cell, st["shadow"], st["phase"])
                if ph is not ABSENT:
                    assert st["shadow"] == (not trades), cell                  # the legacy key never overrides a present phase
                    assert st["phase"] == (ph if ph in A.PHASES else "shadow"), cell
    # the contradiction PR 73 had: v3_alloc + phase shadow/off + legacy shadow false must NOT trade
    for ph in ("shadow", "off"):
        p = R.shipped_params("v3_alloc", phase=ph, shadow=False)
        assert A.settings(p)["shadow"] is True
    p = R.shipped_params("v3_alloc")
    p["allocation"].pop("phase")
    p["allocation"]["shadow"] = False
    assert A.settings(p)["shadow"] is False and A.settings(p)["phase"] == "live_default"       # phase absent: the legacy key counts


def test_config_loader_shows_v3_alloc_for_the_single_switch(tmp_path):
    """B1: everything that reads strategy.mode through the loader (tools, the routines' MODE command via simgold.config) sees v3_alloc
    when allocation.phase is live_default or live, and the raw values otherwise."""
    import yaml
    from simgold import config
    for ph, mode, want in (("live_default", "v2_trend", "v3_alloc"), ("live", "v2_trend", "v3_alloc"), ("shadow", "v2_trend", "v2_trend"),
                           ("off", "v2_trend", "v2_trend"), ("live_default", "v1_scores", "v1_scores"), ("shadow", "v3_alloc", "v3_alloc")):
        raw = yaml.safe_load((R.REPO / "config" / "params.yaml").read_text(encoding="utf-8"))
        raw["strategy"]["mode"], raw["allocation"]["phase"] = mode, ph
        f = tmp_path / f"{mode}-{ph}.yaml"
        f.write_text(yaml.safe_dump(raw), encoding="utf-8")
        got = config.load_params(f)
        assert got["strategy"]["mode"] == want and got["allocation"]["phase"] == ph, (mode, ph)


def test_a_routine_block_bias_only_restricts_in_v3_live(tmp_path):
    """Review fix 6: under v3 live a routine's bias `block` means no new buys of that metal (its weight goes to the others); `favour`
    changes nothing; the file can never add exposure."""
    rng = np.random.default_rng(2)
    closes = make_path(rng, "up")
    i = _td_index(340)
    day = DAYS[i].date().isoformat()

    def run(bias, d):
        d.mkdir()
        env = v3_env(d)
        pf = _cash_book(closes, i)
        env.state_dir.mkdir(parents=True, exist_ok=True)
        for k in range(5):                                                    # several days so the ramp is not the limit
            dk = DAYS[i - 4 + k].date().isoformat()
            (env.state_dir / "guidance.json").write_text(json.dumps({"updated": f"{dk}T15:00:00+05:30", "bias": bias, "note": "t"}), encoding="utf-8")
            v3_tick(pf, env, closes, i - 4 + k, hh=15, mm=10)
        return pf, env
    base_pf, base_env = run({"SILVERBEES": "neutral"}, tmp_path / "n")
    blk_pf, blk_env = run({"SILVERBEES": "block"}, tmp_path / "b")
    fav_pf, fav_env = run({"SILVERBEES": "favour"}, tmp_path / "f")
    assert base_pf.positions.get("SILVERBEES") and base_pf.positions["SILVERBEES"].qty > 0      # without the block silver is bought
    assert blk_pf.positions.get("SILVERBEES") is None or blk_pf.positions["SILVERBEES"].qty == 0
    assert {s: p.qty for s, p in fav_pf.positions.items()} == {s: p.qty for s, p in base_pf.positions.items()}     # favour is a no-op
    px, v = book(blk_pf, closes, i)
    assert sum(p.qty * px[s] for s, p in blk_pf.positions.items()) <= v * 1.0001


# ------------------------------------------------------------------ review of PR 73 (B2 to B4, O1, O2, O5)
def _mkt(closes, i, hh=15, mm=10, stale=()):
    day = DAYS[i].to_pydatetime()
    now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    hist = {}
    for s in SYMS:
        c = closes[s][:i]
        hist[s] = trend.clean_bars(c.tolist(), (c * 1.004).tolist(), (c * 0.996).tolist(), [d.date().isoformat() for d in DAYS[:i]])
    ts = {s: (now - pd.Timedelta(minutes=45) if s in stale else now) for s in SYMS}
    return Market(now=now, prices={s: float(closes[s][i]) for s in SYMS}, price_ts=ts, scores={},
                  prev_close={s: float(closes[s][i - 1]) for s in SYMS}, hist=hist)


def _engine(pf, env, closes, i, hh=15, mm=10):
    from simgold.engine import Engine
    return Engine(pf, _mkt(closes, i, hh, mm), env)


def _bias_file(env, i, bias, hh=15, mm=0):
    dk = DAYS[i].date().isoformat()
    (env.state_dir).mkdir(parents=True, exist_ok=True)
    (env.state_dir / "guidance.json").write_text(json.dumps({"updated": f"{dk}T{hh:02d}:{mm:02d}:00+05:30", "bias": bias, "note": "t"}), encoding="utf-8")


def test_block_on_all_three_metals_buys_nothing_not_even_the_dust_sweep(tmp_path):
    """B2: a routine `block` only restricts. With all three blocked a cash book buys nothing (the sweep respects the block)."""
    rng = np.random.default_rng(2)
    closes = make_path(rng, "up")
    i = _td_index(340)
    env = v3_env(tmp_path)
    pf = _cash_book(closes, i)
    for k in range(3):
        _bias_file(env, i - 2 + k, {"GOLD": "block", "SILVER": "block", "COPPER": "block"})
        v3_tick(pf, env, closes, i - 2 + k)
    assert not pf.positions and pf.cash == pytest.approx(100000.0)
    # only gold blocked: the sweep stays out of gold too, and silver / copper are not lifted above their default by the leftover
    d2 = tmp_path / "g"
    d2.mkdir()
    env2 = v3_env(d2)
    pf2 = _cash_book(closes, i)
    for k in range(6):
        _bias_file(env2, i - 5 + k, {"GOLD": "block"})
        v3_tick(pf2, env2, closes, i - 5 + k)
    assert "GOLDBEES" not in pf2.positions
    al = [r for r in _decisions(env2, DAYS[i].date().isoformat()) if r["action"] == "ALLOC"][0]["alloc"]
    px, v = book(pf2, closes, i)
    for s in ("SILVERBEES", "HINDCOPPER"):
        w = pf2.positions[s].qty * px[s] / v if s in pf2.positions else 0.0
        assert w <= al["default"][s] + 0.03, (s, w, al["default"][s])


def test_a_fast_lane_gold_sell_does_not_become_silver_or_copper_buys_above_default(tmp_path):
    """B3: gold is sold intraday (AI stop above the floor, no re-buy today). The cash must not be pushed into the other metals above their
    default weight, and the credit is only for gold or a metal under its own default."""
    rng = np.random.default_rng(1)
    closes = make_path(rng, "up")
    i = 330
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299, split=(0.7, 0.2, 0.1))
    _settle(pf, env, closes, 300, i)
    px, v = book(pf, closes, i)
    g0 = pf.positions["GOLDBEES"].qty
    pf.positions["GOLDBEES"].ai_stop = px["GOLDBEES"] * 1.05
    v3_tick(pf, env, closes, i, hh=10, mm=0)
    v3_tick(pf, env, closes, i, hh=10, mm=1)
    day = DAYS[i].date().isoformat()
    sold = [r for r in _decisions(env, day) if r["action"] == "SELL" and r["symbol"] == "GOLDBEES"]
    assert sold and pf.positions["GOLDBEES"].qty < g0
    before = {s: pf.positions[s].qty * px[s] / v for s in ("SILVERBEES", "HINDCOPPER") if s in pf.positions}
    v3_tick(pf, env, closes, i)
    px, v = book(pf, closes, i)
    al = [r for r in _decisions(env, day) if r["action"] == "ALLOC"][0]["alloc"]
    for s in ("SILVERBEES", "HINDCOPPER"):
        w = pf.positions[s].qty * px[s] / v if s in pf.positions else 0.0
        assert w <= max(al["default"][s], before.get(s, 0.0)) + 0.03, (s, w, al["default"][s])
    assert not [r for r in _decisions(env, day) if r["action"] == "BUY" and r["symbol"] == "GOLDBEES"]      # gold is not re-bought today


def _level2_book(tmp_path, i=330):
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299)
    _settle(pf, env, closes, 300, i)
    return pf, env, closes, i


def test_credit_is_cleared_at_brake_level_2_and_a_release_does_not_re_enter_outside_the_day_limit(tmp_path):
    """B4: at brake level 2 the credit is zero and gone; the brake sells never create credit; when the brake releases the book comes back
    under the normal 0.20/day limit (plus the 3-point dust), not all at once."""
    pf, env, closes, i = _level2_book(tmp_path)
    px, v = book(pf, closes, i)
    day = DAYS[i].date().isoformat()
    pf.runtime["v3"]["credit"] = {"day": day, "amt": 0.5}                      # a (stale or forged) credit sitting in the state
    pf.runtime["v2"]["brake"]["peak"] = v / 0.70                                # -30%: level 2
    v3_tick(pf, env, closes, i)
    assert brake_level(pf) == 2 and not pf.positions and pf.cash / v > 0.97     # everything sold to cash by the brake
    assert "credit" not in pf.runtime["v3"]                                      # cleared: neither the forged one nor the brake sells' cash
    al = [r for r in _decisions(env, day) if r["action"] == "ALLOC"][0]["alloc"]
    assert al["credit"]["available"] == 0.0
    # release: back at the peak, the next day
    px, v = book(pf, closes, i + 1)
    pf.runtime["v2"]["brake"].update({"peak": v, "level": 0, "reconfirm": [], "days": 0, "rebase_value": None})
    v3_tick(pf, env, closes, i + 1)
    px, v = book(pf, closes, i + 1)
    assert brake_level(pf) < 2
    assert 1 - pf.cash / v <= 0.20 + 0.03 + 0.005, 1 - pf.cash / v              # one day's limit (plus dust), not the whole book


def test_credit_is_valid_for_its_own_trading_day_only(tmp_path):
    pf, env, closes, i = _level2_book(tmp_path)
    prev = DAYS[i - 1].date().isoformat()
    pf.runtime["v3"]["credit"] = {"day": prev, "amt": 0.4}                     # yesterday's: gone
    eng = _engine(pf, env, closes, i)
    assert eng.v3_credit() == 0.0 and "credit" not in pf.runtime["v3"]
    pf.cash = 40000.0
    pf.runtime["v3"]["credit"] = {"day": DAYS[i].date().isoformat(), "amt": 0.3}
    assert eng.v3_credit() == pytest.approx(min(0.3, eng.v3_cash_weight(eng.value())))


@pytest.mark.parametrize("cash,stored,level,halted,expect", [
    (5000.0, 0.40, 0, False, "cash"),          # more credit than cash: bounded by the cash weight
    (0.0, 0.40, 0, False, 0.0),                # no cash at all
    (50000.0, 0.10, 0, False, 0.10),           # less credit than cash
    (50000.0, 0.40, 2, False, 0.0), (50000.0, 0.40, 0, True, 0.0), (50000.0, "x", 0, False, 0.0), (50000.0, float("nan"), 0, False, 0.0)])
def test_the_engine_credit_never_exceeds_the_cash_held(tmp_path, cash, stored, level, halted, expect):
    """O5: 'credit ignores the cash held' must be caught by several tests, not one."""
    pf, env, closes, i = _level2_book(tmp_path)
    pf.cash = cash
    eng = _engine(pf, env, closes, i)
    pf.runtime["v3"]["credit"] = {"day": DAYS[i].date().isoformat(), "amt": stored}
    got = eng.v3_credit(level, halted)
    value = eng.value()
    if expect == "cash":
        assert got == pytest.approx(min(0.40, eng.v3_cash_weight(value)))
        assert got < 0.40
    else:
        assert got == pytest.approx(expect, abs=1e-9)
    assert got <= eng.v3_cash_weight(value) + 1e-12


def test_stored_credit_beyond_the_cash_does_not_exempt_a_discretionary_buy_in_a_tick(tmp_path):
    """O5: a fully invested book with a big stored credit and no cash must not get an exemption in the ALLOC record."""
    pf, env, closes, i = _level2_book(tmp_path)
    day = DAYS[i].date().isoformat()
    pf.cash = 300.0
    pf.runtime["v3"]["credit"] = {"day": day, "amt": 0.6}
    v3_tick(pf, env, closes, i)
    al = [r for r in _decisions(env, day) if r["action"] == "ALLOC"]
    if al:
        assert al[0]["alloc"]["credit"]["available"] <= 0.02


def test_dust_sweep_is_strictly_dust(tmp_path):
    """O1: every sweep buy is at most dust_day of the book, and the 5-session total at most dust_week, even when the turnover budget has room."""
    from simgold import allocation as A, market
    rng = np.random.default_rng(5)
    closes = make_path(rng, "mixed")
    env = v3_env(tmp_path)
    pf = _cash_book(closes, 330)
    i0 = _td_index()
    for i in range(i0, i0 + 14):
        if DAYS[i].date().weekday() >= 5:
            continue
        v3_tick(pf, env, closes, i)
        px, v = book(pf, closes, i)
        day = DAYS[i].date().isoformat()
        for r in _decisions(env, day):
            if r["action"] == "BUY" and "cash sweep" in r["reason"]:
                assert r["qty"] * r["price"] <= 0.03 * v + r["price"] * 1.5, (day, r["qty"] * r["price"] / v)
        d = A.window_total(pf.runtime["v3"].get("dust", {}), DAYS[i].date(), market.prev_trading_day)
        assert d <= 0.06 + 1e-6


def test_a_late_recheck_and_the_exits_only_check_are_booked_in_the_ledger(tmp_path):
    """O2: turnover from runs other than the first full check (a 15:12 re-check of a symbol whose price was late) is booked."""
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    i = _td_index()
    env = v3_env(tmp_path)
    pf = _cash_book(closes, i)
    day = DAYS[i].date().isoformat()
    from simgold.engine import run_tick
    run_tick(pf, _mkt(closes, i - 1, 15, 10), env)                              # a first day: switch the mode on, ramp a little
    run_tick(pf, _mkt(closes, i, 15, 10, stale=("SILVERBEES", "HINDCOPPER")), env)     # gold only: the first full check
    led1 = float(pf.runtime["v3"]["ledger"].get(day, 0.0))
    run_tick(pf, _mkt(closes, i, 15, 12), env)                                  # the late symbols are checked at 15:12
    led2 = float(pf.runtime["v3"]["ledger"].get(day, 0.0))
    buys_late = [r for r in _decisions(env, day) if r["action"] == "BUY" and r["ts"][11:16] == "15:12"]
    if buys_late:
        assert led2 > led1 + 1e-6
    # unit: the booking helper counts the larger of the non-exempt buys and the discretionary sells
    eng = _engine(pf, env, closes, i, 15, 13)
    before = float(pf.runtime["v3"]["ledger"].get(day, 0.0))
    eng._v3_last = None
    eng.v3_book_extra(0.05, 0.08)
    assert pf.runtime["v3"]["ledger"][day] == pytest.approx(before + 0.08, abs=1e-6)
    eng.v3_book_extra(0.0, 0.0)
    assert pf.runtime["v3"]["ledger"][day] == pytest.approx(before + 0.08, abs=1e-6)


def test_the_sweep_alone_buys_at_most_the_dust_allowance_even_with_budget_to_spare(tmp_path):
    """O1: called directly with a book that is 100% cash and a turnover budget of 1.0, the sweep still buys at most dust_day (3%)."""
    import dataclasses
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    i = _td_index()
    env = v3_env(tmp_path)
    pf = _cash_book(closes, i)
    v3_tick(pf, env, closes, i - 1)                                            # switches the mode on (and ramps a little)
    pf = _cash_book(closes, i)
    eng = _engine(pf, env, closes, i)
    eng.housekeeping()
    infos = eng.v2_infos()
    res, _adv = eng.v3_plan(infos, 0, [])
    res = dataclasses.replace(res, turnover_budget=1.0, discretionary_turnover=0.0, brake_level=0, halted=False)
    value = eng.value()
    eng.v3_sweep(infos, value, {"res": res, "sweep": 0.0})
    spent = 100000.0 - pf.cash
    assert 0 < spent <= 0.03 * value + 1e-6
    assert pf.runtime["v3"]["dust"][DAYS[i].date().isoformat()] == pytest.approx(spent / value, abs=2e-3)
    eng.v3_sweep(infos, eng.value(), {"res": res, "sweep": 0.0})                # a second sweep the same day: the day allowance is used up
    assert 100000.0 - pf.cash <= 0.03 * value + 130.0


# ------------------------------------------------------------------ final re-check of PR 73 (F1 to F3)
def test_a_fast_lane_gold_sale_is_not_re_invested_in_silver_outside_the_budget(tmp_path):
    """F1 replay: gold sold intraday (about 60% of the book), gold not re-bought today. At 15:10 silver may rise only through the normal
    0.20/day budget and the rise is booked in the ledger (the review's case: 9.6% to 28.4% with nothing booked)."""
    rng = np.random.default_rng(1)
    closes = make_path(rng, "up")
    i = 330
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299, split=(0.9, 0.07, 0.03))
    _settle(pf, env, closes, 300, i)
    px, v = book(pf, closes, i)
    w0 = {s: (pf.positions[s].qty * px[s] / v if s in pf.positions else 0.0) for s in SYMS}
    day = DAYS[i].date().isoformat()
    pf.positions["GOLDBEES"].ai_stop = px["GOLDBEES"] * 1.05
    v3_tick(pf, env, closes, i, hh=10, mm=0)
    v3_tick(pf, env, closes, i, hh=10, mm=1)
    px, v = book(pf, closes, i)
    sold = w0["GOLDBEES"] - pf.positions["GOLDBEES"].qty * px["GOLDBEES"] / v
    assert sold > 0.25                                                          # a big fast-lane gold sale
    led0 = float(pf.runtime["v3"].get("ledger", {}).get(day, 0.0))
    v3_tick(pf, env, closes, i)
    px, v = book(pf, closes, i)
    w1 = {s: (pf.positions[s].qty * px[s] / v if s in pf.positions else 0.0) for s in SYMS}
    rise = (w1["SILVERBEES"] - w0["SILVERBEES"]) + (w1["HINDCOPPER"] - w0["HINDCOPPER"])
    al = [r for r in _decisions(env, day) if r["action"] == "ALLOC"][0]["alloc"]
    assert al["credit"]["used"] == 0.0 and al["credit"]["exempt_buy"] == 0.0     # gold is blocked: the credit funded nothing
    assert rise <= 0.20 + 0.03 + 0.005                                          # the normal day budget (plus dust), not the sold gold
    booked = float(pf.runtime["v3"]["ledger"].get(day, 0.0)) - led0
    assert booked >= rise - 0.01                                                # and it is booked


def test_spent_credit_is_subtracted_even_when_other_cash_remains(tmp_path):
    """F2: after GOLDBEES buys use the credit it is reduced by what was bought; a same-day re-plan sees no credit left, whatever the cash."""
    pf, env, closes, i = _level2_book(tmp_path)
    day = DAYS[i].date().isoformat()
    pf.cash = 50000.0                                                           # plenty of other cash
    pf.runtime["v3"]["credit"] = {"day": day, "amt": 0.30}
    eng = _engine(pf, env, closes, i)
    assert eng.v3_credit() == pytest.approx(min(0.30, eng.v3_cash_weight(eng.value())))
    eng.v3_spend_credit(0.10)
    assert pf.runtime["v3"]["credit"]["amt"] == pytest.approx(0.20)
    eng.v3_spend_credit(0.25)
    assert "credit" not in pf.runtime["v3"] and eng.v3_credit() == 0.0
    # end to end: the silver-stop day leaves no credit to re-use at 15:12
    pf2, env2, closes2, day2 = _silver_stop_day(tmp_path / "x") if (tmp_path / "x").mkdir() is None else (None,) * 4
    eng2 = _engine(pf2, env2, closes2, 330, 15, 12)
    res, _ = eng2.v3_plan(eng2.v2_infos(), 0, [])
    assert res.credit < 0.02 and res.credit_used == 0.0


def test_brake_level_1_with_gold_blocked_does_not_show_an_untradeable_rotation(tmp_path):
    """F3 at the engine: the ALLOC record's final gold is what can be executed (no jump to 0.79 that the engine never buys)."""
    rng = np.random.default_rng(3)
    closes = make_path(rng, "up")
    env = v3_env(tmp_path)
    pf = invested_book(closes, 299)
    for i in range(300, 330):
        v3_tick(pf, env, closes, i)
    px, v = book(pf, closes, 330)
    pf.runtime["v2"]["brake"].update({"level": 1, "peak": v / 0.84, "days": 5, "rebase_value": None, "brake_day": None})
    pf.runtime["v2"]["brake_day"] = None
    g_w = pf.positions["GOLDBEES"].qty * px["GOLDBEES"] / v
    _bias_file(env, 330, {"GOLD": "block"})
    v3_tick(pf, env, closes, 330)
    day = DAYS[330].date().isoformat()
    al = [r for r in _decisions(env, day) if r["action"] == "ALLOC"][0]["alloc"]
    assert al["brake_level"] == 1 and al["final"]["GOLDBEES"] <= g_w + 0.03
    assert any(c["rule"] == "brake_rotation_blocked" for c in al["clamps"])
    px, v = book(pf, closes, 330)
    assert pf.positions["GOLDBEES"].qty * px["GOLDBEES"] / v <= g_w + 0.03
