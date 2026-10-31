"""Static red-team review of .github/workflows: permissions, concurrency, UTC-vs-IST schedules, secrets, loops."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = {p.name: (p.read_text(encoding="utf-8"), yaml.safe_load(p.read_text(encoding="utf-8")))
      for p in (ROOT / ".github" / "workflows").glob("*.yml")}
IST = timezone(timedelta(hours=5, minutes=30))


def _on(doc):
    return doc.get("on", doc.get(True))        # PyYAML reads the bare key `on` as True


def ist(cron: str) -> str:
    m, h = cron.split()[:2]
    t = datetime(2026, 10, 14, int(h), int(m), tzinfo=timezone.utc).astimezone(IST)
    return t.strftime("%H:%M")


@pytest.mark.parametrize("name", sorted(WF))
def test_every_workflow_declares_least_privilege_permissions(name):
    doc = WF[name][1]
    assert "permissions" in doc, f"{name} would inherit the repository's default token permissions"
    perms = doc["permissions"]
    assert perms != "write-all"
    if name == "tests.yml":
        assert perms == {"contents": "read"}


def test_schedules_mean_what_the_comments_say_in_ist():
    sched = lambda n: [c["cron"] for c in _on(WF[n][1])["schedule"]]
    assert [ist(c) for c in sched("market-hours.yml")] == ["09:00", "09:10", "12:15"]
    assert all(c.endswith("* * 1-5") for c in sched("market-hours.yml"))
    assert ist(sched("backtest.yml")[0]) == "08:00"
    wd = sched("watchdog.yml")
    assert wd[0] == "*/10 3-10 * * 1-5" and ist("0 3") == "08:30" and ist("50 10") == "16:20" and ist(wd[1]) == "16:30"
    a = sched("archive.yml")[0]
    assert a == "30 20 * * 6" and ist(a) == "02:00"           # Saturday 20:30 UTC = Sunday 02:00 IST


def test_engine_and_writers_of_shared_state_are_serialised():
    groups = {n: (d.get("concurrency") or {}).get("group") for n, (_, d) in WF.items()}
    assert groups["market-hours.yml"] == groups["archive.yml"] == "paper-trader"
    assert groups["watchdog.yml"] == groups["reminders.yml"] == groups["incident.yml"] == "alert-state"
    for n in ("market-hours.yml", "archive.yml", "watchdog.yml", "reminders.yml", "incident.yml"):
        assert WF[n][1]["concurrency"]["cancel-in-progress"] is False


def test_incident_workflow_cannot_trigger_itself():
    doc = WF["incident.yml"][1]
    watched = _on(doc)["workflow_run"]["workflows"]
    assert doc["name"] not in watched and set(watched) == {"market-hours paper trading", "tests"}


@pytest.mark.parametrize("name", ["market-hours.yml", "watchdog.yml", "reminders.yml", "incident.yml"])
def test_writers_of_alerts_json_configure_the_json_merge_driver(name):
    assert "merge.simgold-alerts.driver" in WF[name][0]


def test_final_sync_uses_the_robust_engine_sync():
    steps = WF["market-hours.yml"][1]["jobs"]["trade"]["steps"]
    final = [s for s in steps if s.get("name") == "Final sync"][0]
    assert final["if"] == "always()" and "gitsync.sync" in final["run"]


@pytest.mark.parametrize("name", sorted(WF))
def test_secrets_are_only_passed_through_env_and_never_printed(name):
    text = WF[name][0]
    for line in text.splitlines():
        if "secrets." in line:
            assert line.strip().split(":")[0].isupper() or line.strip().startswith("#"), line   # `NAME: ${{ secrets.X }}`
        if "echo" in line:          # names may be mentioned; values ($ROUTINE_..., secrets.X) never printed
            assert "$ROUTINE_" not in line and "${ROUTINE_" not in line and "secrets." not in line, line


def test_missing_routine_secrets_are_handled():
    assert 'if [ -z "$ROUTINE_INCIDENT_URL" ] || [ -z "$ROUTINE_INCIDENT_TOKEN" ]' in WF["incident.yml"][0]
    from simgold import alerts, fastlane
    assert fastlane.fire({}, {"x": 1}) == (False, "ROUTINE_FIRE_URL / ROUTINE_FIRE_TOKEN not set")
    assert alerts.post_routine("x", "", "") is False                       # used to raise ValueError
    assert alerts.post_routine("x", "example.invalid/fire", "tok") is False   # secret pasted without https://


def test_fire_strips_whitespace_from_pasted_secrets():
    from simgold import fastlane
    seen = {}

    def post(url, headers, body, timeout=10):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return 200
    env = {"ROUTINE_FIRE_URL": " https://api.anthropic.com/v1/x/fire\n", "ROUTINE_FIRE_TOKEN": "tok123 \n"}
    assert fastlane.fire(env, {"x": 1}, post=post) == (True, "HTTP 200")
    assert seen == {"url": "https://api.anthropic.com/v1/x/fire", "auth": "Bearer tok123"}
    assert fastlane.fire({"ROUTINE_FIRE_URL": "  ", "ROUTINE_FIRE_TOKEN": "t"}, {})[0] is False

def test_engine_kick_starts_the_engine_on_a_routine_guidance_push():
    """15 Oct 2026: the premarket dispatch was denied and the 09:00/09:10 crons ran ~5 h late (first tick 10:18 IST).
    The premarket push of state/guidance.json must start the engine from GitHub's side."""
    text, doc = WF["engine-kick.yml"]
    on = _on(doc)
    assert on["push"]["branches"] == ["main"] and on["push"]["paths"] == ["state/guidance.json"]
    assert "schedule" not in on                                  # schedules are what ran late
    assert doc["permissions"] == {"actions": "write", "contents": "read"}
    assert doc["concurrency"] == {"group": "engine-kick", "cancel-in-progress": False}
    run = doc["jobs"]["kick"]["steps"][-1]["run"]
    assert "python -m simgold.enginekick --active" in run and 'if [ "$rc" = "0" ]' in run
    assert run.count("/dispatches") == 1
    assert "actions/workflows/market-hours.yml/dispatches\" -f ref=main -f 'inputs[session]=auto'" in run
    assert "select(.status != \"completed\")" in run             # any unfinished run (queued/pending too) blocks it
    assert "git push" not in text and "contents: write" not in text


