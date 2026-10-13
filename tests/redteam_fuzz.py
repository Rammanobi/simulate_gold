"""Seeded fuzz harness for the money invariants (no hypothesis dependency, so CI needs nothing new).

Drives the real engine (simgold.engine.run_tick) over synthetic trading days: random walks, overnight gaps, crash
days, stale prices, random events, guidance (favour/neutral/block/stale/garbage), hostile fast-lane files and safe
mode. After every tick an independent oracle checks:

  * the floor never decreases; no negative or NaN qty / cash / LIQUIDBEES;
  * every BUY fits the per-instrument cap, the gross cap, the 30% LIQUIDBEES minimum and the risk budget,
    computed from the pre-buy state the oracle tracks itself;
  * no BUY in the last 3 trading days, in safe mode, before 09:30 / after 15:00, in an event window, after a
    tripwire (same day), in the caution zone, after the daily/weekly/spacing/loss/stop-out limits, after a
    fast-lane halt, or for a metal whose fresh guidance is block;
  * every fill is near the real market price (never on a bad print);
  * every decision line has the agreed fields, a known trigger and a non-empty reason.

It also measures how often a month ends below 1,00,000 and how often LIQUIDBEES < 30% outside a buy.

    python tests/redteam_fuzz.py --paths 10 --days 500 --profile stress|realistic|calm    # the docs/REDTEAM.md numbers
"""
from __future__ import annotations

import json
import math
import random
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simgold import config, guard, market  # noqa: E402
from simgold.broker import Portfolio  # noqa: E402
from simgold.engine import DECISION_FIELDS, TRIGGERS, Env, Market, run_tick  # noqa: E402
from simgold.market import IST  # noqa: E402

P = config.load_params()
SYMS = list(P["instruments"])
VOL = {"GOLDBEES": 0.010, "SILVERBEES": 0.018, "HINDCOPPER": 0.028}     # daily volatility
START = {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 300.0}
STEP_MIN = 5                                                            # one tick every 5 minutes
TIMES = [time(9, 15)]
while TIMES[-1] < time(15, 30):
    t = datetime.combine(date(2026, 1, 1), TIMES[-1]) + timedelta(minutes=STEP_MIN)
    TIMES.append(t.time())
ACTIONS = {"BUY", "SELL", "SKIP", "HOLD", "BLOCK"}
# per trading day: P(big overnight gap per instrument), gap size range, P(intraday crash per instrument), crash depth,
# P(market-wide crash day). "stress" is far harsher than GOLDBEES/SILVERBEES history on purpose; "calm" has no tail events
# beyond the normal daily volatility; "realistic" is a rough guess at real tails (about one 3-8% gap per instrument a year).
PROFILES = {
    "stress": {"gap_p": 0.02, "gap": (0.03, 0.20), "crash_p": 0.01, "crash": (0.08, 0.25), "market_crash_p": 0.004},
    "realistic": {"gap_p": 0.004, "gap": (0.03, 0.08), "crash_p": 0.002, "crash": (0.05, 0.12), "market_crash_p": 0.0005},
    "calm": {"gap_p": 0.0, "gap": (0.0, 0.0), "crash_p": 0.0, "crash": (0.0, 0.0), "market_crash_p": 0.0},
}


class InvariantError(AssertionError):
    pass


@dataclass
class Stats:
    days: int = 0
    ticks: int = 0
    buys: int = 0
    sells: int = 0
    tripwires: int = 0
    months: int = 0
    red_months: int = 0
    red_month_values: list = field(default_factory=list)
    first_month_red: int = 0
    first_months: int = 0
    liquid_low_ticks: int = 0
    held_ticks: int = 0
    floor_breaks: int = 0                  # value below floor at a tick (a gap jumped the tripwire)
    red_after_floor_break: int = 0         # red month ends that came after value had been below the floor
    red_with_positions_held: int = 0       # red month ends with a position still open at the close
    worst_month_end: float = float("inf")
    month_end_values: list = field(default_factory=list)
    end_values: list = field(default_factory=list)

    def merge(self, o: "Stats") -> None:
        for k, v in vars(o).items():
            cur = getattr(self, k)
            if isinstance(v, list):
                cur.extend(v)
            elif k == "worst_month_end":
                self.worst_month_end = min(cur, v)
            else:
                setattr(self, k, cur + v)


