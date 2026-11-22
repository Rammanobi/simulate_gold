"""Replay of the stored minute ticks (logs/ticks-DATE.csv, 13, 16 and 17 Nov 2026) through the real engine tick.

Shared by tests/test_allocation_engine.py and by the one-off script that wrote the golden files in
tests/fixtures/v3_dormant/ (run on origin/main BEFORE the v3 change, so the golden is what the old engine did).
Only APIs that exist on the old engine are used here. The daily history is synthetic but fixed (seeded): the real part is the
minute prices. Nothing here touches the network or the repo's state/ and logs/ folders.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from simgold import config, trend
from simgold.broker import Portfolio
from simgold.engine import Env, Market, run_tick
from simgold.market import IST

SYMS = ["GOLDBEES", "SILVERBEES", "HINDCOPPER"]
DAYS = ["2026-11-13", "2026-11-16", "2026-11-17"]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "v3_dormant"
REPO = Path(__file__).resolve().parent.parent


def shipped_params(mode: str | None = None, **alloc) -> dict:
    """A fresh copy of config/params.yaml as shipped (not the test-suite's cached, v1-switched copy)."""
    with open(REPO / "config" / "params.yaml", encoding="utf-8") as fh:
        p = yaml.safe_load(fh)
    if mode:
        p["strategy"]["mode"] = mode
    if alloc:
        alloc = dict(alloc)
        if alloc.get("shadow") is False and "phase" not in alloc:          # the old two-key spelling of "v3 trades"
            alloc.pop("shadow")
            alloc["phase"] = "live_default"
        p.setdefault("allocation", {}).update(alloc)
    return p


def read_ticks(day: str) -> list[dict]:
    with open(REPO / "logs" / f"ticks-{day}.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def synthetic_history(first_prices: dict[str, float], n: int = 420, seed: int = 11):
    """n daily bars ending the weekday before DAYS[0], drifting up with noise, last close = the first real price."""
    idx = pd.bdate_range(end=pd.Timestamp(DAYS[0]) - pd.Timedelta(days=1), periods=n)
    out = {}
    for k, s in enumerate(SYMS):
        rng = np.random.default_rng(seed + k)
        r = 0.0008 + rng.normal(0, 0.006 + 0.003 * k, n)
        c = np.cumprod(1 + r)
        c = c / c[-1] * first_prices[s]
        out[s] = (c.tolist(), [d.date().isoformat() for d in idx])
    return out


def replay(tmp: Path, params: dict, days=DAYS, step_outside: int = 5, state_files: dict | None = None):
    """Run the ticks of `days` through run_tick. Returns (portfolio, env). Minutes between 14:50 and 15:30 all run; elsewhere every
    `step_outside`-th minute. The decisions land in tmp/logs/decisions-DAY.jsonl."""
    env = Env(state_dir=tmp / "state", logs_dir=tmp / "logs", params=params, environ={}, match_headline=None)
    env.state_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (state_files or {}).items():
        (env.state_dir / name).write_text(text, encoding="utf-8")
    pf = Portfolio()
    pf.cash, pf.liquidbees = 100000.0, 0.0
    first = {s: float(read_ticks(days[0])[0][s]) for s in SYMS}
    hist_raw = synthetic_history(first)
    closes = {s: list(hist_raw[s][0]) for s in SYMS}
    dates = {s: list(hist_raw[s][1]) for s in SYMS}
    for day in days:
        rows = read_ticks(day)
        hi = {s: [] for s in SYMS}
        for n, row in enumerate(rows):
            hh, mm = (int(x) for x in row["time"].split(":"))
            for s in SYMS:
                if row.get(s):
                    hi[s].append(float(row[s]))
            if not ((hh, mm) >= (14, 50) or n % step_outside == 0):
                continue
            d = date.fromisoformat(day)
            now = datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)
            prices = {s: float(row[s]) for s in SYMS if row.get(s)}
            bars = {s: trend.clean_bars(closes[s], [x * 1.004 for x in closes[s]], [x * 0.996 for x in closes[s]], dates[s])
                    for s in SYMS}
            mkt = Market(now=now, prices=prices, price_ts={s: now for s in prices}, scores={},
                         prev_close={s: closes[s][-1] for s in SYMS}, hist=bars)
            run_tick(pf, mkt, env)
        for s in SYMS:                                          # the day becomes a completed bar
            closes[s].append(hi[s][-1])
            dates[s].append(day)
    return pf, env


def decisions_text(env: Env, days=DAYS, drop_actions=()) -> str:
    out = []
    for day in days:
        f = env.logs_dir / f"decisions-{day}.jsonl"
        for line in (f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
            if drop_actions and json.loads(line).get("action") in drop_actions:
                continue
            out.append(line)
    return "\n".join(out) + "\n"


def runtime_text(pf: Portfolio, drop_keys=()) -> str:
    rt = {k: v for k, v in pf.runtime.items() if k not in drop_keys}
    return json.dumps({"runtime": rt, "cash": round(pf.cash, 4),
                       "positions": {s: [p.qty, round(p.avg_cost, 4)] for s, p in sorted(pf.positions.items())}},
                      sort_keys=True, indent=1, default=str) + "\n"
