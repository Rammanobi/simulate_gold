"""Static checks of the engine-relay workflow and the places that start or hand over to it (style of test_redteam_workflows.py)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = {p.name: (p.read_text(encoding="utf-8"), yaml.safe_load(p.read_text(encoding="utf-8")))
      for p in (ROOT / ".github" / "workflows").glob("*.yml")}


def _on(doc):
    return doc.get("on", doc.get(True))


RELAY_TEXT, RELAY = WF["engine-relay.yml"]


def test_relay_has_its_own_concurrency_group_that_never_cancels():
    assert RELAY["concurrency"] == {"group": "engine-relay", "cancel-in-progress": False}
    groups = {n: (d.get("concurrency") or {}).get("group") for n, (_, d) in WF.items()}
    assert groups["engine-relay.yml"] not in {groups["market-hours.yml"], "paper-trader", "alert-state"}


def test_relay_timeout_is_under_the_six_hour_cap_and_permissions_are_minimal():
    job = RELAY["jobs"]["relay"]
    assert job["timeout-minutes"] <= 355 and job["timeout-minutes"] < 360
    assert RELAY["permissions"] == {"actions": "write", "contents": "read"}


def test_relay_has_dry_run_and_until_inputs_and_overnight_crons():
    inputs = _on(RELAY)["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False
    assert "until" in inputs
    crons = [c["cron"] for c in _on(RELAY)["schedule"]]
    assert {"30 15 * * *", "30 17 * * *", "30 21 * * *", "30 1 * * *"} <= set(crons)       # 21:00, 23:00, 03:00, 07:00 IST (every day)


def test_relay_never_trades_or_commits():
    code = "\n".join(ln.split("#")[0] for ln in RELAY_TEXT.splitlines())          # comments may mention run_live.py
    assert "run_live.py" not in code and "git push" not in code and "contents: write" not in code
    assert "secrets." not in code
    run = RELAY["jobs"]["relay"]["steps"][-1]["run"]
    assert "tools/engine_relay.py" in run and "--dry-run" in run and "--until" in run


def test_relay_user_inputs_reach_the_shell_only_through_env():
    for step in RELAY["jobs"]["relay"]["steps"]:
        assert "inputs." not in step.get("run", "")


def test_market_hours_hands_over_to_the_relay_after_1525_or_on_a_closed_day():
    steps = WF["market-hours.yml"][1]["jobs"]["trade"]["steps"]
    step = next(s for s in steps if "next session" in s.get("name", ""))
    run = step["run"]
    assert step["if"] == "always()" and steps[-2] is step
    assert "engine-relay.yml/dispatches" in run and "engine-relay.yml/runs" in run
    assert '[ "$TRADING" != "True" ] || [ "$NOW" -ge 1525 ]' in run
    assert 'select(.status != "completed")' in run                                  # guard: no relay already active
    assert run.index("engine-relay.yml/dispatches") < run.index("market-hours.yml/dispatches")
    assert "inputs[session]=auto" in run                                             # the in-day dispatch is unchanged


@pytest.mark.parametrize("name", ["report-retry.yml", "fallback.yml", "reminders.yml"])
def test_restart_sources_call_ensure_engine_and_can_dispatch(name):
    text, doc = WF[name]
    assert "python tools/ensure_engine.py" in text
    assert doc["permissions"]["actions"] == "write"
    job = next(iter(doc["jobs"].values()))
    step = next(s for s in job["steps"] if "ensure_engine" in s.get("run", ""))
    assert step.get("continue-on-error") is True                                       # a failed look-up must not break the real job
    env = {**job.get("env", {}), **step.get("env", {})}
    assert "GH_TOKEN" in env and "GITHUB_REPOSITORY" in env


def test_watchdog_dispatches_the_relay_when_relay_missing():
    text, doc = WF["watchdog.yml"]
    assert doc["permissions"]["actions"] == "write"
    step = next(s for s in doc["jobs"]["check"]["steps"] if "relay_missing" in s.get("name", ""))
    assert "relay_missing" in step["if"] and "engine-relay.yml/dispatches" in step["run"]
    assert 'select(.status != "completed")' in step["run"]
