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
    assert relay.next_trading_start(at(2026, 11, 13, 12, 20)) == at(2026, 11, 13, 8, 55)        # 12:20 is no longer the end
    assert relay.next_trading_start(at(2026, 11, 13, 15, 20)) == at(2026, 11, 16, 8, 55)        # last check passed
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
    assert [e.kind for e in relay.pending_events(at(2026, 11, 13, 7, 0), ev)] == ["start"] + [f"check{i}" for i in range(1, 7)]
    got = relay.pending_events(at(2026, 11, 13, 10, 0), ev)
    assert (got[0].kind, got[0].at, got[0].catch_up) == ("check1", at(2026, 11, 13, 10, 0), True)
    assert [e.at for e in got[1:]] == [at(2026, 11, 13, h, m) for h, m in ((12, 20), (12, 30), (13, 30), (14, 30), (15, 20))]
    assert all(not e.catch_up for e in got[1:])
    assert relay.pending_events(at(2026, 11, 13, 15, 30), ev) == []
    assert [e.at.strftime("%H:%M") for e in ev][:4] == ["08:55", "09:30", "12:20", "12:30"]         # 12:30 re-check: a failed afternoon hand-over


# ---------------------------------------------------------------- guards
def test_start_of_day_dispatches_the_engine_and_rechecks_then_hands_over_within_the_budget():
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 8, 54))
    assert r.run() == "handover:dispatched"                          # 08:54 + 5h45 = 14:39: the 15:20 check is for the next hop
    assert api.posts[0] == ("market-hours.yml", {"ref": "main", "inputs": {"session": "auto"}})
    assert api.dispatched()[-1] == "engine-relay.yml" and api.dispatched().count("market-hours.yml") == 6
    assert r.clock.now() == at(2026, 11, 13, 14, 39)


def test_last_check_hands_the_chain_over_to_the_next_relay():
    api = FakeApi({"market-hours.yml": [run_("in_progress", 7)]})
    r = make(api, at(2026, 11, 13, 9, 40))                           # deadline 15:25: reaches the 15:20 check
    assert r.run() == "done"
    assert api.dispatched() == ["engine-relay.yml"] and r.clock.now() == at(2026, 11, 13, 15, 20)


def test_relay_covers_the_window_between_1220_and_1536():
    """No gap: a relay (or the engine) is alive at 12:20, 12:30, 13:30, 14:30 and 15:20, and the last one hands over."""
    ev = relay.day_events(at(2026, 11, 13, 8, 55))
    assert max(b.at - a.at for a, b in zip(ev, ev[1:])) <= timedelta(hours=3)
    assert ev[-1].at == at(2026, 11, 13, 15, 20)


def test_engine_already_running_means_no_dispatch():
    api = FakeApi({"market-hours.yml": [run_("in_progress", 7)]})
    r = make(api, at(2026, 11, 13, 9, 40))
    assert r.run() == "done" and api.dispatched() == ["engine-relay.yml"]         # only the hand-over, never the engine
    assert any("already running" in ln for ln in r.lines)


def test_a_queued_engine_run_counts_as_active():
    api = FakeApi({"market-hours.yml": [run_("queued", 7)]})
    make(api, at(2026, 11, 13, 9, 40)).run()
    assert "market-hours.yml" not in api.dispatched()


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
    first = next(t for t, w in holder["r"].timeline if w == "market-hours.yml")
    assert first == at(2026, 11, 13, 12, 20)                        # 08:55 and 09:30 saw a live run; 12:20 is the first to dispatch


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
    assert api.dispatched()[:3] == ["market-hours.yml"] * 3                   # immediate catch-up + the 12:20 and 12:30 checks
    assert r.timeline[0][0] == at(2026, 11, 13, 10, 0)


def test_run_list_failure_still_dispatches_the_engine():
    api = FakeApi(fail_list=True)
    r = make(api, at(2026, 11, 13, 8, 54))
    r.run()
    assert "market-hours.yml" in api.dispatched()


def test_dispatch_failure_does_not_crash():
    api = FakeApi(fail_post=True)
    assert make(api, at(2026, 11, 13, 8, 54)).run() == "handover:failed"


