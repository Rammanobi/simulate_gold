#!/usr/bin/env python3
"""Build the Daybook Dashboard document for one day (no AI, read-only for the repo).

  python tools/dashboard.py --date D                  # writes /tmp/simgold-dashboard-D.json, prints a one-line summary
  python tools/dashboard.py --date D --out FILE       # another output file (never inside the repo)
  python tools/dashboard.py --date D --write-log      # owner/backfill only: also write logs/dashboard-D.json

Routines then copy the file into the dashboard artifact's database with ArtifactData (`collection` "days",
`doc_id` D, `file_path` the printed path), so the JSON never passes through the conversation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold import dashboard
from simgold.market import now_ist


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, IST)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--write-log", action="store_true")
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root).resolve()
    date = a.date or now_ist().date().isoformat()
    out = Path(a.out or f"/tmp/simgold-dashboard-{date}.json").resolve()
    if root in out.parents:
        print("ERROR: --out must be outside the repo (logs/ is engine-owned; use --write-log for a backfill)")
        return 2
    doc = dashboard.build(date, root)
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if a.write_log:
        dashboard.write(date, root)
    c = doc["counts"]
    print(f"{out} | {date} close {doc['close']:.2f} pnl {doc['pnl']:+.2f} | decisions {len(doc['decisions'])} "
          f"(BUY {c['BUY']} SELL {c['SELL']} SKIP {c['SKIP']} BLOCK {c['BLOCK']} HOLD {c['HOLD']}) | "
          f"{out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
