#!/usr/bin/env python3
"""Print a skeleton of FACTS (numbers only) for one day's memory note.

  python tools/daily_memory.py --date 2026-10-13
The Sonnet routine completes the two WHY/LEARNED lines and saves the result as memory/daily/<date>.md
(at most 15 lines). Nothing is written by this script.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import brief, report


def skeleton(date: str, root: Path, now=None) -> str:
    root = Path(root)
    day = report.build_day(date, root)
    d0 = dt.date.fromisoformat(date)
    L = [f"# {date} ({d0:%a})"]
    if day.get("closed"):
        L.append(f"- Market closed: {day.get('holiday_name')}. No trades.")
        L.append("- LEARNED: nothing")
        return "\n".join(L) + "\n"
    p, c = day["portfolio"], day["counts"]
    L.append(f"- Value: open {p['open']:,.0f}, close {p['close']:,.0f}; day P&L {p['day_pnl']:+,.0f} ({p['day_pnl_pct']:+.2f}%); "
             f"month P&L {p['month_pnl']:+,.0f}")
    L.append(f"- Floor {p['floor']:,.0f}; cushion {p['distance_to_floor']:,.0f}; costs {p['costs_paid']:,.0f}; "
             f"LIQUIDBEES income {p['liquidbees_income']:,.0f}")
    L.append(f"- Counts: {c['buys']} buys, {c['sells']} sells, {c['skips']} skips, {c['blocks']} blocks, "
             f"{c['holds']} holds, {c['fast_lane_runs']} fast-lane runs")
    trades = [x for x in day["decisions"] if x["action"] in ("BUY", "SELL")]
    for x in trades[:5]:
        pnl = f" pnl {x['pnl']:+,.0f}" if x.get("pnl") is not None else ""
        L.append(f"- {x['time']} {x['action']} {x['symbol']} {x['qty']} @ {x['price']}{pnl} [{x['trigger']}]")
    if len(trades) > 5:
        L.append(f"- (+{len(trades) - 5} more trades)")
    if day["holdings_close"]:
        L.append("- Held at close: " + ", ".join(f"{h['symbol']} {h['qty']} (unreal {h['unrealized']:+,.0f})"
                                                  for h in day["holdings_close"]))
    mk = [f"{m['name']} {m['move_pct']:+.1f}%" for m in day.get("market", [])][:5]
    if mk:
        L.append("- Market: " + ", ".join(mk))
    g = brief.guidance_view(root, parse_now(None) if now is None else now)
    if g["exists"]:
        L.append("- Guidance: " + ", ".join(f"{k}={v}" for k, v in g["bias"].items()))
    L.append("- WHY: <one line: what drove the day>")
    L.append("- LEARNED: <one line, or 'nothing'>")
    return "\n".join(L[:15]) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    try:
        dt.date.fromisoformat(a.date)
    except ValueError:
        ap.error("--date must look like 2026-10-13")
    utf8_stdout()
    print(skeleton(a.date, Path(a.root)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