# ---------------------------------------------------------------- dry run
def test_dry_run_dispatches_nothing_and_does_not_sleep():
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 8, 54), dry_run=True)
    assert r.run() == "handover:test-run"                           # a dry run never hands over
    assert api.posts == []
    assert r.actions == ["market-hours.yml"] * 6
    assert sum("[dry-run] would dispatch" in ln for ln in r.lines) == 6


def test_dry_run_hop_does_not_dispatch_the_successor():
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 15, 40), dry_run=True)
    assert r.run().startswith("handover") and api.posts == [] and r.actions == []


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


# ---------------------------------------------------------------- review blockers (PR 57)
def live(status, id_, title="engine-relay"):
    return {"id": id_, "status": status, "display_title": title}


def test_blocker1_a_pending_dry_run_is_not_the_live_hops_successor():
    """Repro: live hop = run 7, a manual dry run 8 pending. The hop must still dispatch the next live relay."""
    api = FakeApi({"engine-relay.yml": [live("in_progress", 7), live("queued", 8, "engine-relay TEST")]})
    r = make(api, at(2026, 11, 9, 15, 40), run_id="7")
    assert r.run() == "handover:dispatched"
    assert api.dispatched() == ["engine-relay.yml"]


def test_blocker1_a_live_pending_relay_still_counts():
    api = FakeApi({"engine-relay.yml": [live("in_progress", 7), live("queued", 8)]})
    assert make(api, at(2026, 11, 9, 15, 40), run_id="7").run() == "handover:pending-exists" and api.posts == []


def test_blocker1_test_runs_never_hand_over_nor_exit_as_duplicates():
    api = FakeApi({"engine-relay.yml": [live("in_progress", 7)]})                      # the live chain is running
    r = make(api, at(2026, 11, 9, 15, 40), run_id="8", dry_run=True)
    assert r.run() == "handover:test-run" and api.posts == []
    r2 = make(api, at(2026, 11, 9, 15, 40), run_id="9", until=at(2026, 11, 9, 15, 45))
    assert r2.run().startswith("handover") and "engine-relay.yml" not in api.dispatched()


def test_blocker1_live_run_ignores_a_running_test_run_for_the_duplicate_guard():
    api = FakeApi({"engine-relay.yml": [live("in_progress", 8, "engine-relay TEST"), live("in_progress", 7)]})
    assert make(api, at(2026, 11, 9, 15, 40), run_id="7").run() == "handover:dispatched"


def test_blocker2_morning_email_is_sent_once_across_the_hop_chain(tmp_path):
    """Repro timeline: hop starting 03:11:30 reaches 08:55 on time (email), its budget ends 08:56:30, the next hop's catch-up start must not mail again."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "morning-email.yml").write_text("name: x\n")
    params = {"report": {"morning_email": True}}
    start = at(2026, 11, 13, 3, 11) + timedelta(seconds=30)
    api = FakeApi()
    h1 = make(api, start, root=tmp_path, params=params, run_id="1")
    assert h1.run().startswith("handover")
    assert h1.clock.now() == start + relay.HOP_MAX == at(2026, 11, 13, 8, 56) + timedelta(seconds=30)
    assert [(t.strftime("%H:%M:%S"), w) for t, w in h1.timeline[:3]] == [
        ("08:55:00", "market-hours.yml"), ("08:55:00", "morning-email.yml"), ("08:56:30", "engine-relay.yml")]
    h2 = make(api, at(2026, 11, 13, 8, 57) + timedelta(seconds=45), root=tmp_path, params=params, run_id="2")
    h2.run()
    assert [w for _, w in h1.timeline + h2.timeline].count("morning-email.yml") == 1
    assert "morning-email.yml" not in [w for _, w in h2.timeline]


def test_blocker2_a_catch_up_start_never_mails_even_seconds_after_0855(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "morning-email.yml").write_text("name: x\n")
    api = FakeApi()
    r = make(api, at(2026, 11, 13, 8, 56), root=tmp_path, params={"report": {"morning_email": True}})
    r.run()
    assert "morning-email.yml" not in api.dispatched() and "market-hours.yml" in api.dispatched()


def test_until_validation():
    now = at(2026, 11, 13, 10, 0)
    assert relay.parse_until("2026-11-16T08:55", now) == at(2026, 11, 16, 8, 55)
    for bad in ("2026-11-13T09:00", "2027-01-01T08:55", "junk"):
        with pytest.raises(ValueError):
            relay.parse_until(bad, now)
