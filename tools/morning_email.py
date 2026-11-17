#!/usr/bin/env python3
"""Morning email sent by GitHub Actions, no Claude involved (paper trading only; no real money).

  python tools/morning_email.py [--date D] [--force] [--dry-run] [--root DIR] [--now ISO]

Run by .github/workflows/morning-email.yml (dispatched by the engine at 08:55 IST, plus two fallback crons). It:
1. does nothing unless config/params.yaml report.morning_email is true (--force bypasses ONLY this switch);
2. guards (never bypassed): D must be today (IST) and an NSE trading day; before 11:00 IST (later is useless);
   once per day (marker state/morning_sent.json {date, sent_at, status}: a "claimed" marker is pushed BEFORE sending);
3. builds the email with `tools/send_report.py --date D --kind morning`; if that option does not exist yet it exits 0;
4. sends it by the Gmail SMTP path of tools/backup_email.py: one recipient (REPORT_EMAIL), the renderer's own subject
   (must start "Metals Daybook"), plain-text and HTML parts. Secrets and addresses are never printed.
--dry-run builds and validates, sends nothing and writes no marker.
Exit codes: 0 sent / skipped / dry run, 2 refused configuration or bad renderer output, 1 SMTP or renderer failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import time
import uuid
import unicodedata
import sys
from email.message import EmailMessage
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

import backup_email as BE
from simgold import config
from simgold.market import IST, is_trading_day

CUTOFF = dt.time(11, 0)
MARKER = Path("state") / "morning_sent.json"


def switch_on(root: Path) -> bool:
    params = config.load_params(Path(root) / "config" / "params.yaml")
    return bool((params.get("report") or {}).get("morning_email"))


CLAIM_TTL = dt.timedelta(minutes=15)


def marker_blocks(text: str | None, date: str, now: dt.datetime) -> bool:
    """True when this marker text means 'the email for `date` is sent or being sent'. A claim older than CLAIM_TTL
    (run killed between claim and send) or one with an unreadable timestamp counts as released."""
    try:
        d = json.loads(text or "")
    except ValueError:
        return False
    if not isinstance(d, dict) or d.get("date") != date:
        return False
    if d.get("status", "sent") != "claimed":
        return True
    try:
        t = dt.datetime.fromisoformat(str(d["claimed_utc"]))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
    except (KeyError, ValueError):
        return False
    return (now.astimezone(dt.timezone.utc) - t) < CLAIM_TTL


def already_sent(root: Path, date: str, now: dt.datetime | None = None) -> bool:
    try:
        text = (Path(root) / MARKER).read_text(encoding="utf-8")
    except OSError:
        return False
    return marker_blocks(text, date, now or dt.datetime.now(dt.timezone.utc))


def write_marker(root: Path, date: str, now: dt.datetime, status: str = "sent") -> None:
    p = Path(root) / MARKER
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {"date": date, "sent_at": now.isoformat(timespec="seconds"), "status": status}
    if status == "claimed":
        d["nonce"] = f"{os.environ.get('GITHUB_RUN_ID', '')}-{uuid.uuid4().hex}"      # two runs never write identical claims
        d["claimed_utc"] = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    p.write_text(json.dumps(d, indent=1) + "\n", encoding="utf-8")


def renderer_available(root: Path, runner=subprocess.run) -> bool:
    """Feature-detect `send_report.py --kind` through --help (an old send_report has no such option)."""
    try:
        r = runner([sys.executable, str(Path(root) / "tools" / "send_report.py"), "--help"], capture_output=True, text=True,
                   timeout=60, cwd=str(root))
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and "--kind" in (r.stdout or "")


def build(root: Path, date: str, runner=subprocess.run) -> dict:
    r = runner([sys.executable, str(Path(root) / "tools" / "send_report.py"), "--date", date, "--kind", "morning",
                "--root", str(root)], capture_output=True, text=True, timeout=300, cwd=str(root))
    if r.returncode != 0:
        raise RuntimeError(f"renderer exit {r.returncode}")
    out = r.stdout or ""
    starts = [m.start() for m in re.finditer(r"^\{", out, re.M)]      # send_report prints indent=1 JSON: only the top-level brace is at column 0
    if not starts:
        raise RuntimeError("renderer printed no JSON")
    obj, _ = json.JSONDecoder().raw_decode(out[starts[-1]:])
    if not isinstance(obj, dict):
        raise RuntimeError("renderer JSON is not an object")
    return obj


def subject_problem(subject: str) -> str | None:
    """None when fine. No emoji/symbols/format characters, must start 'Metals Daybook' (the middle dot is allowed)."""
    if not subject.startswith(BE.SUBJECT_PREFIX):
        return "the generated subject does not start with 'Metals Daybook'"
    for ch in subject:
        if ch != "\u00b7" and (unicodedata.category(ch) in ("So", "Sk", "Cf", "Cs", "Cn", "Co", "Cc", "Mn", "Me", "Mc")
                                or 0xFE00 <= ord(ch) <= 0xFE0F or ord(ch) >= 0x1F000
                                or (ord(ch) > 127 and unicodedata.category(ch) == "Sm")):
            return "the generated subject contains an emoji or symbol character"
    return None


def marker_nonce(text: str | None) -> str | None:
    try:
        d = json.loads(text or "")
        return str(d["nonce"]) if isinstance(d, dict) and "nonce" in d else None
    except ValueError:
        return None


def git_publish(root: Path, message: str, retries: int = 3, sleep=time.sleep, claim: bool = False, date: str = "",
                now: dt.datetime | None = None) -> bool:
    """Commit state/morning_sent.json and push it to main. True only when a push succeeded.

    claim=True is the race-safe path: no `-X theirs`; before every push the marker on origin is re-read and if another run
    already claimed or sent `date` we give up, and after a push the marker on origin must carry OUR nonce (a no-op push of a
    dropped identical commit would otherwise look like success). On any failure the local claim commit is dropped (reset --hard to upstream),
    so nothing leaves a stray 'claimed' marker behind for a later push."""
    def git(*a):
        return subprocess.run(["git", *a], cwd=str(root), capture_output=True, text=True)

    def drop():
        if git("reset", "--hard", "@{u}").returncode != 0:
            git("reset", "--hard", "origin/main")

    git("config", "user.name", "simgold-morning-email")
    git("config", "user.email", "actions@users.noreply.github.com")
    git("add", "-A", "--", str(MARKER))
    if git("diff", "--cached", "--quiet").returncode != 0:
        if git("commit", "-q", "-m", message).returncode != 0:
            return False
    own = marker_nonce((Path(root) / MARKER).read_text(encoding="utf-8")) if claim and (Path(root) / MARKER).exists() else None
    for i in range(retries):
        if claim:
            git("fetch", "-q")
            up = git("show", f"@{{u}}:{MARKER.as_posix()}")
            if up.returncode == 0 and marker_blocks(up.stdout, date, now or dt.datetime.now(dt.timezone.utc)):
                break                                                    # someone else claimed or sent: we lost
            if git("pull", "--rebase", "--autostash", "-q").returncode != 0:
                git("rebase", "--abort")
                sleep(1)
                continue
        elif git("pull", "--rebase", "--autostash", "-X", "theirs", "-q").returncode != 0:
            git("rebase", "--abort")
        if git("push", "-q").returncode == 0:
            if not claim:
                return True
            git("fetch", "-q")
            up = git("show", f"@{{u}}:{MARKER.as_posix()}")
            if up.returncode == 0 and own is not None and marker_nonce(up.stdout) == own:
                return True
            break                                                        # pushed nothing of ours: someone else holds the claim
        sleep(5)
    if claim:
        drop()
    return False


def build_message(subject: str, sender: str, recipient: str, text: str, html: str) -> EmailMessage:
    s = " ".join((subject or "").split())              # no CR/LF header injection
    problem = subject_problem(s)
    if problem:
        raise BE.Refused(problem)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = s, sender, recipient
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def run(root: Path, date: str | None, environ, force=False, dry_run=False, now=None, smtp_factory=None,
        runner=subprocess.run, out=print, publish=None, clock=None) -> int:
    """publish(message, claim=False) -> bool commits+pushes the marker (None: no git, e.g. tests/local runs). clock() -> now, re-read before sending."""
    root = Path(root)
    now = (now or parse_now(None)).astimezone(IST)
    today = now.date()
    date = date or today.isoformat()
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        out("REFUSED: date must look like 2026-10-13")
        return 2
    if not force and not switch_on(root):
        out("morning email switch is off (report.morning_email: false); nothing to do")
        return 0
    if not is_trading_day(day):
        out(f"{date} is not an NSE trading day; skipped")
        return 0
    if day != today or now.time() >= CUTOFF:
        out("too late, skipped (the morning email is only sent on the day, before 11:00 IST)")
        return 0
    if already_sent(root, date, now):
        out(f"already sent for {date}; skipped")
        return 0
    if not renderer_available(root, runner):
        out("morning renderer not available yet (compact email not merged)")
        return 0
    try:
        built = build(root, date, runner)
        text = Path(built["text_path"]).read_text(encoding="utf-8")
        html = Path(built["html_path"]).read_text(encoding="utf-8")
        subject = " ".join(str(built["subject"]).split())
    except Exception as e:  # noqa: BLE001 - class name only
        out(f"renderer failed ({type(e).__name__})")
        return 1
    try:
        problem = subject_problem(subject)
        if problem:
            raise BE.Refused(problem)
        recipient = BE.check_recipient(environ.get("REPORT_EMAIL"))
    except BE.Refused as e:
        out(f"REFUSED: {e}")
        return 2
    out(f"subject: {subject}")
    out(f"to: {BE.mask(recipient)} (1 recipient)")
    out(f"text: {len(text.encode('utf-8'))} bytes, html: {len(html.encode('utf-8'))} bytes")
    if dry_run:
        out("dry run: not sent, no marker written")
        return 0
    try:
        sender, password = BE.check_sender(environ.get("SMTP_USER"), environ.get("SMTP_APP_PASSWORD"))
        msg = build_message(subject, sender, recipient, text, html)
    except BE.Refused as e:
        out(f"REFUSED: {e}")
        return 2
    later = (clock() if clock else now).astimezone(IST)           # the renderer may have taken minutes
    if later.date() != today or later.time() >= CUTOFF:
        out("too late, skipped (11:00 IST passed while building)")
        return 0
    # claim BEFORE sending: the marker must be on main first, so a failed push can never lead to a second email
    write_marker(root, date, later, "claimed")
    mine = (root / MARKER).read_text(encoding="utf-8")
    if publish and not publish(f"morning-email: {date} claim", claim=True):
        try:                                   # drop our own stray marker, never one a git reset restored from another run
            if (root / MARKER).read_text(encoding="utf-8") == mine:
                (root / MARKER).unlink()
        except OSError:
            pass
        out("could not push the claim marker (or another run already claimed it); nothing sent")
        return 1
    try:
        BE.send(msg, sender, password, recipient, smtp_factory)
    except Exception as e:  # noqa: BLE001 - never echo the exception text (it can hold addresses)
        (root / MARKER).unlink(missing_ok=True)
        if publish:
            publish(f"morning-email: {date} claim released (send failed)", claim=False)
        out(f"SMTP failed: {type(e).__name__}")
        return 1
    write_marker(root, date, later, "sent")
    if publish and not publish(f"morning-email: {date} sent"):
        out("warning: sent marker not pushed (the claim marker is already on main)")
    out(f"morning email sent to {BE.mask(recipient)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default="", help="the trading day, YYYY-MM-DD (default: today IST; must be today)")
    ap.add_argument("--force", action="store_true", help="bypass the report.morning_email switch only")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    ap.add_argument("--git", action="store_true", help="commit and push the marker (claim before sending); the workflow uses it")
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root)
    return run(root, a.date or None, os.environ, a.force, a.dry_run, parse_now(a.now),
               out=lambda s: print(s, flush=True),
               publish=(lambda m, claim=False: git_publish(root, m, claim=claim, date=a.date or parse_now(a.now).date().isoformat())) if a.git else None,
               clock=None if a.now else (lambda: parse_now(None)))


if __name__ == "__main__":
    sys.exit(main())
