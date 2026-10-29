"""simgold.runguard: an engine run exits when an older run of the same workflow is already in progress."""
from __future__ import annotations

from simgold import runguard

ENV = {"GITHUB_RUN_ID": "200", "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "x"}


def fake(runs, me=None):
    me = me or {"id": 200, "workflow_id": 7, "created_at": "2026-10-14T04:46:47Z"}
    calls = []

    def fetch(path):
        calls.append(path)
        if path == "repos/o/r/actions/runs/200":
            return me
        assert path.startswith("repos/o/r/actions/workflows/7/runs?status=in_progress")
        return {"workflow_runs": runs}
    fetch.calls = calls
    return fetch


def test_older_run_in_progress_wins():
    older = {"id": 100, "created_at": "2026-10-14T04:46:23Z", "event": "workflow_dispatch"}
    me = {"id": 200, "workflow_id": 7, "created_at": "2026-10-14T04:46:47Z"}
    assert runguard.older_engine_run(ENV, fake([older, me], me))["id"] == 100


def test_newer_run_or_only_me_does_not_stop_this_run():
    me = {"id": 200, "workflow_id": 7, "created_at": "2026-10-14T04:46:47Z"}
    newer = {"id": 300, "created_at": "2026-10-14T05:00:00Z"}
    assert runguard.older_engine_run(ENV, fake([me, newer], me)) is None


def test_fails_open_outside_actions_and_on_errors():
    assert runguard.older_engine_run({}, fake([])) is None
    assert runguard.older_engine_run({"GITHUB_RUN_ID": "200", "GITHUB_REPOSITORY": "o/r"}) is None    # no token

    def boom(path):
        raise OSError("network down")
    assert runguard.older_engine_run(ENV, boom) is None


def test_run_live_checks_the_guard_before_the_lock_and_pulls_main():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "run_live.py").read_text()
    assert src.index("runguard.older_engine_run") < src.index("acquire_lock(STATE") < src.index("gitsync.pull_latest")
    assert src.index("gitsync.pull_latest") < src.index("live = Live(")
    wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "market-hours.yml").read_text()
    assert "ref: main" in wf and "actions: write" in wf     # write includes the read the guard needs; 29 Oct 2026 and "GH_TOKEN" in wf
