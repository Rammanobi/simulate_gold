"""Live PAPER-trading loop: every minute while NSE is open, pull prices + news, decide, and
paper-execute. State lives in state/, decision logs in logs/, daily reports in reports/daily/.

  python run_live.py                 # loop until market close
  python run_live.py --once --force  # one tick now, even if the market is closed (for testing)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from simgold import config, llm
from simgold.brain import apply_review, decide
from simgold.broker import Portfolio
from simgold.data import daily_history, live_prices
from simgold.features import all_tickers, build_frames, snapshot_row, with_live_prices
from simgold.market import is_market_open, minutes_to_close, now_ist
from simgold.news import NewsBook

ROOT = Path(__file__).parent
STATE, LOGS, DAILY = ROOT / "state", ROOT / "logs", ROOT / "reports" / "daily"
for p in (STATE, LOGS, DAILY):
    p.mkdir(parents=True, exist_ok=True)
PF_PATH, NEWS_PATH, EQUITY_PATH = STATE / "portfolio.json", STATE / "news.json", STATE / "equity.csv"
STALE_MIN = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("live")


class Live:
    def __init__(self, force: bool):
        self.force = force
        self.pf = Portfolio.load(PF_PATH)
        self.news = NewsBook(NEWS_PATH)
        self.hist, self.hist_at = None, 0.0
        self.last_log_at = 0.0
        self.day_reports: list[dict] = []

    def _history(self):
        if self.hist is None or time.time() - self.hist_at > 3600:
            self.hist, self.hist_at = daily_history(all_tickers(), period="2y"), time.time()
        return self.hist

    def tick(self):
        ts = now_ist()
        tickers = all_tickers()
        prices, stamps = live_prices(tickers)
        asset_tk = {a: s["ticker"] for a, s in config.ASSETS.items()}
        fresh = {a: t for a, t in asset_tk.items() if t in prices and
                 (ts - stamps[t].tz_convert(ts.tzinfo)).total_seconds() < STALE_MIN * 60}
        if not fresh and not self.force:
            log.info("asset feeds stale (exchange closed or holiday) — no trading this tick")
            return
        new = self.news.refresh()
        if new:
            log.info("scored %d new headlines", new)
        sentiment = self.news.sentiment() if self.news.items else None

        today = pd.Timestamp(ts.date())
        frames = build_frames(with_live_prices(self._history(), prices, today))
        snap = {a: snapshot_row(f.iloc[-1]) for a, f in frames.items() if a in asset_tk and asset_tk[a] in prices}
        naive_ts = ts.replace(tzinfo=None)
        orders, report = decide(self.pf, snap, naive_ts, sentiment, cooldown_min=config.RISK.trade_cooldown_min)
        report["headlines"] = {a: self.news.top(a, 3) for a in config.ASSETS}

        if orders and os.getenv("ANTHROPIC_API_KEY"):
            review = llm.review_orders({**report, "upcoming_events_hint":
                                        "Check headlines for FOMC / US CPI / RBI dates within 24h."})
            report["review"] = review
            orders = apply_review(orders, review)
        fills = [f for o in orders if (f := self.pf.execute(o["asset"], o["side"], o["qty"], o["price"], naive_ts, o["reason"]))]
        report["fills"] = fills
        self.pf.save(PF_PATH)

        eq = self.pf.equity({a: s["price"] for a, s in snap.items()})
        scores = " ".join(f"{a}:{v['score']:+.2f}→{v['target_w']:.0%}" for a, v in report["assets"].items())
        log.info(f"equity ₹{eq:,.0f} | {scores} | fills={len(fills)}")
        if fills or time.time() - self.last_log_at > 15 * 60:
            with open(LOGS / f"decisions-{ts.date()}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(report, default=str) + "\n")
            self.last_log_at = time.time()
        self.day_reports.append({"ts": str(naive_ts), "equity": eq, "fills": fills, "risk": report["risk"],
                                 "assets": report["assets"]})

    def close_day(self):
        if not self.day_reports:
            return
        last = self.day_reports[-1]
        d = last["ts"][:10]
        with open(EQUITY_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{d},{last['equity']:.2f}\n")
        fills = [f for r in self.day_reports for f in r["fills"]]
        summary = {"date": d, "equity": last["equity"], "start_capital": config.STARTING_CAPITAL,
                   "risk": last["risk"], "assets": last["assets"], "fills": fills,
                   "positions": {a: vars(p) for a, p in self.pf.positions.items()},
                   "cash": self.pf.cash, "headlines": {a: self.news.top(a, 5) for a in config.ASSETS}}
        body = llm.write_journal(summary) or _template_journal(summary)
        (DAILY / f"{d}.md").write_text(body, encoding="utf-8")
        log.info("wrote daily report %s", DAILY / f"{d}.md")


def _template_journal(s: dict) -> str:
    pnl = s["equity"] - s["start_capital"]
    lines = [f"# {s['date']} — equity ₹{s['equity']:,.0f} ({pnl:+,.0f} vs start)", "",
             f"Cash ₹{s['cash']:,.0f} · drawdown {s['risk']['drawdown']:.1%} · MTD {s['risk']['mtd']:+.1%}", "",
             "| asset | price | score | tech | macro | news | target |", "|---|---|---|---|---|---|---|"]
    for a, v in s["assets"].items():
        news = "—" if v["news"] is None else f"{v['news']:+.2f}"
        lines.append(f"| {a} | {v['price']} | {v['score']:+.2f} | {v['tech']:+.2f} | {v['macro']:+.2f} | "
                     f"{news} | {v['target_w']:.0%} |")
    lines += ["", "## Trades"] + ([f"- {f['side']} {f['qty']} {f['asset']} @ {f['price']} — {f['reason']}" for f in s["fills"]] or ["- none"])
    lines += ["", "## Risk notes"] + ([f"- {n}" for n in s["risk"]["notes"]] or ["- none"])
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--force", action="store_true", help="ignore market hours (testing)")
    ap.add_argument("--max-minutes", type=float, default=400, help="stop after this many minutes")
    args = ap.parse_args()

    live = Live(force=args.force)
    started = time.time()
    if not (args.force or is_market_open()):
        log.info("NSE closed at %s IST — nothing to do", now_ist().strftime("%a %H:%M"))
        return
    while True:
        t0 = time.time()
        try:
            live.tick()
        except Exception:
            log.exception("tick failed; will retry next minute")
        if args.once or (not args.force and (not is_market_open() or minutes_to_close() <= 0)) \
                or (time.time() - started) / 60 >= args.max_minutes:
            break
        time.sleep(max(1.0, 60 - (time.time() - t0)))
    if args.once or not is_market_open():
        live.close_day()


if __name__ == "__main__":
    main()
