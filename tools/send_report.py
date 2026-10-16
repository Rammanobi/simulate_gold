#!/usr/bin/env python3
"""Generate the day's report and print what the routine must pass to the Gmail send_message tool.

  python tools/send_report.py --date 2026-10-13 [--report-url URL]
Prints one JSON object: {"to", "subject", "market_closed", "html_path", "text_path", "page_path", "warnings"}.
  to         -> send_message `to` (the REPORT_EMAIL env var, "" when unset: then skip the email, "REPORT_EMAIL not set")
  subject    -> send_message `subject` (always starts with "Metals Daybook · ")
  html_path  -> read this file and pass its text as `htmlBody`
  text_path  -> read this file and pass its text as `body`
  page_path  -> the interactive page (not sent)
Never run echo/printenv for the address (the auto-mode permission check denies it); read it from `to` here.

  python tools/send_report.py --date D --mark-sent   write reports/daily/D.sent.json; run ONLY after Gmail send_message
                                                     confirmed success (tools/report_retry.py treats it as "sent")
  python tools/send_report.py --recipient            print {"to": ...} only (for the weekly / incident emails)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

from _common import ROOT, utf8_stdout

from run_report import generate
from simgold import brief, report
from simgold.market import now_ist


def dashboard_link(root: Path, date: str) -> str | None:
    """The email's button opens this day on the Daybook Dashboard (config/dashboard_url.txt), when configured."""
    try:
        url = (Path(root) / "config" / "dashboard_url.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return f"{url}#{date}" if url.startswith("https://claude.ai/") else None


def recipient(environ=None) -> str:
    """The one report address from REPORT_EMAIL ("" when unset). Callers print it only as the JSON field `to`."""
    return ((environ if environ is not None else os.environ).get("REPORT_EMAIL") or "").strip()


def mark_sent(date: str, root: Path) -> Path:
    """reports/daily/D.sent.json: written by the routine only after the Gmail send_message succeeded."""
    p = Path(root) / "reports" / "daily" / f"{date}.sent.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": date, "sent_at": now_ist().isoformat(timespec="seconds")}, indent=1) + "\n", encoding="utf-8")
    return p


def make(date: str, root: Path, report_url: str | None = None) -> dict:
    root = Path(root)
    day, paths = generate(date, root, report_url=report_url)
    warnings = list(day.get("warnings", []))
    mt = brief.load_json(root / "state" / "market_today.json", None)
    if isinstance(mt, dict) and mt.get("date") and mt["date"] != date and not day.get("closed"):
        warnings.append(f"state/market_today.json is dated {mt['date']}, not {date}; the Market section may be stale")
    return {"to": recipient(), "subject": report.subject_line(day), "market_closed": bool(day.get("closed")), "html_path": str(paths["email"].resolve()), "text_path": str(paths["txt"].resolve()),
            "page_path": str(paths["page"].resolve()), "warnings": warnings}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="trading date, YYYY-MM-DD")
    ap.add_argument("--mark-sent", action="store_true")
    ap.add_argument("--recipient", action="store_true")
    ap.add_argument("--report-url")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    utf8_stdout()
    if a.recipient:
        print(json.dumps({"to": recipient()}))
        return 0
    if not a.date:
        ap.error("--date is required")
    try:
        dt.date.fromisoformat(a.date)
    except ValueError:
        ap.error("--date must look like 2026-10-13")
    if a.mark_sent:
        print(json.dumps({"marked": str(mark_sent(a.date, Path(a.root)).relative_to(Path(a.root)))}))
        return 0
    out = make(a.date, Path(a.root), a.report_url or dashboard_link(Path(a.root), a.date))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
