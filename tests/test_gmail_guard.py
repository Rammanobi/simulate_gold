"""The Gmail send lock (.claude/hooks/gmail_guard.py) driven the way Claude Code drives it: JSON on stdin."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = Path(os.environ.get("GMAIL_GUARD_PATH") or ROOT / ".claude" / "hooks" / "gmail_guard.py")
pytestmark = pytest.mark.skipif(not GUARD.exists(), reason="the Gmail guard hook is not installed under .claude/hooks")

# a made-up address: the real one must never be in this public repo
ADDR = "daybook-test@example.invalid"
SUBJECT = "Metals Daybook · 📈 Mon 12 Oct · +₹147 (green)"
INCIDENT_SUBJECT = "Metals Daybook · 🔁 Re-checked · 2026-10-12"

# the daily report must be the generated files verbatim: a fake project with one generated report
FAKE_ROOT = Path(tempfile.mkdtemp(prefix="gmail-guard-"))
(FAKE_ROOT / "reports" / "daily").mkdir(parents=True)
(FAKE_ROOT / "reports" / "daily" / "2026-10-12.email.html").write_text("<p>hi</p>\n", encoding="utf-8")
(FAKE_ROOT / "reports" / "daily" / "2026-10-12.txt").write_text("hi\n", encoding="utf-8")


def run(tool, tool_input=None, addr=ADDR, raw=None, root=FAKE_ROOT):
    env = {k: v for k, v in os.environ.items() if k != "REPORT_EMAIL"}
    env["CLAUDE_PROJECT_DIR"] = str(root)
    if addr is not None:
        env["REPORT_EMAIL"] = addr
    payload = raw if raw is not None else json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                                      "tool_input": tool_input if tool_input is not None else {}})
    return subprocess.run([sys.executable, "-I", str(GUARD)], input=payload, capture_output=True, text=True, env=env)


def send(**over):
    body = {"to": [ADDR], "subject": SUBJECT, "body": "hi", "htmlBody": "<p>hi</p>"}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def test_the_report_email_is_allowed():
    assert run("mcp__Gmail__send_message", send()).returncode == 0
    assert run("mcp__Gmail__send_message", send(to=[ADDR.upper()])).returncode == 0       # case-insensitive
    assert run("mcp__Gmail__send_message", send(to=ADDR)).returncode == 0                 # a bare string is one recipient
    assert run("mcp__Gmail__send_message", send(subject=INCIDENT_SUBJECT, htmlBody=None)).returncode == 0
    assert run("mcp__claude_ai_Gmail__send_message", send()).returncode == 0              # other server prefixes


@pytest.mark.parametrize("over", [
    {"to": ["someone@example.invalid"]},
    {"to": [ADDR, "someone@example.invalid"]},
    {"to": []},
    {"to": [ADDR + ".evil"]},
    {"to": ["x" + ADDR]},
    {"to": ["Daybook <%s>" % ADDR]},
    {"to": ADDR + ", someone@example.invalid"},
    {"to": None},
    {"cc": ["someone@example.invalid"]},
    {"bcc": ["someone@example.invalid"]},
    {"attachments": [{"content": "AAAA"}]},
    {"draftId": "r123"},
    {"replyThreadId": "t1"},
    {"subject": "Hello"},
    {"subject": "metals daybook lower"},
    {"subject": " Metals Daybook"},
    {"subject": None},
    {"body": ["not text"]},
])
def test_everything_else_in_a_send_is_blocked(over):
    r = run("mcp__Gmail__send_message", send(**over))
    assert r.returncode == 2 and "gmail_guard" in r.stderr


def test_sending_is_blocked_without_report_email():
    assert run("mcp__Gmail__send_message", send(), addr=None).returncode == 2
    assert run("mcp__Gmail__send_message", send(), addr="").returncode == 2
    assert run("mcp__Gmail__send_message", send(), addr="   ").returncode == 2
    assert run("mcp__Gmail__send_message", send(to=[""]), addr="").returncode == 2


@pytest.mark.parametrize("tool", [
    "create_draft", "update_draft", "delete_draft", "forward", "reply", "trash_message", "trash_thread", "untrash_message",
    "label_message", "label_thread", "unlabel_message", "create_label", "delete_label", "update_label",
    "update_message_labels", "mark_message_spam", "mark_thread_spam", "apply_sensitive_message_label", "send_draft",
    "something_new",
])
def test_every_other_write_tool_is_blocked(tool):
    assert run("mcp__Gmail__" + tool, send()).returncode == 2


@pytest.mark.parametrize("tool", ["get_message", "get_thread", "get_draft", "list_drafts", "list_labels", "search_threads"])
def test_read_tools_are_allowed(tool):
    assert run("mcp__Gmail__" + tool, {"query": "x"}).returncode == 0
    assert run("mcp__Gmail__" + tool, {"query": "x"}, addr=None).returncode == 0


def test_bad_input_fails_closed():
    for raw in ("", "not json", "[]", "null", json.dumps({"tool_name": "mcp__Gmail__send_message"}),
                json.dumps({"tool_name": "mcp__Gmail__send_message", "tool_input": "x"}),
                json.dumps({"tool_name": 5, "tool_input": {}})):
        assert run("", raw=raw).returncode == 2, raw


def test_the_address_is_not_in_the_repo():
    # the settings file only names the guard; the real address lives in the environment
    settings = ROOT / ".claude" / "settings.json"
    if settings.exists():
        cfg = json.loads(settings.read_text())
        hook = cfg["hooks"]["PreToolUse"][0]
        assert hook["matcher"] == "mcp__.*[Gg][Mm][Aa][Ii][Ll].*"     # any letter case of "gmail"
        assert hook["hooks"][0]["command"] == "python3 -I .claude/hooks/gmail_guard.py"
        assert "@" not in settings.read_text()
    assert "@" not in GUARD.read_text()


# ------------------------------------------------------------------ the daily report is sent verbatim (14 Oct 2026)
def test_daily_report_must_be_the_generated_html_and_text():
    ok = run("mcp__Gmail__send_message", send(htmlBody="<p>hi</p>", body="hi"))
    assert ok.returncode == 0
    assert run("mcp__Gmail__send_message", send(htmlBody="  <p>hi</p>\n\n", body="hi\n")).returncode == 0   # whitespace only
    for over in ({"htmlBody": "<div>my own short version</div>"}, {"htmlBody": None}, {"htmlBody": ""},
                 {"body": "a rewritten text body"}):
        r = run("mcp__Gmail__send_message", send(**over))
        assert r.returncode == 2 and "send_report.py" in r.stderr, over


def test_other_metals_daybook_emails_are_not_tied_to_the_report_files():
    assert run("mcp__Gmail__send_message", send(subject=INCIDENT_SUBJECT, htmlBody="<p>incident</p>")).returncode == 0
    weekly = "Metals Daybook · Weekly 2026-W42 · +0.1% vs GOLDBEES -0.4%"
    assert run("mcp__Gmail__send_message", send(subject=weekly, htmlBody="<p>w</p>")).returncode == 0


def test_the_real_generated_report_passes(tmp_path):
    day = ROOT / "reports" / "daily" / "2026-10-14.email.html"
    if not day.exists():
        pytest.skip("no generated report in this checkout")
    r = run("mcp__Gmail__send_message", send(subject="Metals Daybook · 📈 Wed 14 Oct · +₹17 (green)",
                                             htmlBody=day.read_text(encoding="utf-8"),
                                             body=day.with_name("2026-10-14.txt").read_text(encoding="utf-8")), root=ROOT)
    assert r.returncode == 0, r.stderr
