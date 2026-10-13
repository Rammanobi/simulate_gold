"""Live PAPER-trading loop for the Green Guard rulebook (docs/RULEBOOK.md). No real money, no broker.

Every tick (default 60 s) it pulls 1-minute prices and headlines, then runs simgold.engine.run_tick, which
applies the rulebook: floor, tripwire, sizing, limits, exits, fast-lane triggers and the decision log.

  python run_live.py                       # trade until the NSE close (15:30 IST)
  python run_live.py --until 12:20         # loop until 12:20 IST, whenever it was started
  python run_live.py --once --force        # one tick now, even if the market is closed (testing)
  python run_live.py --tick-seconds 30     # tick faster or slower
Weekends and NSE holidays are skipped (simgold/market.py), unless --force.
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path

import os

from simgold import config, gitsync, market
from simgold.broker import Portfolio
from simgold.data import daily_history, intraday_bars
from simgold.engine import Env, run_tick
from simgold.feed import build_market
from simgold.market import CLOSE, IST, is_market_open, now_ist
from simgold.news import NewsBook

ROOT = Path(__file__).parent
STATE, LOGS = ROOT / "state", ROOT / "logs"
PF_PATH, NEWS_PATH, EQUITY_PATH = STATE / "portfolio.json", STATE / "news.json", STATE / "equity.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("live")


class Live:
    def __init__(self, force: bool, tick_seconds: int):
        for d in (STATE, LOGS):
            d.mkdir(parents=True, exist_ok=True)
        self.force = force
        self.p = config.load_params()
        self.pf = Portfolio.load(PF_PATH)
        self.news = NewsBook(NEWS_PATH)
        self.env = Env(state_dir=STATE, logs_dir=LOGS, params=self.p, tick_seconds=tick_seconds)
        self.tickers = list(self.p["engine"]["tickers"].values())
        self.hist, self.hist_at = None, 0.0
        self.last_tick_day = None

    def _daily(self):
        if self.hist is None or time.time() - self.hist_at > 3600:
            self.hist, self.hist_at = daily_history(self.tickers, period="1y"), time.time()
        return self.hist

    def _value(self) -> float:
        v = self.pf.cash + self.pf.liquidbees
        for s, p in self.pf.positions.items():
            v += p.qty * (p.last_price or p.avg_cost) * (1 - self.p["instruments"][s]["cost"] / 2)
        return v

    def tick(self):
        ts = now_ist()
        bars = intraday_bars(self.tickers)
        if not bars:
            log.info("no intraday bars (exchange closed or feed down) - nothing to do")
            return
        newest = max(b.index[-1] for b in bars.values())
        stale_min = self.p["engine"]["stale_feed_skip_min"]
        if not self.force and (ts - newest.to_pydatetime()).total_seconds() > stale_min * 60:
            log.info("feeds are stale (last bar %s) - no trading this tick", newest.strftime("%H:%M"))
            return
        try:
            self.news.refresh(llm_score=False)
        except Exception:
            log.exception("news refresh failed")
        mkt = build_market(ts, bars, self._daily(), headlines=self.news.last_new, params=self.p)
        decisions = run_tick(self.pf, mkt, self.env)
        self.pf.save(PF_PATH)
        self.last_tick_day = ts.date()
        log.info("value Rs %.0f | floor Rs %.0f | positions %s | decisions %d", self._value(), self.pf.floor,
                 {s: p.qty for s, p in self.pf.positions.items()} or "none", len(decisions))

    def sync(self):
        """Commit and push live state, pull routine output. The engine re-reads guidance.json, fastlane.json and
        events.json from disk on every tick, so pulled changes are picked up on the next tick."""
        if not gitsync.enabled(os.environ):
            return
        if gitsync.sync(ROOT):
            log.info("git sync ok")
        # a pull may have changed files the engine owns the schema of; nothing is cached, so just note it
        log.info("guidance/fastlane/events will be re-read on the next tick")

    def close_day(self):
        """One 'date,value' line per day in state/equity.csv (the equity curve)."""
        day = (self.last_tick_day or now_ist().date()).isoformat()
        try:
            lines = EQUITY_PATH.read_text().splitlines() if EQUITY_PATH.exists() else []
            lines = [x for x in lines if not x.startswith(day)] + [f"{day},{self._value():.2f}"]
            EQUITY_PATH.write_text("\n".join(lines) + "\n")
        except OSError:
            log.exception("could not write equity.csv")


def _until(arg: str | None, now: datetime) -> datetime:
    if arg:
        h, m = map(int, arg.split(":"))
        return datetime.combine(now.date(), dtime(h, m), tzinfo=IST)
    return datetime.combine(now.date(), CLOSE, tzinfo=IST).replace(second=30)   # just after the close


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--force", action="store_true", help="ignore market hours and holidays (testing)")
    ap.add_argument("--max-minutes", type=float, default=400, help="stop after this many minutes")
    ap.add_argument("--tick-seconds", type=int, default=60, help="seconds between ticks (default 60)")
    ap.add_argument("--sync-every", type=float, default=5, metavar="MIN",
                    help="git sync interval in minutes (only if SIMGOLD_GIT_SYNC=1; default 5)")
    ap.add_argument("--until", metavar="HH:MM", help="keep looping until this IST clock time")
    args = ap.parse_args()

    now = now_ist()
    if not args.force and not market.is_trading_day(now.date()):
        log.info("%s is a weekend or NSE holiday - nothing to do", now.strftime("%a %d %b"))
        return
    end = _until(args.until, now)
    if not args.force and not args.once and now >= end:
        log.info("already past --until %s IST - nothing to do", args.until)
        return

    live = Live(force=args.force, tick_seconds=args.tick_seconds)
    started = last_sync = time.time()
    while True:
        t0 = time.time()
        now = now_ist()
        if args.force or is_market_open(now):
            try:
                live.tick()
            except Exception:
                log.exception("tick failed; will retry next tick")
        elif now.time() > CLOSE:
            break                                   # the session is over
        if args.sync_every > 0 and time.time() - last_sync >= args.sync_every * 60:
            live.sync()
            last_sync = time.time()
        if args.once or now_ist() >= end or (time.time() - started) / 60 >= args.max_minutes:
            break
        time.sleep(max(1.0, args.tick_seconds - (time.time() - t0)))
    if live.last_tick_day and (args.once or now_ist().time() >= dtime(15, 25)):
        live.close_day()
    live.sync()                                    # final push of whatever is left


if __name__ == "__main__":
    main()
