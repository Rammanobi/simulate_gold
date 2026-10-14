"""The routines' GitHub Actions and git push commands must match the pre-approved rules in .claude/settings.json.

14 Oct 2026: the premarket routine's `gh api -X POST .../dispatches` calls were denied by the auto-mode permission
check, so the engine never started in the morning. The rules are prefix rules (`Bash(prefix:*)`), so the playbooks
must use exactly these command shapes: no pipes, `;`, `&&`, loops or `2>&1` around them."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = json.loads((ROOT / ".claude" / "settings.json").read_text())
RULES = [r[len("Bash("):-1] for r in SETTINGS["permissions"]["allow"] if r.startswith("Bash(")]


def allowed(cmd: str) -> bool:
    for r in RULES:
        if r.endswith(":*") and cmd.startswith(r[:-2]):
            return True
        if cmd == r:
            return True
    return False


def playbook_commands(pattern: str) -> list[tuple[str, str]]:
    out = []
    for md in sorted((ROOT / "routines").glob("*.md")):
        for span in re.findall(r"`([^`\n]+)`", md.read_text(encoding="utf-8")):
            if re.match(pattern, span):
                out.append((md.name, span.strip()))
        for block in re.findall(r"```bash\n(.*?)```", md.read_text(encoding="utf-8"), re.S):
            for line in block.splitlines():
                if re.match(pattern, line.strip()):
                    out.append((md.name, line.strip()))
    return out


def test_the_gmail_hook_is_still_there():
    pre = SETTINGS["hooks"]["PreToolUse"]
    assert any("gmail_guard.py" in h["command"] for m in pre for h in m["hooks"])


def test_rules_cover_the_three_dispatches_and_the_run_reads():
    for wf in ("market-hours", "watchdog", "reminders"):
        assert allowed(f"gh api -X POST repos/Rammanobi/simulate_gold/actions/workflows/{wf}.yml/dispatches -f ref=main")
    assert allowed('gh api "repos/Rammanobi/simulate_gold/actions/runs?status=in_progress&per_page=5" --jq .total_count')
    assert allowed("gh api repos/Rammanobi/simulate_gold/actions/runs/123/jobs")
    assert not allowed("gh api -X POST repos/Rammanobi/simulate_gold/actions/workflows/fallback.yml/dispatches -f ref=main")
    assert not allowed("gh api -X DELETE repos/Rammanobi/simulate_gold/actions/runs/123")
    assert not allowed("git push --force origin HEAD:main")


def test_every_actions_command_in_the_playbooks_is_pre_approved():
    cmds = playbook_commands(r"gh api .*actions/")
    assert len(cmds) >= 8
    bad = [(f, c) for f, c in cmds if not allowed(c)]
    assert not bad, bad


def test_dispatches_are_never_wrapped_in_loops_or_pipes():
    for md in (ROOT / "routines").glob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert "for s in in_progress queued" not in text, md.name
        assert "for i in 1 2 3" not in text, md.name
        for line in text.splitlines():
            if "/dispatches" in line:
                assert "&&" not in line and "2>&1" not in line and "; do" not in line and "|" not in line, (md.name, line)


def test_push_and_pull_commands_match_the_rules():
    pushes = playbook_commands(r"git push")
    assert pushes and all(c == "git push origin HEAD:main" for _, c in pushes), pushes
    pulls = playbook_commands(r"git pull")
    assert pulls and all(allowed(c) for _, c in pulls), pulls


def test_the_afternoon_dispatch_is_skipped_when_a_run_is_waiting():
    hourly = (ROOT / "routines" / "hourly.md").read_text(encoding="utf-8")
    assert "inputs[session]=afternoon" in hourly
    i = hourly.index("At the 12:15 run only")
    assert "queued" in hourly[i:i + 900] and "pending" in hourly[i:i + 900] and "skip" in hourly[i:i + 900]
