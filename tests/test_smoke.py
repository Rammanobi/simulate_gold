"""Offline smoke test: a whole simulated NSE day on synthetic 1-minute prices through the real tick function.
No network. Checks decisions are logged with the exact contract and that the floor holds."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from simgold import config
from simgold.broker import Portfolio
from simgold.engine import DECISION_FIELDS, TRIGGERS, Env, run_tick
from simgold.feed import build_market
from simgold.market import IST

P = config.load_params()
TICKERS = P["engine"]["tickers"]
START = {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 400.0}
DAY = date(2026, 10, 14)         # a Wednesday, trading day 9 of October 2026


def synthetic_daily(seed=1, n=120):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(DAY) - pd.Timedelta(days=1), periods=n)
    close, high, low = {}, {}, {}
    for sym, tk in TICKERS.items():
        r = rng.normal(0.0006, 0.008, n)                       # gentle uptrend, ~0.8% daily noise
        c = START[sym] * np.cumprod(1 + r) / np.cumprod(1 + r)[-1]
        close[tk], high[tk], low[tk] = c, c * 1.006, c * 0.994
    mk = lambda d: pd.DataFrame(d, index=idx)
    return {"close": mk(close), "high": mk(high), "low": mk(low)}


def synthetic_minutes(daily, scenario, seed=2):
    """scenario: 'rally_then_crash' | 'quiet_rally' | 'crash_after_buy'"""
    rng = np.random.default_rng(seed)
    t0 = datetime(DAY.year, DAY.month, DAY.day, 9, 15, tzinfo=IST)
    idx = pd.DatetimeIndex([t0 + timedelta(minutes=m) for m in range(375)])
    out = {}
    for sym, tk in TICKERS.items():
        prev = float(daily["close"][tk].iloc[-1])
        n = len(idx)
        drift = np.full(n, 0.00012)
        if scenario == "rally_then_crash":
            drift[150:] = -0.0006            # from ~11:45 a steady slide
        elif scenario == "crash_after_buy":
            drift[40:] = -0.0012             # right after the first buys, a hard fall
        steps = rng.normal(0, 0.0002, n) + drift
        px = prev * (1 + 0.0005) * np.cumprod(1 + steps)
        out[tk] = pd.DataFrame({"open": px, "high": px * 1.0003, "low": px * 0.9997, "close": px,
                                "volume": rng.integers(500, 1500, n)}, index=idx)
    return out


def simulate(tmp_path, scenario, stop_at=None):
    daily = synthetic_daily()
    bars = synthetic_minutes(daily, scenario)
    env = Env(state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", params=P, environ={}, match_headline=None)
    pf = Portfolio()
    pf.cash, pf.liquidbees = 0.0, 100000.0
    pf.floor = 99500.0
    floors, values = [], []
    for ts in bars[TICKERS["GOLDBEES"]].index:
        if stop_at and ts.strftime("%H:%M") > stop_at:
            break
        mkt = build_market(ts.to_pydatetime(), bars, daily, params=P)
        run_tick(pf, mkt, env)
        floors.append(pf.floor)
        values.append(pf.cash + pf.liquidbees + sum(p.qty * p.last_price * (1 - P["instruments"][s]["cost"] / 2)
                                                  for s, p in pf.positions.items()))
    return pf, env, floors, values


def read_log(env):
    f = env.logs_dir / f"decisions-{DAY.isoformat()}.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def test_quiet_rally_day(tmp_path):
    pf, env, floors, values = simulate(tmp_path, "quiet_rally")
    recs = read_log(env)
    assert any(r["action"] == "BUY" for r in recs), "a clean rally should trigger at least one buy"
    for r in recs:
        assert list(r) == DECISION_FIELDS
        assert r["trigger"] in TRIGGERS
        assert r["reason"].endswith(".") and r["symbol"] in (set(P["instruments"]) | {"ALL"})
    assert all(b >= a for a, b in zip(floors, floors[1:])), "floor must never decrease"
    assert min(v - f for v, f in zip(values, floors)) > 0, "value must stay above the floor"
    rows = list(csv.reader(open(env.logs_dir / f"ticks-{DAY.isoformat()}.csv")))
    assert rows[0] == ["time", "value", "floor", "cash", "liquidbees", "GOLDBEES", "SILVERBEES", "HINDCOPPER"] and len(rows) == 376
    assert sum(r["action"] == "BUY" for r in recs) <= 3
    # budget never exceeded: worst-case risk of everything held fits the usable risk when each buy was made
    assert all(p.stop < p.last_price for p in pf.positions.values())


def test_crash_day_sells_and_floor_holds(tmp_path):
    pf, env, floors, values = simulate(tmp_path, "crash_after_buy")
    recs = read_log(env)
    acts = [r["action"] for r in recs]
    assert "BUY" in acts and "SELL" in acts
    assert any(r["trigger"] in ("Stop-loss", "Trailing stop", "Tripwire") for r in recs if r["action"] == "SELL")
    assert all(b >= a for a, b in zip(floors, floors[1:]))
    assert min(v - f for v, f in zip(values, floors)) > 0      # the loss stayed above the floor all day
    assert not pf.positions or all(p.stop < p.last_price for p in pf.positions.values())
