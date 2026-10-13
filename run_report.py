#!/usr/bin/env python
"""Build the end-of-day report: python run_report.py --date YYYY-MM-DD [--report-url URL]

Writes reports/daily/<date>.{json,email.html,txt,page.html} and prints the email subject.
`--sample` renders templates/sample_day.json into reports/daily/sample.* instead.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from simgold import report

ROOT = Path(__file__).resolve().parent


def generate(date: str, root: Path = ROOT, report_url: str | None = None, holiday: str | None = None):
    """Build and write the report files for one trading date. Returns (day dict, {json,email,txt,page: Path})."""
    root = Path(root)
    day = report.build_day(date, root, holiday=holiday)
    paths = report.write_reports(day, root / "reports" / "daily", date, ROOT / "templates" / "daily_report_template.html",
                                 report_url=report_url)
    return day, paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="trading date, YYYY-MM-DD (default: today)")
    ap.add_argument("--report-url", help="link for the email button")
    ap.add_argument("--sample", action="store_true", help="render templates/sample_day.json (keeps the SAMPLE badge)")
    ap.add_argument("--holiday", metavar="NAME", help="force a 'Market closed' report for this date")
    ap.add_argument("--root", default=str(ROOT), help="project root holding logs/, state/, reports/ (default: this repo)")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    root = Path(a.root)
    template = ROOT / "templates" / "daily_report_template.html"
    out = root / "reports" / "daily"
    if a.sample:
        day = json.loads((ROOT / "templates" / "sample_day.json").read_text(encoding="utf-8"))
        day = report.prepare(day)
        stem = "sample"
    else:
        date = a.date or dt.date.today().isoformat()
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            ap.error("--date must look like 2026-10-12")
        day = report.build_day(date, root, holiday=a.holiday)
        stem = date
    report.write_reports(day, out, stem, template, report_url=a.report_url, sample=a.sample)
    print(report.subject_line(day))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