def test_engine_kick_path_is_one_the_premarket_routine_commits():
    premarket = (ROOT / "routines" / "premarket.md").read_text(encoding="utf-8")
    step8 = premarket[premarket.index("**Commit and push**"):]
    assert "state/guidance.json" in step8.split("```")[1]
    assert "engine-kick.yml" in premarket


def test_report_retry_backup_email_step_is_gated_and_uses_the_right_secrets():
    text, doc = WF["report-retry.yml"]
    steps = doc["jobs"]["retry"]["steps"]
    b = next(s for s in steps if s.get("name", "").startswith("Backup email"))
    assert b["if"] == "steps.decide.outputs.verdict == 'BACKUP' || steps.decide.outputs.verdict == 'GIVE_UP' || steps.decide.outputs.verdict == 'FORCE_BACKUP'"
    assert b["env"]["SMTP_USER"] == "${{ secrets.SMTP_USER }}"
    assert b["env"]["SMTP_APP_PASSWORD"] == "${{ secrets.SMTP_APP_PASSWORD }}"
    assert b["env"]["REPORT_EMAIL"] == "${{ secrets.REPORT_EMAIL }}"
    assert b["env"]["DEEPSEEK_API_KEY"] == "${{ secrets.DEEPSEEK_API_KEY }}"
    run = b["run"]
    assert 'status --date "$DAY")" = "backup_sent"' in run and '"$VERDICT" != "FORCE_BACKUP"' in run                      # never twice
    assert "Backup email not configured: add SMTP_USER, SMTP_APP_PASSWORD, REPORT_EMAIL secrets" in run
    assert run.index("tools/backup_email.py") < run.index("mark-backup")
    assert "--dry-run" not in run and "echo \"$SMTP" not in run and "echo \"$REPORT_EMAIL" not in run
    # the secrets live only on that step, not on the job (the incident wake step never sees them)
    assert "SMTP_APP_PASSWORD" not in str(doc["jobs"]["retry"]["env"])
    assert _on(doc)["workflow_dispatch"]["inputs"]["force_backup"]["type"] == "boolean"
    commit = next(s for s in steps if s.get("name") == "Commit the retry state")
    assert commit["if"] == "always()" and "state/report_retry.json" in commit["run"]


def test_push_workflows_ignore_the_live_branch_and_market_hours_enables_the_live_push():
    """The engine pushes the live P&L to branch `live` every minute: nothing may run on that push."""
    for name, (_, doc) in WF.items():
        push = (_on(doc) or {}).get("push")
        if name == "tests.yml" or push is not None:
            push = push or {}
            ok = push.get("branches") == ["main"] or "live" in (push.get("branches-ignore") or [])
            assert ok, f"{name} would run on every push to the live branch"
    env = WF["market-hours.yml"][1]["jobs"]["trade"]["env"]
    assert str(env["SIMGOLD_LIVE_PUSH"]) == "1" and str(env["SIMGOLD_GIT_SYNC"]) == "1"


def test_market_hours_dispatches_its_own_next_session_safely():
    """29 Oct 2026: the engine starts the next session itself (the hourly routine moved to one 14:40 run)."""
    doc = WF["market-hours.yml"][1]
    assert doc["permissions"]["actions"] == "write"
    assert doc["concurrency"]["cancel-in-progress"] is False and doc["concurrency"]["group"] == "paper-trader"
    steps = doc["jobs"]["trade"]["steps"]
    step = next(s for s in steps if "next session" in s.get("name", ""))
    assert steps[-2] is step and steps[-1]["name"].startswith("Fail the run") and step["if"] == "always()"
    assert doc["jobs"]["trade"]["env"]["GH_TOKEN"] == "${{ github.token }}"          # the job's GITHUB_TOKEN, no PAT
    run = step["run"]
    assert "is_trading_day" in run and '"$NOW" -ge 1525' in run                       # trading day + time guard
    assert "$GITHUB_RUN_ID" in run and 'status != \\"completed\\"' in run and '"$OTHERS" != "0"' in run   # duplicate guard
    assert "actions/workflows/market-hours.yml/dispatches" in run and "-f ref=main" in run
    assert run.count("GITHUB_STEP_SUMMARY") >= 4                                      # every decision is logged
    assert "secrets." not in run


def test_report_retry_has_the_1630_backup_crons():
    doc = WF["report-retry.yml"][1]
    crons = [c["cron"] for c in _on(doc)["schedule"]]
    assert "0,15,30 11 * * *" in crons and "0 12 * * *" in crons
    assert [ist(f"{m} 11") for m in (0, 15, 30)] == ["16:30", "16:45", "17:00"] and ist("0 12") == "17:30"
