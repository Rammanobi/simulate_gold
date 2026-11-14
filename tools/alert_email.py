#!/usr/bin/env python3
"""Claude-free alarm email for a missed trading session (no AI, no routine; the Claude routines were the silent party on 12 Nov 2026).

    python tools/alert_email.py --problems FILE [--state state] [--dry-run]

FILE = tools/healthcheck.py output (one JSON problem per line). For a `session_missed` problem it sends ONE short plain-text email
per IST day through the same SMTP helper as tools/backup_email.py (recipient REPORT_EMAIL only, subject starts "Metals Daybook").
Once per day is kept in state/alerts.json (the incident's `emails` list, via simgold.alerts.record_email). Never raises and always
exits 0, so a failed send can never fail the watchdog. Env: SMTP_USER, SMTP_APP_PASSWORD, REPORT_EMAIL (never printed).
"""
from __future__ import annotations

import json
import os
import sys
import time
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from simgold import alerts  # noqa: E402

SUBJECT = "Metals Daybook · ALERT · no trading session today"
CHECK = "session_missed"


def session_missed_detail(text: str) -> str | None:
    for ln in text.splitlines():
        try:
            p = json.loads(ln)
        except ValueError:
            continue
        if isinstance(p, dict) and p.get("check") == CHECK:
            return str(p.get("detail") or "")[:400]
    return None


def already_sent_today(state_dir: Path, fp: str, now: float) -> bool:
    e = alerts.load_state(state_dir)["alerts"].get(fp)
    return bool(e and any(alerts.day_of(t) == alerts.day_of(now) for t in e.get("emails") or []))


def build(sender: str, recipient: str, detail: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = SUBJECT
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(
        "The paper-trading engine did not run (most of) a trading session.\n\n"
        f"{detail}\n\n"
        "No trade was placed by the missing session; the next one is started by the engine relay "
        "(see docs/RELAY.md). Check the market-hours and engine-relay runs in GitHub Actions and "
        "dispatch market-hours.yml by hand if the day is still open.\n\n"
        "Plain alert from the watchdog; Claude is not involved. Paper trading only.\n")
    return msg


def run(problems_text: str, state_dir: Path, environ, now: float | None = None, dry_run: bool = False,
        smtp_factory=None, out=print) -> str:
    """Returns 'none', 'duplicate', 'dry-run', 'sent', 'not-configured' or 'failed'. Never raises."""
    try:
        detail = session_missed_detail(problems_text)
        if detail is None:
            return "none"
        now = time.time() if now is None else now
        fp = alerts.fingerprint("watchdog", CHECK, "")
        if alerts.load_state(state_dir)["alerts"].get(fp) is None:
            alerts.raise_alert(state_dir, "watchdog", CHECK, CHECK, detail, wake=False, now=now)
        if already_sent_today(state_dir, fp, now):
            out("alert email: already sent today")
            return "duplicate"
        import backup_email as B
        try:
            recipient = B.check_recipient(environ.get("REPORT_EMAIL"))
            sender, password = B.check_sender(environ.get("SMTP_USER"), environ.get("SMTP_APP_PASSWORD"))
        except B.Refused as e:
            out(f"alert email: not sent, {e}")
            return "not-configured"
        if dry_run:
            out(f"alert email: [dry-run] would send '{SUBJECT}' to {B.mask(recipient)}")
            return "dry-run"
        B.send(build(sender, recipient, detail), sender, password, recipient, smtp_factory)
        alerts.record_email(state_dir, fp, now)
        out(f"alert email: sent to {B.mask(recipient)}")
        return "sent"
    except Exception as e:  # noqa: BLE001 - never fail the watchdog
        out(f"alert email: failed ({type(e).__name__})")
        return "failed"


def main(argv=None) -> int:
    a = sys.argv[1:] if argv is None else argv
    get = lambda k, d="": a[a.index(k) + 1] if k in a else d   # noqa: E731
    f = get("--problems")
    text = Path(f).read_text(encoding="utf-8", errors="replace") if f and Path(f).exists() else ""
    run(text, Path(get("--state", str(ROOT / "state"))), os.environ, dry_run="--dry-run" in a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
