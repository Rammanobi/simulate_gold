#!/usr/bin/env python3
"""Retry the daily report when the 15:45 routine did not run (e.g. Claude usage limit reached). No AI.

Run by .github/workflows/report-retry.yml every 30 minutes after the close. It decides, for trading day D:
  SENT     reports/daily/D.sent.json exists (written only after the Gmail send succeeded; dates before 2026-10-16: the commit "routine(daily_report): D")  -> nothing to do
  WAIT     a retry is not due yet (backoff)                                           -> nothing to do
  FIRE n   wake the incident routine with {"kind": "daily_report_retry", "date": D, "attempt": n}
  BACKUP   it is past report.backup_after (config/params.yaml, default 16:30 IST) on the trading day and the report is
           still not SENT -> the workflow sends the backup email (tools/backup_email.py) at once, then mark-backup sets
           "backup_sent". After that nothing else happens that day: no further Claude retries (saves usage), no second
           backup, and if Claude sends its report later anyway there is no duplicate handling. Before backup_after the
           backoff below applies as before. Weekends/holidays: SKIP.
  BACKUP_SENT  the backup already went out for D -> nothing to do (also stops the Claude retries)
  GIVE_UP  (legacy path, normally unreachable now that BACKUP fires at 16:30) all 8 attempts used and the 8th was fired more than 90 minutes ago, still not SENT
           -> the workflow sends the backup email (tools/backup_email.py) unless the status is already "backup_sent"
Backoff after the 15:45 run: first retry 30 min after it, then waits of 60, 120, 120, ... minutes (exponential,
capped at 2 h), at most 8 attempts. So retries run until the plan's 5-hour usage window has reset at least twice.
State: state/report_retry.json {"date": D, "attempts": [iso, ...], "status": "..."} (written by the workflow only).
Status: new, "attempt n of 8", sent, gave_up, backup_sent.

  python tools/report_retry.py step [--date D] [--now ISO] [--dry-run]   # prints one of the words above
  python tools/report_retry.py check --date D                             # prints SENT or MISSING
  python tools/report_retry.py status --date D                            # prints the stored status of D
  python tools/report_retry.py mark-backup --date D                       # status := backup_sent (after the backup email)
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold.config import load_params
from simgold.market import IST, is_trading_day, now_ist

MAX_ATTEMPTS = 8
FIRST_DELAY_MIN = 30          # after the 15:45 routine (counted from 15:50, when it should be done)
WAITS_MIN = [60, 120]          # then 60, then 120 for every further attempt (cap)
GIVE_UP_AFTER_MIN = 90         # after the 8th attempt, wait this long for the routine before the backup email
REPORT_DONE = time(15, 50)
STATE = "state/report_retry.json"
BACKUP_AFTER_DEFAULT = "16:30"   # report.backup_after in config/params.yaml (owner approval 2026-10-29)


def backup_after(params: dict | None = None) -> time:
    """report.backup_after as a time of day (IST); a missing or malformed value falls back to 16:30."""
    try:
        p = params if params is not None else load_params()
        raw = str(((p or {}).get("report") or {}).get("backup_after", BACKUP_AFTER_DEFAULT))
        h, m = raw.strip().split(":")
        return time(int(h), int(m))
    except (OSError, ValueError, TypeError, AttributeError):
        return time(16, 30)


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


MARKER_FROM = "2026-10-16"   # from this date on only the sent marker counts (16 Oct: the commit existed, no email went out)


def sent(root: Path, d: str, runner=subprocess.run) -> bool:
    """SENT = reports/daily/D.sent.json exists (written after Gmail confirmed). Older dates: the routine commit."""
    if d >= MARKER_FROM:
        return (root / "reports" / "daily" / f"{d}.sent.json").is_file()
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


def decide(state: dict, d: date, now: datetime, params: dict | None = None) -> tuple[str, int]:
    """Verdict for an unsent report of trading day d (the caller has already handled SENT and SKIP).

    BACKUP_SENT  status is backup_sent: done for the day (no second email, no more Claude retries)
    BACKUP       now >= d at report.backup_after (16:30 IST): send the code backup immediately
    FIRE n / WAIT n / GIVE_UP  the Claude retry backoff described in the module docstring (before backup_after)
    """
    if state.get("status") == "backup_sent":
        return "BACKUP_SENT", len(state["attempts"])
    if now >= datetime.combine(d, backup_after(params), IST):
        return "BACKUP", len(state["attempts"])
    return decide_retry(state, d, now)


def decide_retry(state: dict, d: date, now: datetime) -> tuple[str, int]:
    """The Claude-retry backoff (FIRE / WAIT / GIVE_UP), without the 16:30 backup rule."""
    tries = state["attempts"]
    n = len(tries) + 1
    if n > MAX_ATTEMPTS:
        last = datetime.fromisoformat(tries[MAX_ATTEMPTS - 1])
        if now >= last + timedelta(minutes=GIVE_UP_AFTER_MIN) - timedelta(minutes=2):
            return "GIVE_UP", MAX_ATTEMPTS
        return "WAIT", MAX_ATTEMPTS
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
    ap.add_argument("cmd", choices=["step", "check", "status", "mark-backup"])
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
    if not is_trading_day(d):
        print(f"SKIP not a trading day {d.isoformat()}")
        return 0
    ds = d.isoformat()
    if a.cmd == "check":
        print("SENT" if sent(root, ds) else "MISSING")
        return 0
    state = load(root, ds)
    if a.cmd == "status":
        print(state["status"])
        return 0
    if a.cmd == "mark-backup":
        state["status"] = "backup_sent"
        state["backup_sent_at"] = now.isoformat(timespec="seconds")
        if not a.dry_run:
            save(root, state)
        print(f"BACKUP_SENT {ds}")
        return 0
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
    elif verdict == "GIVE_UP" and state["status"] not in ("gave_up", "backup_sent") and not a.dry_run:
        state["status"] = "gave_up"
        save(root, state)
    print(f"GIVE_UP {ds}" if verdict == "GIVE_UP" else f"{verdict} {ds}" if verdict in ("BACKUP", "BACKUP_SENT") else f"{verdict} {n} {ds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
