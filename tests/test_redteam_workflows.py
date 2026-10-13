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