def _value(pf: Portfolio) -> float:
    v = pf.cash + pf.liquidbees
    for s, pos in pf.positions.items():
        v += pos.qty * (pos.last_price or pos.avg_cost) * (1 - P["instruments"][s]["cost"] / 2)
    return v


def _fail(msg: str, ctx: dict):
    raise InvariantError(f"{msg} | {json.dumps(ctx, default=str)[:1500]}")


class Path_:
    """One continuous portfolio over `days` trading days."""

    def __init__(self, seed: int, days: int, start: date, workdir: Path, profile: str = "stress"):
        self.rng = random.Random(seed)
        self.prof = PROFILES[profile]
        self.days, self.day = days, market.prev_trading_day(start) if market.is_trading_day(start) else start
        self.sd, self.ld = workdir / "state", workdir / "logs"
        self.sd.mkdir(parents=True, exist_ok=True)
        self.env = Env(self.sd, self.ld, P, environ={}, match_headline=None, tick_seconds=STEP_MIN * 60)
        self.pf = Portfolio()
        self.pf.cash, self.pf.liquidbees = 0.0, 100000.0
        self.px = dict(START)
        self.score = {s: 0.5 for s in SYMS}
        self.stats = Stats()
        self.month_first_seen: set[str] = set()
        self.first_month = None
        # oracle bookkeeping
        self.buys_day: dict[str, int] = {}
        self.buys_week: dict[tuple, int] = {}
        self.last_buy: datetime | None = None
        self.losses_day = 0
        self.stopped: set[str] = set()
        self.tripwire_day: date | None = None
        self.halt_day: date | None = None
        self.broke_floor = False

    # ------------------------------------------------------------------ the outside world for one day
    def new_day(self):
        self.day = market.next_trading_day(self.day)
        r = self.rng
        self.prev_close = dict(self.px)
        self.crash = {}
        for s in SYMS:
            g = r.gauss(0, VOL[s] * 0.6)
            pr = self.prof
            if r.random() < pr["gap_p"]:                            # a big overnight gap, either way
                g = r.choice((-1, 1)) * r.uniform(*pr["gap"])
            self.px[s] = max(1.0, self.px[s] * (1 + g))
            if r.random() < pr["crash_p"]:                          # an intraday crash in this instrument
                self.crash[s] = (r.randrange(5, len(TIMES) - 10), r.uniform(*pr["crash"]))
        if r.random() < self.prof["market_crash_p"]:                # a market-wide crash day
            k = r.randrange(5, len(TIMES) - 10)
            for s in SYMS:
                self.crash[s] = (k, r.uniform(*self.prof["crash"]))
        self.series = {s: [] for s in SYMS}
        self.buys_day, self.losses_day, self.stopped = {}, 0, set()
        self._write_world_files()

    def _write_world_files(self):
        r, d = self.rng, self.day
        ev = []
        if r.random() < 0.25:
            typ = r.choice(["CPI", "FOMC", "JOBS", "RBI", "HINDCOPPER_RESULTS", "BUDGET", "fed", "bogus"])
            when = d + timedelta(days=r.choice([0, 0, 1, 2]))
            row = {"date": when.isoformat(), "type": typ}
            if r.random() < 0.5:
                row["time"] = r.choice(["11:30", "18:00", "25:99", "abc", 1800, "00:30"])
            ev.append(row)
        (self.sd / "events.json").write_text(json.dumps(ev if r.random() > 0.03 else "garbage"))
        self.safe = r.random() < 0.03
        sm = self.sd / "safe_mode.json"
        if self.safe:
            sm.write_text(json.dumps({"on": True, "since": d.isoformat()}) if r.random() < 0.8 else "{corrupt")
        elif sm.exists():
            sm.unlink()

    def _guidance(self, now: datetime):
        r = self.rng
        roll = r.random()
        path = self.sd / "guidance.json"
        if roll < 0.10:
            if path.exists():
                path.unlink()
            return
        if roll < 0.15:
            path.write_text(r.choice(['{"bias": ["favour"]}', "[]", "{", '{"updated": 5, "bias": "favour"}']))
            return
        age = r.choice([1, 5, 30, 60, 200, -60])                  # -60: a future timestamp (must count as stale)
        bias = {k: r.choice(["favour", "neutral", "block", "FAVOUR", 7]) for k in ("GOLD", "SILVER", "COPPER")}
        path.write_text(json.dumps({"updated": (now - timedelta(minutes=age)).isoformat(), "bias": bias}))

    def _fastlane(self, now: datetime):
        r = self.rng
        sym = r.choice(SYMS)
        obj = r.choice([
            {sym: {"sell_fraction": r.choice([0.5, 1.0, 2.0, "x"])}},
            {sym: {"tighten_stop": r.choice([0.005, 1.0, 10 ** 9, -3])}},
            {sym: {"block_until": (now + timedelta(minutes=r.randint(-30, 120))).isoformat()}},
            {"halt_buys_today": True},
            {"buy": {sym: 100}, "floor": 0, sym: {"bias": "favour", "qty": 999}},       # hostile: all refused
            {sym: {"bias": r.choice(["neutral", "block", "favour"])}},
        ])
        upd = now + timedelta(minutes=r.choice([0, 0, 0, -45, 600]))
        obj["updated"] = upd.isoformat()
        (self.sd / "fastlane.json").write_text(json.dumps(obj))
        if obj.get("halt_buys_today") and upd <= now and (now - upd) <= timedelta(minutes=30):
            self.halt_day = self.day

    def _prices(self, k: int):
        r = self.rng
        sig = {s: VOL[s] / math.sqrt(len(TIMES)) for s in SYMS}
        for s in SYMS:
            ret = r.gauss(0, sig[s])
            if s in self.crash:
                start, depth = self.crash[s]
                if start <= k < start + 6:
                    ret -= depth / 6
            self.px[s] = max(1.0, self.px[s] * (1 + ret))
            target = 0.55 + 0.35 * math.tanh((self.px[s] / self.prev_close[s] - 1) / VOL[s])
            self.score[s] = min(1.0, max(0.0, 0.7 * self.score[s] + 0.3 * target + r.gauss(0, 0.08)))

    # ------------------------------------------------------------------ the oracle
    def _pre_trade_state(self, recs, trades):
        """The state the engine traded from in this tick (after interest accrual and re-marking, before any fill),
        rebuilt backwards from the state after the tick and this tick's fills."""
        pf = self.pf
        liquid = pf.cash + pf.liquidbees
        pos = {s: [p.qty, p.last_price or p.avg_cost] for s, p in pf.positions.items()}
        fills = [r for r in recs if r["action"] in ("BUY", "SELL")]
        for rec, tr in reversed(list(zip(fills, trades))):
            sym, q, fill = rec["symbol"], rec["qty"], rec["price"]
            slip = P["valuation"]["paper_slippage"][sym]
            if rec["action"] == "BUY":
                liquid += q * fill + tr["costs"]
                pos[sym][0] -= q
                if pos[sym][0] == 0:
                    pos.pop(sym)
            else:
                liquid -= q * fill - tr["costs"]
                pos.setdefault(sym, [0, fill / (1 - slip)])[0] += q
        return {"liquid": liquid, "pos": pos}

    def _check_buy(self, rec, trade, snap, now, fresh_block: set[str]):
        sym, qty, fill = rec["symbol"], rec["qty"], rec["price"]
        ctx = {"rec": rec, "day": self.day, "snap": snap}
        inst = P["instruments"][sym]
        slip = P["valuation"]["paper_slippage"][sym]
        t = now.time()
        if not (time(9, 30) <= t <= time(15, 0)):
            _fail("buy outside 09:30-15:00", ctx)
        td, left = market.trading_day_of_month(self.day), market.trading_days_left_in_month(self.day)
        if left < 3:                                       # independent of guard.week_factor on purpose
            _fail("buy in the last 3 trading days of the month", ctx)
        if self.safe:
            _fail("buy in safe mode", ctx)
        if self.tripwire_day == self.day:
            _fail("buy after a tripwire the same day", ctx)
        if self.halt_day == self.day:
            _fail("buy after a fast-lane halt", ctx)
        if sym in fresh_block:
            _fail("buy of a metal whose fresh guidance is block", ctx)
        if self.buys_day.get("ALL", 0) >= 3 or self.buys_day.get(sym, 0) >= 1:
            _fail("buy over the daily limit", ctx)
        wk = (guard.week_key(self.day), self.day.strftime("%Y-%m"), sym)
        if self.buys_week.get(wk, 0) >= 3:
            _fail("buy over the weekly limit", ctx)
        if self.last_buy and now - self.last_buy < timedelta(minutes=30):
            _fail("buys less than 30 minutes apart", ctx)
        if self.losses_day >= 2:
            _fail("buy after two losing trades today", ctx)
        if sym in self.stopped:
            _fail("re-buy after a stop-out the same day", ctx)
        evs = guard.normalise_events(json.loads((self.sd / "events.json").read_text()))
        if guard.event_rules(self.day, evs, P).buy_block(sym, t):
            _fail("buy inside an event window", ctx)
        # sizing, from the pre-buy state the oracle tracked
        px = fill / (1 + slip)
        mult = lambda s: guard.gap_multiplier(self.day, guard.event_rules(self.day, evs, P).night_mult(s), P)
        pos = snap["pos"]
        value = snap["liquid"] + sum(q * p * (1 - P["instruments"][s]["cost"] / 2) for s, (q, p) in pos.items())
        floor = rec["floor"]
        if value <= floor + P["green_guard"]["caution_buffer"] - 1:      # 1 rupee of slack for the oracle's own rounding
            _fail("buy in the caution zone", ctx)
        usable = guard.usable_risk(value, floor, snap["liquid"], self.day, P)
        used = sum(guard.position_risk(s, q, p, mult(s), P) for s, (q, p) in pos.items())
        new_risk = guard.position_risk(sym, qty, fill, mult(sym), P)
        tol = 30 + 0.002 * usable
        if used + new_risk > usable + tol:
            _fail(f"risk budget exceeded: used {used:.0f} + new {new_risk:.0f} > usable {usable:.0f}", ctx)
        own = (pos.get(sym, [0, 0])[0]) * fill + qty * fill
        if pos.get(sym, [0])[0] * fill + qty * fill > inst["cap_pct"] * value + fill:
            _fail(f"instrument cap exceeded: {own:.0f} > {inst['cap_pct']} x {value:.0f}", ctx)
        held = sum(q * p for q, p in pos.values())
        if held + qty * fill > guard.max_gross_pct(td, P) * value + fill + 1:
            _fail("gross cap exceeded", ctx)
        liquid_after = snap["liquid"] - qty * fill - trade["costs"]
        if liquid_after < P["capital"]["min_cash_pct"] * value - 1:
            _fail(f"LIQUIDBEES below 30% after a buy: {liquid_after:.0f} of {value:.0f}", ctx)
        # apply to the oracle
        snap["liquid"] = liquid_after
        q0 = pos.get(sym, [0, px])[0]
        pos[sym] = [q0 + qty, px]
        self.buys_day["ALL"] = self.buys_day.get("ALL", 0) + 1
        self.buys_day[sym] = self.buys_day.get(sym, 0) + 1
        self.buys_week[wk] = self.buys_week.get(wk, 0) + 1
        self.last_buy = now

    def _apply_sell(self, rec, trade, snap):
        sym = rec["symbol"]
        snap["liquid"] += rec["qty"] * rec["price"] - trade["costs"]
        q = snap["pos"].get(sym, [0, 0])
        q[0] -= rec["qty"]
        if q[0] <= 0:
            snap["pos"].pop(sym, None)
        if trade["pnl"] < 0:
            self.losses_day += 1
        if rec["trigger"] == "Stop-loss":
            self.stopped.add(sym)
        if rec["trigger"] == "Tripwire":
            if self.tripwire_day != self.day:
                self.stats.tripwires += 1
            self.tripwire_day = self.day

    def _fresh_blocks(self, now) -> set[str]:
        try:
            g = json.loads((self.sd / "guidance.json").read_text())
        except (OSError, ValueError):
            return set()
        if not isinstance(g, dict) or not isinstance(g.get("bias"), dict):
            return set()
        try:
            upd = datetime.fromisoformat(str(g.get("updated")))
        except ValueError:
            return set()
        upd = upd if upd.tzinfo else upd.replace(tzinfo=IST)
        if not (timedelta(minutes=-5) <= now - upd <= timedelta(minutes=75)):
            return set()
        return {config.symbol_of(k) for k, v in g["bias"].items() if guard.normalise_bias(v) == "block"}

    # ------------------------------------------------------------------ run
    def run(self) -> Stats:
        st = self.stats
        prev_floor = self.pf.floor
        for _ in range(self.days):
            self.new_day()
            st.days += 1
            ym = self.day.strftime("%Y-%m")
            if self.first_month is None:
                self.first_month = ym
            for k, t in enumerate(TIMES):
                now = datetime.combine(self.day, t, tzinfo=IST)
                self._prices(k)
                if k % 12 == 0:
                    self._guidance(now)
                if self.rng.random() < 0.02:
                    self._fastlane(now)
                stale = self.rng.random() < 0.03
                for s in SYMS:
                    self.series[s].append((now, self.px[s]))
                prices = dict(self.px)
                if self.rng.random() < 0.01:                       # bad prints: must be ignored
                    prices[self.rng.choice(SYMS)] = self.rng.choice([0.0, -1.0, float("nan"), float("inf"), 0.01])
                if self.rng.random() < 0.02:                       # one symbol missing this minute
                    prices.pop(self.rng.choice(SYMS))
                mkt = Market(now, prices, {s: (now - timedelta(minutes=20) if stale else now) for s in prices},
                             dict(self.score), {s: list(v) for s, v in self.series.items()}, dict(self.prev_close))
                n_trades = len(self.pf.trades)
                fresh_block = self._fresh_blocks(now)
                safe_before = self.safe
                recs = run_tick(self.pf, mkt, self.env)
                st.ticks += 1
                trades = self.pf.trades[n_trades:]
                snap = self._pre_trade_state(recs, trades)
                ti = 0
                for rec in recs:
                    if list(rec) != DECISION_FIELDS or rec["action"] not in ACTIONS or rec["trigger"] not in TRIGGERS \
                            or not isinstance(rec["reason"], str) or not rec["reason"].strip():
                        _fail("bad decision record", {"rec": rec})
                    if rec["action"] in ("BUY", "SELL"):
                        true = self.px[rec["symbol"]]
                        if not (0.5 * true <= rec["price"] <= 2 * true):      # filled on a bad print, not the market
                            _fail("fill far from the real market price", {"rec": rec, "true": true})
                        tr = trades[ti]
                        ti += 1
                        if rec["action"] == "BUY":
                            self.safe = safe_before
                            self._check_buy(rec, tr, snap, now, fresh_block)
                            st.buys += 1
                        else:
                            self._apply_sell(rec, tr, snap)
                            st.sells += 1
                if ti != len(trades):
                    _fail("trades and BUY/SELL records disagree", {"recs": recs, "trades": trades})
                pf = self.pf
                ctx = {"day": self.day, "t": str(t)}
                if pf.floor < prev_floor - 1e-9:
                    _fail("floor decreased", {**ctx, "was": prev_floor, "now": pf.floor})
                prev_floor = pf.floor
                if not (pf.cash >= -1e-6 and pf.liquidbees >= -1e-6) or pf.cash != pf.cash or pf.liquidbees != pf.liquidbees:
                    _fail("negative or NaN cash", {**ctx, "cash": pf.cash, "liquidbees": pf.liquidbees})
                for s, pos in pf.positions.items():
                    if pos.qty <= 0 or not (pos.last_price > 0) or pos.last_price != pos.last_price:
                        _fail("bad position", {**ctx, "sym": s, "qty": pos.qty, "last": pos.last_price})
                v = _value(pf)
                if v < pf.floor:
                    st.floor_breaks += 1
                    self.broke_floor = True
                if pf.positions:
                    st.held_ticks += 1
                    if (pf.cash + pf.liquidbees) < 0.30 * v - 0.005 * v and not pf.cooldowns.get("no_buys_until"):
                        st.liquid_low_ticks += 1
            if market.is_last_trading_day_of_month(self.day):
                v = _value(self.pf)
                st.months += 1
                st.month_end_values.append(round(v, 2))
                st.worst_month_end = min(st.worst_month_end, v)
                if v < 100000:
                    st.red_months += 1
                    st.red_month_values.append(round(v, 2))
                    st.red_after_floor_break += self.broke_floor
                    st.red_with_positions_held += bool(self.pf.positions)
                if ym == self.first_month:
                    st.first_months += 1
                    st.first_month_red += v < 100000
            # keep the work dir small: logs of finished days are not needed again
            for f in self.ld.glob("*"):
                f.unlink()
        st.end_values.append(round(_value(self.pf), 2))
        return st


