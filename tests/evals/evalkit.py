"""Shared harness for the engine scenario evals (tests/evals). No network, no git, no clock: everything is injected.

`Sim` drives the real `simgold.engine.Engine` one minute at a time and, after EVERY tick, re-checks the money invariants
independently of the engine's own bookkeeping:

  * cash and LIQUIDBEES are never negative, units are whole numbers, no position has qty <= 0
  * value reconciliation: cash + sum(qty x price x (1 - cost/2)) == Engine.value()   (the price comes from the feed, not the engine)
  * the live P&L payload (simgold.livepnl.build) is valid schema v:1, JSON-safe, and its total equals Engine.value()
  * the money summary: booked + open - costs + interest == overall profit to the paisa, and `interest` (the plug) does not
    drift: with idle cash it must stay at the interest the account already held at the start (0 for a fresh account)

The shipped rules are loaded straight from config/params.yaml (not the cached copy that tests/conftest.py switches to v1).
"""
from __future__ import annotations

import copy
import csv
import json
import math
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import yaml

from simgold import livepnl, summary, trend
from simgold.broker import Portfolio, Position
from simgold.engine import Engine, Env, Market
from simgold.market import IST

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).resolve().parent / "fixtures"
SYMS = ["GOLDBEES", "SILVERBEES", "HINDCOPPER"]


