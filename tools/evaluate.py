#!/usr/bin/env python3
"""Monthly / since-inception performance scorecard for the paper-trading simulator (read-only, no AI).

  python tools/evaluate.py --month 2026-10 [--format json|md|html]
  python tools/evaluate.py --all
  python tools/evaluate.py --month 2026-10 --write       # also writes reports/monthly/2026-10.json and .md
  python tools/evaluate.py --month 2026-10 --benchmarks closes.csv   # optional CSV: date,GOLDBEES,SILVERBEES
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _common import ROOT, now_ist, utf8_stdout

from simgold import evaluate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--month", help="YYYY-MM (default: current IST month)")
    g.add_argument("--all", action="store_true", help="since inception")
    ap.add_argument("--format", choices=("json", "md", "html"), default="md")
    ap.add_argument("--benchmarks", default=None, help="optional CSV of closes: date,GOLDBEES,SILVERBEES")
    ap.add_argument("--no-weekly", action="store_true", help="skip the 'This week: live vs rules alone' line (it fetches price data)")
    ap.add_argument("--write", action="store_true", help="write reports/monthly/YYYY-MM.json and .md (needs a month)")
    a = ap.parse_args(argv)
    utf8_stdout()
    month = None if a.all else (a.month or now_ist().strftime("%Y-%m"))
    if month and (len(month) != 7 or month[4] != "-" or not (month[:4] + month[5:]).isdigit()
                  or not 1 <= int(month[5:]) <= 12):
        print("--month must be YYYY-MM", file=sys.stderr)
        return 2
    if a.write and not month:
        print("--write needs a single month, not --all", file=sys.stderr)
        return 2
    r = evaluate.evaluate(a.root, month, a.benchmarks, weekly=not a.no_weekly)
    if a.write:
        if not r.get("available"):
            print("nothing to write: " + r.get("note", "no data"), file=sys.stderr)
            return 1
        d = Path(a.root) / "reports" / "monthly"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{month}.json").write_text(evaluate.render(r, "json"), encoding="utf-8")
        (d / f"{month}.md").write_text(evaluate.render(r, "md"), encoding="utf-8")
    sys.stdout.write(evaluate.render(r, a.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
