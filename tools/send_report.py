#!/usr/bin/env python3
"""Generate the day's report and print what the routine must pass to the Gmail send_message tool.

  python tools/send_report.py --date 2026-10-13 [--report-url URL]
Prints one JSON object: {"subject", "market_closed", "html_path", "text_path", "page_path", "warnings"}.
  subject    -> send_message `subject` (always starts with "Metals Daybook · ")
  html_path  -> read this file and pass its text as `htmlBody`
  text_path  -> read this file and pass its text as `body`
  page_path  -> the interactive page (not sent)
The recipient is NOT here: the Gmail guard hook only lets a message go to the REPORT_EMAIL address.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from _common import ROOT, utf8_stdout

from run_report import generate
from simgold import brief, report


def make(date: str, root: Path, report_url: str | None = None) -> dict:
    root = Path(root)
    day, paths = generate(date, root, report_url=report_url)
    warnings = list(day.get("warnings", []))
    mt = brief.load_json(root / "state" / "market_today.json", None)
    if isinstance(mt, dict) and mt.get("date") and mt["date"] != date and not day.get("closed"):
        warnings.append(f"state/market_today.json is dated {mt['date']}, not {date}; the Market section may be stale")
    return {"subject": report.subject_line(day), "market_closed": bool(day.get("closed")), "html_path": str(paths["email"].resolve()), "text_path": str(paths["txt"].resolve()),
            "page_path": str(paths["page"].resolve()), "warnings": warnings}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="trading date, YYYY-MM-DD")
    ap.add_argument("--report-url")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    try:
        dt.date.fromisoformat(a.date)
    except ValueError:
        ap.error("--date must look like 2026-10-13")
    utf8_stdout()
    out = make(a.date, Path(a.root), a.report_url)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