def shipped_params() -> dict:
    """A fresh copy of config/params.yaml: the CURRENT rules (v2_trend, idle cash, D5-S stops, ...)."""
    with open(ROOT / "config" / "params.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def at(day: date | str, hh: int, mm: int) -> datetime:
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


def synth_bars(kind: str, start: float, n: int = 300) -> trend.Bars:
    """Synthetic completed daily bars. up: +40% over n days (trend ON), down: from +15% to -2% of start (trend OFF)."""
    rng = np.random.default_rng(int(start))
    a, b = (1.0, 1.4) if kind == "up" else (1.15, 0.98)
    c = start * np.linspace(a, b, n) * (1 + rng.normal(0, 0.002, n))
    return trend.clean_bars(c.tolist(), (c * 1.003).tolist(), (c * 0.997).tolist())


START_PX = {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 400.0}


def synth_hist(kinds: dict) -> dict:
    return {s: synth_bars(kinds.get(s, "down"), START_PX[s]) for s in SYMS}


def last_close(hist: dict, sym: str) -> float:
    return float(hist[sym].close[-1])


# ---------------------------------------------------------------- live payload validation
def validate_live_payload(d: dict, total: float) -> None:
    assert d["v"] == 1, "schema version must stay 1"
    for k in ("ts", "session_date", "market_open", "feed_ok", "start_capital", "prev_close_value", "total_value", "today", "open",
              "total", "prices", "positions", "liquidbees", "cash", "series", "summary"):
        assert k in d, f"live payload lost the key {k!r}"
    assert abs(d["total_value"] - total) < 0.006, (d["total_value"], total)
    for k in ("pnl", "pct", "parts"):
        assert k in d["today"]
    assert set(d["today"]["parts"]) == {"positions", "liquidbees", "realised", "costs", "other"}
    assert isinstance(d["positions"], list) and isinstance(d["series"], list)
    for p in d["positions"]:
        assert isinstance(p["qty"], int) and p["qty"] > 0 and p["symbol"] in SYMS
    for row in d["series"]:
        assert len(row) == 3 and isinstance(row[0], str) and len(row[0]) == 5
    assert [r[0] for r in d["series"]] == sorted(r[0] for r in d["series"])
    blob = json.dumps(d, allow_nan=False)           # NaN / inf would raise here
    assert json.loads(blob) == d
    assert livepnl.dumps(d).endswith(b"\n")


class Sim:
    """One portfolio, one set of state/log dirs, ticked minute by minute. `fails` stays empty when all is well."""

    def __init__(self, tmp: Path, params: dict | None = None, pf: Portfolio | None = None, hist=None, cash: float = 100000.0,
                 match_headline=None, check: bool = True, payload: bool = True, expected_interest: float | None = None):
        self.p = params or shipped_params()
        self.tmp = Path(tmp)
        self.env = Env(state_dir=self.tmp / "state", logs_dir=self.tmp / "logs", params=self.p, environ={}, match_headline=match_headline)
        self.env.state_dir.mkdir(parents=True, exist_ok=True)
        self.env.logs_dir.mkdir(parents=True, exist_ok=True)
        if pf is None:
            pf = Portfolio()
            pf.cash, pf.liquidbees = cash, 0.0
        self.pf = pf
        self.hist = hist                                   # dict sym -> Bars, or callable(date) -> dict
        self.check, self.payload = check, payload
        self.last_px: dict[str, float] = {}
        self.prev_close: dict[str, float] = {}
        self.series: dict[str, list] = {}
        self.live_series: list = []
        self.equity_rows: list[tuple[str, float]] = []
        self.day: date | None = None
        self.ticks = 0
        self.expected_interest = (pf.cash + pf.liquidbees + sum(x.qty * x.avg_cost + x.buy_costs for x in pf.positions.values()) - 100000.0
                                  if expected_interest is None else expected_interest)
        self.interest_seen: list[float] = []
        self.last_payload: dict | None = None

    # ---- helpers
    def value_now(self) -> float:
        v = self.pf.cash + self.pf.liquidbees
        for s, pos in self.pf.positions.items():
            v += pos.qty * (self.last_px.get(s) or pos.last_price or pos.avg_cost) * (1 - self.p["instruments"][s]["cost"] / 2)
        return v

    def hist_for(self, day: date):
        return self.hist(day) if callable(self.hist) else (self.hist or {})

    def decisions(self, day: date | str | None = None) -> list[dict]:
        out = []
        for f in sorted(self.env.logs_dir.glob("decisions-*.jsonl")):
            if day is None or f.name == f"decisions-{day if isinstance(day, str) else day.isoformat()}.jsonl":
                out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        return out

    def trades(self, action: str, sym: str | None = None, day=None) -> list[dict]:
        return [d for d in self.decisions(day) if d["action"] == action and (sym is None or d["symbol"] == sym)]

    def weights(self) -> dict:
        v = self.value_now()
        return {s: (self.pf.positions[s].qty * self.last_px.get(s, self.pf.positions[s].last_price) / v if s in self.pf.positions and v > 0 else 0.0)
                for s in SYMS}

    # ---- the tick
    def tick(self, now: datetime, prices: dict | None, ages: dict | None = None, headlines=None, hist=None, prev_close=None,
             use_series: bool = True) -> list[dict]:
        """prices None or {} = feed down (no price at all). ages {sym: minutes the price is old}."""
        if self.day != now.date():
            self.day = now.date()
            self.series = {}
            self.live_series = []
        prices = {s: float(v) for s, v in (prices or {}).items() if v is not None}
        ages = ages or {}
        if use_series:
            for s, px in prices.items():
                self.series.setdefault(s, []).append((now - timedelta(minutes=ages.get(s, 0)), px))
        pc = prev_close if prev_close is not None else (self.prev_close or dict(prices))
        mkt = Market(now=now, prices=prices, price_ts={s: now - timedelta(minutes=ages.get(s, 0)) for s in prices}, scores={},
                     series={s: list(v) for s, v in self.series.items()} if use_series else {}, prev_close=dict(pc),
                     headlines=headlines or [], hist=hist if hist is not None else self.hist_for(now.date()))
        eng = Engine(self.pf, mkt, self.env)
        before = len(eng.log)
        eng.run()
        self.ticks += 1
        for s, px in mkt.prices.items():                   # what the engine accepted (bad prints were dropped from mkt.prices)
            self.last_px[s] = px
        if self.check:
            self._invariants(eng, mkt, now)
        return eng.log[before:]

    def _invariants(self, eng: Engine, mkt: Market, now: datetime) -> None:
        pf = self.pf
        assert pf.cash >= -1e-6, f"cash went negative: {pf.cash}"
        assert pf.liquidbees >= -1e-6, f"LIQUIDBEES went negative: {pf.liquidbees}"
        for s, pos in pf.positions.items():
            assert isinstance(pos.qty, int) and not isinstance(pos.qty, bool), f"{s} units are not an integer: {pos.qty!r}"
            assert pos.qty > 0, f"{s} is held with {pos.qty} units"
            assert pos.avg_cost > 0
        for t in pf.trades:
            assert isinstance(t["qty"], int) and t["qty"] > 0, t
        expect = pf.cash + pf.liquidbees + sum(
            pos.qty * (self.last_px.get(s) or pos.avg_cost) * (1 - self.p["instruments"][s]["cost"] / 2) for s, pos in pf.positions.items())
        assert abs(expect - eng.value()) < 1e-6, f"value does not reconcile at {now:%H:%M}: independent {expect:.4f} vs engine {eng.value():.4f}"
        if not self.payload:
            return
        pfd = asdict(pf)
        prices = dict(mkt.prices)
        d = livepnl.build(pfd, prices, self.prev_close or prices, self.equity_rows, self.p, now, self.live_series,
                          feed_ok=bool(prices), market_open=True, price_ts=mkt.price_ts)
        self.live_series = livepnl.add_point(self.live_series, now.strftime("%H:%M"), livepnl.total_value(pfd, prices, self.p))
        validate_live_payload(d, eng.value())
        self.last_payload = d
        sm = d["summary"]
        bd = sm["breakdown"]
        assert abs(bd["booked"] + bd["open"] + bd["costs"] + bd["interest"] - sm["overall_profit"]) < 0.0051, ("summary parts do not sum", bd, sm["overall_profit"])
        assert abs(bd["sum"] - sm["overall_profit"]) < 0.0051
        assert abs(sm["total_now"] - eng.value()) < 0.025, (sm["total_now"], eng.value())
        assert abs(sm["overall_profit"] - (eng.value() - 100000.0)) < 0.025
        if self.p["capital"]["idle"] == "cash":
            self.interest_seen.append(bd["interest"])
            assert abs(bd["interest"] - self.expected_interest) < 0.02, (
                f"the 'interest' plug drifted to {bd['interest']} (expected {self.expected_interest:.2f}): a trade is not reconciling")

    # ---- day / restart helpers
    def close_day(self) -> None:
        if self.day:
            self.equity_rows.append((self.day.isoformat(), round(self.value_now(), 2)))
            self.prev_close = {s: v for s, v in self.last_px.items()}

    def restart(self) -> None:
        """Save portfolio.json and load it into a brand-new Portfolio, as a new engine process would."""
        path = self.tmp / "state" / "portfolio.json"
        self.pf.save(path)
        self.pf = Portfolio.load(path)
        json.dumps(asdict(self.pf))                        # the reloaded state serialises again

    def run_flat(self, day, start: tuple[int, int], end: tuple[int, int], prices: dict, **kw) -> None:
        """Tick every minute from start to end (inclusive) at constant prices."""
        t = at(day, *start)
        stop = at(day, *end)
        while t <= stop:
            self.tick(t, prices, **kw)
            t += timedelta(minutes=1)


def floor_pf(cash: float = 100000.0) -> Portfolio:
    pf = Portfolio()
    pf.cash, pf.liquidbees = cash, 0.0
    return pf


def seed_v2(pf: Portfolio, last_full_day: str) -> None:
    """Mark the previous day's check as done so the first tick of the test day is not an exits-only catch-up."""
    pf.runtime["strategy_mode"] = "v2_trend"
    pf.runtime.setdefault("v2", {})["last_full"] = last_full_day


def hold(pf: Portfolio, sym: str, qty: int, px: float, opened: str = "2026-10-30") -> None:
    pf.positions[sym] = Position(qty=qty, avg_cost=px, opened=opened, last_price=px, high_water=px, entry_ts=opened + "T15:10:00+05:30")
    pf.cash -= qty * px


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def read_ticks(day: str) -> list[tuple[str, dict]]:
    with open(FIX / f"ticks-{day}.csv", encoding="utf-8", newline="") as fh:
        return [(r["time"], {s: float(r[s]) for s in SYMS}) for r in csv.DictReader(fh)]


def load_daily() -> dict:
    raw = json.loads((FIX / "daily_bars.json").read_text(encoding="utf-8"))
    return raw


def hist_before(raw: dict, day: date) -> dict:
    """Completed bars strictly before `day`, as the live feed builds them (feed.completed_bars)."""
    out = {}
    for s, d in raw.items():
        n = sum(1 for x in d["dates"] if x < day.isoformat())
        out[s] = trend.clean_bars(d["close"][:n], d["high"][:n], d["low"][:n], d["dates"][:n])
    return out


def prev_close_of(raw: dict, day: date) -> dict:
    return {s: d["close"][sum(1 for x in d["dates"] if x < day.isoformat()) - 1] for s, d in raw.items()}


def close_is_finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
