"""Red-team regression tests for the alert policy: fingerprints, ladder timing, caps, the IST-day budget, ack spoofing."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from simgold import alerts as A
from simgold.market import IST

CFG = A.params()


def ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST).timestamp()


# ------------------------------------------------------------------ fingerprints
@pytest.mark.parametrize("a,b", [
    ("GOLDBEES.NS: timed out after 30s", "SILVERBEES.NS: timed out after 10s"),
    ("request 3f2a9c1d-ab12-cd34-ef56-0123456789ab failed", "request 7b1e2d3c-9a8b-1c2d-3e4f-a1b2c3d4e5f6 failed"),
    ("worker run_ab12 crashed", "worker run_cd34 crashed"),
    ("ValueError at 2026-10-14T10:00:01 in /home/runner/work/x.py line 12", "ValueError at 2026-10-15 in /tmp/y/z.py line 9"),
])
def test_same_problem_is_one_fingerprint(a, b):
    assert A.fingerprint("engine", "tick", a) == A.fingerprint("engine", "tick", b)


@pytest.mark.parametrize("a,b", [
    (("engine", "tick", "ValueError: x"), ("engine", "tick", "KeyError: x")),
    (("engine", "tick", "boom"), ("engine", "git-sync", "boom")),
    (("watchdog", "ticks_stale", ""), ("watchdog", "ticks_missing", "")),
])
def test_different_problems_do_not_collide(a, b):
    assert A.fingerprint(*a) != A.fingerprint(*b)


# ------------------------------------------------------------------ budget across midnight IST
def test_fire_budget_resets_at_ist_midnight_not_utc(tmp_path):
    late = ts(2026, 10, 14, 23, 50)
    fired = [A.raise_alert(tmp_path, "c", f"step {chr(97 + i)}", "workflow_failed", now=late + i)["fire"] for i in range(7)]
    assert fired == [True] * 6 + [False]
    # 00:01 IST: a new IST day, so the budget is fresh
    assert A.raise_alert(tmp_path, "c", "step z", "workflow_failed", now=ts(2026, 10, 15, 0, 1))["fire"]
    # UTC midnight (05:30 IST) is not a reset: 5 more fit, the 7th of the IST day does not
    more = [A.raise_alert(tmp_path, "c", f"other {chr(97 + i)}", "workflow_failed", now=ts(2026, 10, 15, 5, 31) + i)["fire"]
            for i in range(6)]
    assert more == [True] * 5 + [False]


def test_incident_deferred_by_the_budget_fires_once_the_budget_is_back(tmp_path):
    """Before the fix a HIGH held back by the 6/day budget never woke the routine, even on later days."""
    t = ts(2026, 10, 14, 14, 0)
    for i in range(6):
        A.raise_alert(tmp_path, "c", f"s{chr(97 + i)}", "workflow_failed", now=t + i)
    d = A.raise_alert(tmp_path, "engine", "floor", "floor_broken", now=t + 10)
    assert not d["fire"] and A.load_state(tmp_path)["alerts"][d["fingerprint"]]["deferred"]
    again = A.raise_alert(tmp_path, "engine", "floor", "floor_broken", now=t + 600)
    assert not again["fire"]                                          # still the same day: still over budget
    nxt = A.raise_alert(tmp_path, "engine", "floor", "floor_broken", now=ts(2026, 10, 15, 9, 0))
    assert nxt["fire"] and "budget" in nxt["reason"]
    assert not A.raise_alert(tmp_path, "engine", "floor", "floor_broken", now=ts(2026, 10, 15, 9, 1))["fire"]


def test_batch_also_retries_a_deferred_incident(tmp_path):
    t = ts(2026, 10, 14, 14, 0)
    for i in range(6):
        A.raise_alert(tmp_path, "c", f"s{chr(97 + i)}", "workflow_failed", now=t + i)
    item = {"component": "watchdog", "step": "ticks_stale", "category": "engine_not_running"}
    assert not A.raise_batch(tmp_path, [item], now=t + 60)["fire"]
    assert A.raise_batch(tmp_path, [item], now=ts(2026, 10, 15, 9, 20))["fire"]
    assert not A.raise_batch(tmp_path, [item], now=ts(2026, 10, 15, 9, 30))["fire"]


# ------------------------------------------------------------------ reminder ladder with late cron runs
def _high_emailed(tmp_path, t0):
    d = A.raise_alert(tmp_path, "engine", "tick-exceptions", "tick_exceptions", now=t0)
    A.record_email(tmp_path, d["fingerprint"], now=t0)
    return d["fingerprint"]


def test_hourly_ladder_tolerates_a_late_cron_and_never_double_sends(tmp_path):
    t0 = ts(2026, 10, 14, 10, 0)
    fp = _high_emailed(tmp_path, t0)
    due = lambda now: [e["fp"] for e in A.reminders_due(A.load_state(tmp_path), now, CFG)]
    assert due(t0 + 54 * 60) == [] and due(t0 + 55 * 60) == [fp]
    late = t0 + 100 * 60                                               # the :07 cron ran 40 minutes late
    assert due(late) == [fp]
    A.record_email(tmp_path, fp, now=late)
    assert due(late + 27 * 60) == []                                   # the next on-time run must not send again


def test_high_reminders_stop_at_24(tmp_path):
    t = ts(2026, 10, 14, 10, 0)
    fp = _high_emailed(tmp_path, t)
    sent = 0
    for k in range(1, 40):
        now = t + k * 3600
        if A.reminders_due(A.load_state(tmp_path), now, CFG):
            A.record_email(tmp_path, fp, now=now)
            sent += 1
    assert sent == 24
    e = A.load_state(tmp_path)["alerts"][fp]
    assert A.should_email(e, t + 50 * 3600, CFG)[0] == "HOLD"


# ------------------------------------------------------------------ ack spoofing
OPEN = {"state": "open", "labels": []}


@pytest.mark.parametrize("assoc,user_type,body", [
    ("NONE", "User", "ack"), ("CONTRIBUTOR", "User", "ACK"), ("FIRST_TIME_CONTRIBUTOR", "User", "ok"),
    ("FIRST_TIMER", "User", "seen"), ("OWNER", "Bot", "ack"), (None, "User", "ack"), ("owner", "User", "ack"),
    ("MEMBER", "User", "please do not ack yet"), ("MEMBER", "User", "acknowledged? no"),
])
def test_ack_from_non_owner_or_non_ack_text_is_ignored(assoc, user_type, body):
    c = {"author_association": assoc, "user": {"type": user_type, "login": "x"}, "body": body}
    assert A.issue_acked(OPEN, [c]) == ""


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_ack_from_trusted_commenter_counts(assoc):
    c = {"author_association": assoc, "user": {"type": "User"}, "body": "  ACK, looking"}
    assert A.issue_acked(OPEN, [c]) == "comment"


def test_spoofed_ack_does_not_stop_reminders(tmp_path):
    t = ts(2026, 10, 14, 10, 0)
    fp = _high_emailed(tmp_path, t)
    A.attach_incident(tmp_path, fp, "inc-1", issue=7)
    gh = lambda args: ('{"state": "open", "labels": [{"name": "Ack"}]}' if args[1].endswith("/7")
                       else '[{"author_association": "NONE", "user": {"type": "User"}, "body": "ack"}]')
    assert A.check_acks(tmp_path, "o/r", gh=gh, now=t + 60) == []
    assert A.reminders_due(A.load_state(tmp_path), t + 3600, CFG)
