"""Engine relay (simgold/relay.py, tools/engine_relay.py): next start, hop planning, guards, dry run. No network: fake GitHub API."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from simgold import relay
from simgold.market import IST

ROOT = Path(__file__).resolve().parent.parent


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class FakeApi:
    """Records POSTs; GET returns the configured runs per workflow."""

    def __init__(self, runs=None, fail_list=False, fail_post=False):
        self.runs = runs or {}
        self.posts: list[tuple[str, dict]] = []
        self.fail_list, self.fail_post = fail_list, fail_post

    def __call__(self, method, path, body=None):
        if method == "GET":
            if self.fail_list:
                raise OSError("api down")
            wf = path.split("/workflows/")[1].split("/")[0]
            return {"workflow_runs": self.runs.get(wf, [])}
        if self.fail_post:
            raise OSError("dispatch refused")
        wf = path.split("/workflows/")[1].split("/")[0]
        self.posts.append((wf, body))
        return {}

    def dispatched(self):
        return [w for w, _ in self.posts]


def run_(status="in_progress", id_=1):
    return {"id": id_, "status": status}


def make(api, now, **kw):
    out: list[str] = []
    kw.setdefault("root", ROOT)
    r = relay.Relay(api, "o/r", kw.pop("run_id", "100"), relay.VirtualClock(now), kw.pop("dry_run", False), out=out.append, **kw)
    r.lines = out
    return r


# ---------------------------------------------------------------- next start
def test_next_start_after_close_goes_to_next_morning():
    assert relay.next_trading_start(at(2026, 11, 12, 15, 40)) == at(2026, 11, 13, 8, 55)        # Thu -> Fri


def test_friday_evening_goes_to_monday():
    assert relay.next_trading_start(at(2026, 11, 13, 15, 40)) == at(2026, 11, 16, 8, 55)        # Fri -> Mon
    assert relay.next_trading_start(at(2026, 11, 14, 12, 0)) == at(2026, 11, 16, 8, 55)         # Saturday
    assert relay.next_trading_start(at(2026, 11, 15, 23, 59)) == at(2026, 11, 16, 8, 55)        # Sunday


def test_holidays_are_skipped():
    assert relay.next_trading_start(at(2026, 11, 9, 16, 0)) == at(2026, 11, 11, 8, 55)          # 10 Nov holiday
    assert relay.next_trading_start(at(2026, 11, 23, 16, 0)) == at(2026, 11, 25, 8, 55)         # 24 Nov holiday
    assert relay.next_trading_start(at(2026, 12, 24, 16, 0)) == at(2026, 12, 28, 8, 55)         # 25 Dec holiday + weekend


def test_today_counts_until_the_last_recheck():
    assert relay.next_trading_start(at(2026, 11, 13, 6, 0)) == at(2026, 11, 13, 8, 55)
    assert relay.next_trading_start(at(2026, 11, 13, 10, 0)) == at(2026, 11, 13, 8, 55)         # day phase still open
    assert relay.next_trading_start(at(2026, 11, 13, 12, 20)) == at(2026, 11, 16, 8, 55)        # last check passed
    assert relay.next_trading_start(at(2026, 11, 10, 7, 0)) == at(2026, 11, 11, 8, 55)          # holiday morning


# ---------------------------------------------------------------- hop planning
def test_plan_hop_never_exceeds_the_budget_or_the_target():
    assert relay.plan_hop(at(2026, 11, 12, 15, 40), at(2026, 11, 13, 8, 55)) == (5 * 3600 + 45 * 60, False)
    assert relay.plan_hop(at(2026, 11, 13, 3, 25), at(2026, 11, 13, 8, 55)) == (5.5 * 3600, True)
    assert relay.plan_hop(at(2026, 11, 13, 9, 0), at(2026, 11, 13, 8, 55)) == (0.0, True)


def test_weekend_is_a_chain_of_hops_each_within_5h45():
    now, target, hops = at(2026, 11, 13, 15, 40), at(2026, 11, 16, 8, 55), 0
    while True:
        sec, final = relay.plan_hop(now, target)
        assert sec <= relay.HOP_MAX.total_seconds()
        now += timedelta(seconds=sec)
        hops += 1
        if final:
            break
    assert now == target and 11 <= hops <= 13


def test_pending_events_collapse_passed_ones_into_one_catch_up():
    ev = relay.day_events(at(2026, 11, 13, 8, 55))
    assert [e.kind for e in relay.pending_events(at(2026, 11, 13, 7, 0), ev)] == ["start", "check1", "check2"]
    got = relay.pending_events(at(2026, 11, 13, 10, 0), ev)
    assert [(e.kind, e.at) for e in got] == [("check1", at(2026, 11, 13, 10, 0)), ("check2", at(2026, 11, 13, 12, 20))]
    assert relay.pending_events(at(2026, 11, 13, 13, 0), ev) == []


# ---------------------------------------------------------------- guards
def test_start_of_day_dispatches_the_engine_once_and_stays_for_both_rechecks():
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 8, 54))
    assert r.run() == "done"
    assert api.posts == [("market-hours.yml", {"ref": "main", "inputs": {"session": "auto"}})] * 3
    assert r.clock.now() == at(2026, 11, 13, 12, 20)


def test_engine_already_running_means_no_dispatch():
    api = FakeApi({"market-hours.yml": [run_("in_progress", 7)]})
    r = make(api, at(2026, 11, 13, 8, 54))
    assert r.run() == "done" and api.posts == []
    assert any("already running" in ln for ln in r.lines)


def test_a_queued_engine_run_counts_as_active():
    api = FakeApi({"market-hours.yml": [run_("queued", 7)]})
    make(api, at(2026, 11, 13, 8, 54)).run()
    assert api.posts == []


def test_only_the_missing_recheck_dispatches():
    """Morning run dies at 10:00: the 12:20 check (and only it) starts the engine again."""
    class Flaky(FakeApi):
        def __call__(self, method, path, body=None):
            if method == "GET" and "market-hours" in path:
                self.runs["market-hours.yml"] = [run_()] if relay_now()[0] < at(2026, 11, 13, 10, 0) else []
            return super().__call__(method, path, body)
    holder = {}
    relay_now = lambda: (holder["r"].clock.now(),)
    api = Flaky()
    holder["r"] = make(api, at(2026, 11, 13, 8, 54))
    holder["r"].run()
    assert api.dispatched() == ["market-hours.yml"]


@pytest.mark.parametrize("day", [(2026, 11, 10), (2026, 11, 24), (2026, 12, 25), (2026, 11, 14), (2026, 11, 15)])
def test_holiday_or_weekend_never_dispatches_the_engine(day):
    api = FakeApi()
    r = make(api, at(*day, 8, 54), until=at(*day, 8, 55))          # the `until` override forces the day phase on a closed day
    r.run()
    assert api.posts == []
    assert any("not a trading day" in ln for ln in r.lines)


def test_normal_run_over_a_holiday_waits_for_the_next_trading_day():
    api = FakeApi()
    r = make(api, at(2026, 11, 9, 15, 40))                          # Mon evening, Tue 10 Nov is a holiday
    out = r.run()
    assert out.startswith("handover") and api.dispatched() == ["engine-relay.yml"]
    assert r.clock.now() == at(2026, 11, 9, 15, 40) + relay.HOP_MAX


def test_own_run_id_is_excluded_from_the_other_relay_guard():
    api = FakeApi({"engine-relay.yml": [run_("in_progress", 100)]})        # that is me
    r = make(api, at(2026, 11, 9, 15, 40), run_id="100")
    assert r.run().startswith("handover:dispatched")
    assert api.dispatched() == ["engine-relay.yml"]


def test_another_in_progress_relay_makes_a_duplicate_exit():
    api = FakeApi({"engine-relay.yml": [run_("in_progress", 55), run_("in_progress", 100)]})
    r = make(api, at(2026, 11, 9, 15, 40), run_id="100")
    assert r.run() == "duplicate" and api.posts == []


def test_at_most_one_pending_relay():
    api = FakeApi({"engine-relay.yml": [run_("in_progress", 100), run_("queued", 101)]})
    r = make(api, at(2026, 11, 9, 15, 40), run_id="100")
    assert r.run() == "handover:pending-exists" and api.posts == []


def test_catch_up_when_started_in_the_day_phase():
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 10, 0))
    assert r.run() == "done"
    assert api.dispatched() == ["market-hours.yml", "market-hours.yml"]      # immediate catch-up + the 12:20 check


def test_run_list_failure_still_dispatches_the_engine():
    api = FakeApi(fail_list=True)
    r = make(api, at(2026, 11, 13, 8, 54))
    r.run()
    assert "market-hours.yml" in api.dispatched()


def test_dispatch_failure_does_not_crash():
    api = FakeApi(fail_post=True)
    assert make(api, at(2026, 11, 13, 8, 54)).run() == "done"


# ---------------------------------------------------------------- dry run
def test_dry_run_dispatches_nothing_and_does_not_sleep():
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 8, 54), dry_run=True)
    assert r.run() == "done"
    assert api.posts == []
    assert r.actions == ["market-hours.yml"] * 3
    assert sum("[dry-run] would dispatch" in ln for ln in r.lines) == 3


def test_dry_run_hop_does_not_dispatch_the_successor():
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 15, 40), dry_run=True)
    assert r.run().startswith("handover") and api.posts == [] and r.actions == ["engine-relay.yml"]


# ---------------------------------------------------------------- morning email (optional workflow + param)
def test_morning_email_needs_the_workflow_file_and_the_param(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    on, off = {"report": {"morning_email": True}}, {"report": {}}
    assert not relay.morning_email_enabled(tmp_path, on)                         # no workflow file
    (tmp_path / ".github" / "workflows" / "morning-email.yml").write_text("name: x\n")
    assert relay.morning_email_enabled(tmp_path, on)
    assert not relay.morning_email_enabled(tmp_path, off) and not relay.morning_email_enabled(tmp_path, {})
    assert not relay.morning_email_enabled(tmp_path, {"report": None})


def test_morning_email_is_dispatched_only_at_start_and_only_when_enabled(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "morning-email.yml").write_text("name: x\n")
    api = FakeApi()
    make(api, at(2026, 11, 13, 8, 54), root=tmp_path, params={"report": {"morning_email": True}}).run()
    assert api.dispatched().count("morning-email.yml") == 1
    api2 = FakeApi()
    make(api2, at(2026, 11, 13, 8, 54), root=tmp_path, params={"report": {}}).run()
    assert "morning-email.yml" not in api2.dispatched()
    api3 = FakeApi()                                                              # a catch-up at 11:00 is too late for a morning email
    make(api3, at(2026, 11, 13, 11, 0), root=tmp_path, params={"report": {"morning_email": True}}).run()
    assert "morning-email.yml" not in api3.dispatched()


def test_missing_report_section_never_raises():
    assert relay.morning_email_enabled(ROOT, {"strategy": {}}) is False


# ---------------------------------------------------------------- CLI
def _cli():
    spec = importlib.util.spec_from_file_location("engine_relay_cli", ROOT / "tools" / "engine_relay.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["engine_relay_cli"] = m
    spec.loader.exec_module(m)
    return m


def test_cli_rejects_a_bad_until(capsys):
    assert _cli().main(["--until", "tomorrow-ish"]) == 2


def test_parse_until():
    assert relay.parse_until("") is None
    assert relay.parse_until("2026-11-13T08:55") == at(2026, 11, 13, 8, 55)
    assert relay.parse_until("2026-11-13T03:25:00Z") == at(2026, 11, 13, 8, 55)
