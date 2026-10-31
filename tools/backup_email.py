#!/usr/bin/env python3
"""BACKUP email of the daily report when Claude could not send it (paper trading only; no real money). No AI needed.

  python tools/backup_email.py --date D [--dry-run] [--root DIR]

Run by .github/workflows/report-retry.yml after the 8 retries of the daily_report routine all failed (GIVE_UP), or by
hand with workflow_dispatch force_backup. It:
1. Optionally asks DeepSeek for a short summary paragraph, ONLY when state/market_today.json is not dated D (Claude wrote
   no market notes that day) and DEEPSEEK_API_KEY is set. It reuses the paragraph of an existing fallback report
   (reports/daily/D.json "fallback_summary") when there is one, else calls tools/fallback_report.summary_paragraph (fast
   tier; plain text and every number checked against the facts). Any DeepSeek problem is ignored: the report still goes.
2. Builds the normal report with tools/send_report.make (deterministic Python, numbers from the logs).
3. Sends it with Gmail SMTP (smtp.gmail.com:465, SSL) as multipart/alternative: the generated text and HTML exactly as
   generated (with a DeepSeek paragraph, only a short labelled banner is put in front of each).

Safety (enforced here, tested in tests/test_backup_email.py):
  * exactly one recipient, REPORT_EMAIL; refused if empty or containing a comma, semicolon, whitespace or newline;
  * subject = the normal subject + " · backup (Claude unavailable)", and it must start with "Metals Daybook";
  * the password and the addresses are never printed (the recipient is shown masked, e.g. r***@gmail.com);
  * env: SMTP_USER (sending Gmail address), SMTP_APP_PASSWORD (a Gmail app password), REPORT_EMAIL.
Exit codes: 0 sent (or dry run built), 2 refused configuration, 1 SMTP failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from _common import ROOT, utf8_stdout

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SUBJECT_PREFIX = "Metals Daybook"
SUBJECT_SUFFIX = " · backup (Claude unavailable)"
ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+\x40[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")   # \x40: no at-sign here
BANNER_STYLE = "padding:12px 20px;font-family:Arial,sans-serif;font-size:14px;background:#fbf6e3;color:#1f2937;"


class Refused(Exception):
    """Configuration that breaks a safety rule. The message never contains an address or the password."""


def mask(addr: str) -> str:
    """r***@gmail.com style; anything that is not one plain address is shown as '(invalid)'."""
    a = (addr or "").strip()
    if not ADDRESS.match(a):
        return "(invalid)"
    local, domain = a.split("\x40", 1)
    return f"{local[0]}***\x40{domain}"


def check_recipient(addr: str | None) -> str:
    a = (addr or "").strip()          # GitHub's secret box can keep a pasted line break at the ends
    if not a.strip():
        raise Refused("REPORT_EMAIL is empty")
    if re.search(r"[,;\s]", a):
        raise Refused("REPORT_EMAIL must be exactly one address (no comma, semicolon, space or newline)")
    if not ADDRESS.match(a):
        raise Refused("REPORT_EMAIL is not a plain email address")
    return a


def check_sender(user: str | None, password: str | None) -> tuple[str, str]:
    u = (user or "").strip()
    if not u.strip() or re.search(r"[,;\s]", u) or not ADDRESS.match(u):
        raise Refused("SMTP_USER must be one plain Gmail address")
    password = re.sub(r"\s", "", password or "")   # Gmail shows it as 4 groups of 4; spaces/line breaks are never part of it
    if not password:
        raise Refused("SMTP_APP_PASSWORD is empty")
    return u, password


def backup_subject(subject: str) -> str:
    s = " ".join((subject or "").split())          # no CR/LF header injection
    if not s.startswith(SUBJECT_PREFIX):
        raise Refused("the generated subject does not start with 'Metals Daybook'")
    return s + SUBJECT_SUFFIX


def build_message(subject: str, sender: str, recipient: str, text: str, html: str, summary: str = "") -> EmailMessage:
    """multipart/alternative: the generated text and HTML exactly as generated (plus a short banner if summary)."""
    from simgold import report
    msg = EmailMessage()
    msg["Subject"] = backup_subject(subject)
    msg["From"] = sender
    msg["To"] = recipient
    if summary:
        text = f"Backup (Claude unavailable), summary by the fallback model: {summary}\n\n{text}"
        banner = (f'<div style="{BANNER_STYLE}"><b>Backup email (Claude unavailable).</b> {report.esc(summary)}</div>')
        m = re.search(r"<body[^>]*>", html, re.I)
        html = html[:m.end()] + banner + html[m.end():] if m else banner + html
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send(msg: EmailMessage, sender: str, password: str, recipient: str, factory=None) -> None:
    if factory is None:
        factory = lambda h, p: smtplib.SMTP_SSL(h, p, timeout=30, context=ssl.create_default_context())  # noqa: E731
    with factory(SMTP_HOST, SMTP_PORT) as s:
        s.login(sender, password)
        s.send_message(msg, from_addr=sender, to_addrs=[recipient])      # exactly one envelope recipient


def market_today_date(root: Path) -> str | None:
    try:
        d = json.loads((root / "state" / "market_today.json").read_text(encoding="utf-8"))
        return str(d.get("date")) if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def deepseek_summary(root: Path, date: str, environ, client=None) -> tuple[str, str]:
    """-> (summary, note). Empty summary when not needed, not configured or failed. Never raises."""
    if market_today_date(root) == date:
        return "", "market notes for the day exist; no DeepSeek summary needed"
    try:
        old = json.loads((root / "reports" / "daily" / f"{date}.json").read_text(encoding="utf-8"))
        if isinstance(old, dict) and old.get("fallback") and isinstance(old.get("fallback_summary"), str) \
                and old["fallback_summary"].strip():
            return " ".join(old["fallback_summary"].split())[:600], "summary from the earlier fallback report"
    except (OSError, ValueError):
        pass
    key = environ.get("DEEPSEEK_API_KEY", "")
    if not key and client is None:
        return "", "DEEPSEEK_API_KEY not set; deterministic report only"
    try:
        import fallback_report as fr
        from simgold import config, report
        from simgold.fallback_llm import Client, load_cfg
        client = client or Client(key, load_cfg(config.load_params()))
        day = report.build_day(date, root)
        text, model = fr.summary_paragraph(day, client)
        if model:
            return text, f"summary by DeepSeek ({model})"
        return "", "DeepSeek gave no usable paragraph; deterministic report only"
    except Exception as e:  # noqa: BLE001 - the backup must still go out
        return "", f"DeepSeek step failed ({type(e).__name__}); deterministic report only"


def run(root: Path, date: str, environ, dry_run: bool = False, smtp_factory=None, client=None, out=print) -> int:
    root = Path(root)
    summary, note = deepseek_summary(root, date, environ, client)
    out(note)
    import send_report
    built = send_report.make(date, root, send_report.dashboard_link(root, date))
    text = Path(built["text_path"]).read_text(encoding="utf-8")
    html = Path(built["html_path"]).read_text(encoding="utf-8")
    try:
        subject = backup_subject(built["subject"])
        recipient = check_recipient(environ.get("REPORT_EMAIL"))
    except Refused as e:
        out(f"REFUSED: {e}")
        return 2
    out(f"subject: {subject}")
    out(f"to: {mask(recipient)} (1 recipient)")
    out(f"text: {len(text.encode('utf-8'))} bytes, html: {len(html.encode('utf-8'))} bytes")
    if dry_run:
        out("dry run: not sent")
        return 0
    try:
        sender, password = check_sender(environ.get("SMTP_USER"), environ.get("SMTP_APP_PASSWORD"))
        msg = build_message(built["subject"], sender, recipient, text, html, summary)
    except Refused as e:
        out(f"REFUSED: {e}")
        return 2
    try:
        send(msg, sender, password, recipient, smtp_factory)
    except Exception as e:  # noqa: BLE001 - never echo the exception text (it can hold addresses)
        out(f"SMTP failed: {type(e).__name__}")
        return 1
    out(f"backup email sent to {mask(recipient)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="trading date, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="build and show subject, masked recipient and sizes; send nothing")
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    try:
        dt.date.fromisoformat(a.date)
    except ValueError:
        ap.error("--date must look like 2026-10-13")
    utf8_stdout()
    return run(Path(a.root), a.date, os.environ, a.dry_run, out=lambda s: print(s, flush=True))


if __name__ == "__main__":
    sys.exit(main())
