#!/usr/bin/env python3
"""Retry the daily report when the 15:45 routine did not run (e.g. Claude usage limit reached). No AI.

Run by .github/workflows/report-retry.yml every 30 minutes after the close. It decides, for trading day D:
  SENT     the daily report was pushed (a commit "routine(daily_report): D" exists)  -> nothing to do
  WAIT     a retry is not due yet (backoff)                                           -> nothing to do
  FIRE n   wake the incident routine with {"kind": "daily_report_retry", "date": D, "attempt": n}
  GIVE_UP  all attempts used                                                          -> nothing more
Backoff after the 15:45 run: first retry 30 min after it, then waits of 60, 120, 120, ... minutes (exponential,
capped at 2 h), at most 8 attempts. So retries run until the plan's 5-hour usage window has reset at least twice.
State: state/report_retry.json {"date": D, "attempts": [iso, ...], "status": "..."} (written by the workflow only).

  python tools/report_retry.py step [--date D] [--now ISO] [--dry-run]   # prints one of the words above
  python tools/report_retry.py check --date D                             # prints SENT or MISSING
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold.market import IST, is_trading_day, now_ist

MAX_ATTEMPTS = 8
FIRST_DELAY_MIN = 30          # after the 15:45 routine (counted from 15:50, when it should be done)
WAITS_MIN = [60, 120]          # then 60, then 120 for every further attempt (cap)
REPORT_DONE = time(15, 50)
STATE = "state/report_retry.json"


def wait_before(attempt: int) -> int:
    """Minutes to wait after attempt n-1 before attempt n (n >= 2)."""
    return WAITS_MIN[min(attempt - 2, len(WAITS_MIN) - 1)]


def report_day(now: datetime) -> date | None:
    """The trading day whose report is due: today after 15:50, else the previous trading day (within 3 days)."""
    d = now.date() if now.time() >= REPORT_DONE else now.date() - timedelta(days=1)
    for _ in range(4):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return None


def sent(root: Path, d: str, runner=subprocess.run) -> bool:
    r = runner(["git", "-C", str(root), "log", "--since", f"{d} 00:00 +0530", "--format=%s", "-n", "2000"],
               capture_output=True, text=True)
    return any(line.strip().startswith(f"routine(daily_report): {d}") for line in (r.stdout or "").splitlines())


def load(root: Path, d: str) -> dict:
    try:
        s = json.loads((root / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    if not isinstance(s, dict) or s.get("date") != d:
        s = {"date": d, "attempts": [], "status": "new"}
    s["attempts"] = [str(a) for a in s.get("attempts") or []][:MAX_ATTEMPTS]
    return s


def decide(state: dict, d: date, now: datetime) -> tuple[str, int]:
    tries = state["attempts"]
    n = len(tries) + 1
    if n > MAX_ATTEMPTS:
        return "GIVE_UP", len(tries)
    if not tries:
        due = datetime.combine(d, REPORT_DONE, IST) + timedelta(minutes=FIRST_DELAY_MIN)
    else:
        due = datetime.fromisoformat(tries[-1]) + timedelta(minutes=wait_before(n))
    return ("FIRE", n) if now >= due - timedelta(minutes=2) else ("WAIT", n)


def save(root: Path, state: dict) -> None:
    p = root / STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["step", "check"])
    ap.add_argument("--date")
    ap.add_argument("--now")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root)
    now = datetime.fromisoformat(a.now) if a.now else now_ist()
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    d = date.fromisoformat(a.date) if a.date else report_day(now)
    if d is None:
        print("SKIP no trading day")
        return 0
    ds = d.isoformat()
    if a.cmd == "check":
        print("SENT" if sent(root, ds) else "MISSING")
        return 0
    state = load(root, ds)
    if sent(root, ds):
        if state["status"] != "sent" and state["attempts"]:
            state["status"] = "sent"
            if not a.dry_run:
                save(root, state)
        print(f"SENT {ds}")
        return 0
    verdict, n = decide(state, d, now)
    if verdict == "FIRE" and not a.dry_run:
        state["attempts"].append(now.isoformat(timespec="seconds"))
        state["status"] = f"attempt {n} of {MAX_ATTEMPTS}"
        save(root, state)
    elif verdict == "GIVE_UP" and state["status"] != "gave_up" and not a.dry_run:
        state["status"] = "gave_up"
        save(root, state)
    print(f"{verdict} {n} {ds}" if verdict != "GIVE_UP" else f"GIVE_UP {ds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
