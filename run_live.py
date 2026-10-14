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
import shutil

from simgold import alerts, brief, config, dashboard, gitsync, market, runguard
from simgold.incidents import IncidentMonitor, load_portfolio_safe
from simgold.broker import Portfolio
from simgold.data import daily_history, intraday_bars
from simgold.engine import Env, Market, run_tick
from simgold.feed import build_market
from simgold.market import CLOSE, IST, is_market_open, now_ist
from simgold.news import NewsBook

ROOT = Path(__file__).parent
STATE, LOGS = ROOT / "state", ROOT / "logs"
PF_PATH, NEWS_PATH, EQUITY_PATH = STATE / "portfolio.json", STATE / "news.json", STATE / "equity.csv"
PF_BACKUP = STATE / "portfolio.backup.json"
TRADES_ARCHIVE = LOGS / "trades-archive.jsonl"
PROTECTIVE_FROM = dtime(15, 15)       # time stop / score exit (15:15), overnight and month-end checks (15:20)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("live")


class Live:
    def __init__(self, force: bool, tick_seconds: int):
        for d in (STATE, LOGS, ROOT / "memory"):
            d.mkdir(parents=True, exist_ok=True)
        self.force = force
        self.p = config.load_params()
        self.mon = IncidentMonitor(LOGS, STATE)
        self.pf, recovery = load_portfolio_safe(PF_PATH, PF_BACKUP, ROOT)
        if recovery:      # restored (or rebuilt) state: safe mode, no new buys until the routine verifies a fix
            log.error("STATE RECOVERY: %s - safe mode, no new buys", recovery)
            self.mon.state_corrupt(recovery)
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
            return self._protective_tick(ts)
        newest = max(b.index[-1] for b in bars.values())
        stale_min = self.p["engine"]["stale_feed_skip_min"]
        if not self.force and (ts - newest.to_pydatetime()).total_seconds() > stale_min * 60:
            log.info("feeds are stale (last bar %s) - no trading this tick", newest.strftime("%H:%M"))
            return self._protective_tick(ts)
        self.mon.mark_fresh()
        try:
            self.news.refresh(llm_score=False)
        except Exception:
            log.exception("news refresh failed")
        mkt = build_market(ts, bars, self._daily(), headlines=self.news.last_new, params=self.p)
        decisions = run_tick(self.pf, mkt, self.env)
        self._save_pf()
        self._backup_state()
        self.last_tick_day = ts.date()
        log.info("value Rs %.0f | floor Rs %.0f | positions %s | decisions %d", self._value(), self.pf.floor,
                 {s: p.qty for s, p in self.pf.positions.items()} or "none", len(decisions))

    def _protective_tick(self, ts):
        """Feed down or stale (early close, outage) from 15:15: still run the 15:15 exits and the 15:20 overnight and
        month-end checks on the last known prices. No buy can happen: the engine only buys on a fresh price."""
        if ts.time() < PROTECTIVE_FROM:
            return
        decisions = run_tick(self.pf, Market(now=ts, prices={}, price_ts={}, scores={}), self.env)
        self._save_pf()
        self._backup_state()
        self.last_tick_day = ts.date()
        log.info("protective tick on last known prices (feed stale): %d decisions", len(decisions))

    def write_brief(self):
        """memory/brief.md for the Claude routines (cheap, no AI). Never raises."""
        if brief.safe_write(ROOT, params=self.p):
            log.info("memory/brief.md updated")

    def _save_pf(self):
        """Move trades beyond the last 200 to logs/trades-archive.jsonl (appended first), then save atomically."""
        moved = self.pf.trim_trades(TRADES_ARCHIVE)
        if moved:
            log.info("moved %d old trade(s) to %s", moved, TRADES_ARCHIVE.name)
        self.pf.save(PF_PATH)

    def _backup_state(self):
        try:
            shutil.copyfile(PF_PATH, PF_BACKUP)     # last known-good copy, written from memory just now
        except OSError:
            log.warning("could not write the portfolio backup")

    def sync(self):
        """Commit and push live state, pull routine output. The engine re-reads guidance.json, fastlane.json and
        events.json from disk on every tick, so pulled changes are picked up on the next tick."""
        if not gitsync.enabled(os.environ):
            return
        self.write_brief()                          # committed by the sync below
        day = (self.last_tick_day or now_ist().date()).isoformat()
        dashboard.safe_write(day, ROOT)             # logs/dashboard-DATE.json for the dashboard page
        if gitsync.sync(ROOT, monitor=self.mon):
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


def acquire_lock(path: Path):
    """An exclusive, non-blocking lock so two engine runs on one checkout cannot both trade and overwrite each other's
    portfolio.json (lost update). Returns the open file (keep it open) or None if another run holds it.
    On Actions the `paper-trader` concurrency group already serialises runs; this covers local / manual runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    try:
        import fcntl
    except ImportError:          # no flock (Windows): fall back to no lock
        return fh
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


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
    ap.add_argument("--safe-mode", metavar="REASON", nargs="?", const="tests failing", default=None,
                    help="turn safe mode on first (exits, stops and the floor only; no buys), e.g. when the test suite failed")
    args = ap.parse_args()

    now = now_ist()
    if not args.force and not market.is_trading_day(now.date()):
        log.info("%s is a weekend or NSE holiday - nothing to do", now.strftime("%a %d %b"))
        return
    end = _until(args.until, now)
    if not args.force and not args.once and now >= end:
        log.info("already past --until %s IST - nothing to do", args.until)
        return

    other = runguard.older_engine_run(os.environ)
    if other:          # a duplicate dispatch or a late cron: the older run owns the session, this one stops cleanly
        log.warning("another engine run holds this session (run %s, %s, started %s) - exiting without trading",
                    other.get("id"), other.get("event"), other.get("created_at"))
        return
    lock = acquire_lock(STATE / ".engine.lock")
    if lock is None:
        log.error("another engine run holds state/.engine.lock - not starting a second one")
        return
    if gitsync.enabled(os.environ):
        # start from the tip of main, not from the commit this run was dispatched on (it may have waited hours
        # in the concurrency queue): otherwise the engine trades on an old portfolio and overwrites newer logs
        gitsync.pull_latest(ROOT)
    if args.safe_mode:
        alerts.set_safe_mode(STATE, args.safe_mode, notify=False)    # idempotent; the incident pipeline alerts separately
        log.error("SAFE MODE requested on the command line (%s): no new buys this session", args.safe_mode)
    live = Live(force=args.force, tick_seconds=args.tick_seconds)
    started = last_sync = time.time()
    while True:
        t0 = time.time()
        now = now_ist()
        if args.force or is_market_open(now):
            try:
                live.tick()
                live.mon.tick_ok()
            except Exception as e:
                log.exception("tick failed; will retry next tick")
                live.mon.tick_failed(e)
            if not args.force:
                live.mon.check_feed(True)
        elif now.time() > CLOSE:
            break
        live.mon.housekeeping()                                   # the session is over
        if args.sync_every > 0 and time.time() - last_sync >= args.sync_every * 60:
            live.sync()
            last_sync = time.time()
        if args.once or now_ist() >= end or (time.time() - started) / 60 >= args.max_minutes:
            break
        time.sleep(max(1.0, args.tick_seconds - (time.time() - t0)))
    if live.last_tick_day and (args.once or now_ist().time() >= dtime(15, 25)):
        live.close_day()
    live.write_brief()                             # end of the run: always, even without git sync
    live.sync()                                    # final push of whatever is left (rewrites the brief once more)


if __name__ == "__main__":
    main()
