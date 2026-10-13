"""Red-team bypass attempts against the Gmail send lock (.claude/hooks/gmail_guard.py), driven over stdin like Claude Code."""
import json
import re
from pathlib import Path

import pytest

from test_gmail_guard import ADDR, GUARD, ROOT, SUBJECT, run, send

pytestmark = pytest.mark.skipif(not GUARD.exists(), reason="the Gmail guard hook is not installed under .claude/hooks")


@pytest.mark.parametrize("over", [
    {"to": ["daybooK-test@example.invalid"]},                 # Kelvin sign: .lower() turns it into a plain 'k'
    {"to": [ADDR.replace("a", "а", 1)]},                       # Cyrillic 'a'
    {"to": [ADDR + "\n"]},                                          # trailing newline
    {"to": [ADDR + "\r\nBcc: evil@example.invalid"]},
    {"to": [ADDR + "\x00evil@example.invalid"]},
    {"to": {"0": ADDR}},
    {"subject": SUBJECT + "\r\nBcc: evil@example.invalid"},         # header injection through the subject
    {"subject": SUBJECT + "\n"},
    {"subject": "​Metals Daybook"},                            # zero-width space before the prefix
    {"CC": ["evil@example.invalid"]},
    {"Bcc": ["evil@example.invalid"]},
    {"to ": [ADDR]},
])
def test_bypass_attempts_are_blocked(over):
    r = run("mcp__Gmail__send_message", send(**over))
    assert r.returncode == 2 and "gmail_guard" in r.stderr


def test_outer_spaces_and_case_are_still_fine():
    assert run("mcp__Gmail__send_message", send(to=["  " + ADDR.upper() + " "])).returncode == 0


@pytest.mark.parametrize("env", [ADDR + ", evil@example.invalid", ADDR + ";evil@example.invalid", "Owner <%s>" % ADDR,
                                 ADDR + " evil@example.invalid", "not-an-address"])
def test_report_email_must_be_one_plain_address(env):
    r = run("mcp__Gmail__send_message", send(to=[env]), addr=env)
    assert r.returncode == 2 and "REPORT_EMAIL" in r.stderr


@pytest.mark.parametrize("tool", ["mcp__Gmail__get_and_send", "mcp__Gmail__list_and_delete", "mcp__Gmail__search_then_forward",
                                  "mcp__GMAIL__create_draft", "mcp__gmail__GMAIL_SEND_DRAFT"])
def test_write_tools_disguised_as_reads_are_blocked(tool):
    assert run(tool, {}).returncode == 2


@pytest.mark.parametrize("tool", ["mcp__Gmail__get_thread", "mcp__Gmail__search_threads", "mcp__Gmail__list_labels"])
def test_real_read_tools_still_work(tool):
    assert run(tool, {"query": "x"}).returncode == 0


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", '{"tool_name": 5}', "\xff"])
def test_malformed_hook_input_blocks(raw):
    assert run("x", raw=raw).returncode == 2


def test_hook_matcher_covers_any_case_of_gmail():
    m = json.loads((ROOT / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"][0]["matcher"]
    for name in ("mcp__Gmail__send_message", "mcp__GMAIL__send_message", "mcp__gmail__send_message",
                 "mcp__claude_ai_Gmail__send_message", "mcp__GMail__forward"):
        assert re.fullmatch(m, name), name