def simulate(paths: int, days: int, seed: int = 20261013, start: date = date(2026, 1, 1), profile: str = "stress") -> Stats:
    total = Stats()
    for i in range(paths):
        work = Path(tempfile.mkdtemp(prefix="sg-fuzz-"))
        try:
            total.merge(Path_(seed + i, days, start, work, profile).run())
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return total


def summary(st: Stats) -> dict:
    mv = sorted(st.month_end_values)
    return {
        "trading_days": st.days, "ticks": st.ticks, "buys": st.buys, "sells": st.sells,
        "tripwire_days": st.tripwires, "month_ends": st.months, "red_month_ends": st.red_months,
        "red_rate": round(st.red_months / st.months, 4) if st.months else None,
        "first_month_red": f"{st.first_month_red}/{st.first_months}",
        "worst_month_end": round(st.worst_month_end, 2) if st.months else None,
        "median_month_end": mv[len(mv) // 2] if mv else None,
        "ticks_below_floor": st.floor_breaks, "red_after_floor_break": st.red_after_floor_break,
        "red_with_positions_held": st.red_with_positions_held,
        "liquid_below_30pct_share_of_held_ticks": round(st.liquid_low_ticks / st.held_ticks, 4) if st.held_ticks else 0.0,
        "end_values": st.end_values,
    }


if __name__ == "__main__":
    import argparse
    import logging
    logging.disable(logging.WARNING)
    import time as _t
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", type=int, default=10)
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20261013)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="stress")
    a = ap.parse_args()
    t0 = _t.time()
    s = simulate(a.paths, a.days, a.seed, profile=a.profile)
    out = summary(s)
    out["seconds"] = round(_t.time() - t0, 1)
    out["profile"] = a.profile
    print(json.dumps(out, indent=1))
