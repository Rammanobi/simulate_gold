import json
from datetime import datetime

import pytest

from simgold import alerts as A
from simgold.market import IST

T0 = datetime(2026, 10, 13, 10, 0, tzinfo=IST).timestamp()
CFG = dict(A.DEFAULTS)
MIN = 60


def raise_(tmp_path, comp, step, cat, now=T0, **kw):
    return A.raise_alert(tmp_path, comp, step, cat, now=now, cfg=CFG, **kw)


# ---- fingerprints ------------------------------------------------------------------
def test_fingerprint_ignores_timestamps_numbers_paths():
    a = A.fingerprint("Market-Hours Paper Trading", "Run loop", "RuntimeError: no data for ['GOLDBEES.NS'] at 2026-10-13 10:41:02 /home/runner/work/x.py line 33")
    b = A.fingerprint("market hours paper trading", "run loop", "RuntimeError: other thing 99 at 2026-10-14T09:00:00+05:30")
    assert a == b == "market hours paper trading|run loop|runtimeerror"
    assert A.fingerprint("a", "b", "boom 1") != A.fingerprint("a", "c", "boom 1")
    assert A.fingerprint("a", "b", "ValueError: x") != A.fingerprint("a", "b", "KeyError: x")
    assert A.normalise("took 12.5s at 10:41 in /usr/lib/python3.11/site.py") == "took at in"     # "12.5s" holds a digit: dropped whole


# ---- severity table ----------------------------------------------------------------
@pytest.mark.parametrize("cat,sev", [
    ("engine_not_running", "high"), ("floor_broken", "high"), ("near_floor", "high"), ("state_corrupt", "high"),
    ("test_failure_main", "high"), ("git_push_failing", "high"),
    ("watchdog_stale", "medium"), ("data_source_down", "medium"), ("workflow_failed", "medium"), ("stale_guidance", "medium"), ("memory_cap", "medium"),
    ("transient_fetch", "low"), ("slow_run", "low"), ("warning", "low")])
def test_severity_table(cat, sev):
    assert A.severity_of(cat) == sev


# ---- wake budget, grouping ---------------------------------------------------------
def test_budget_of_six_fires_per_day(tmp_path):
    fired = [raise_(tmp_path, "w", f"s{chr(97 + i)}", "workflow_failed", now=T0 + i * 20 * MIN)["fire"] for i in range(8)]
    # MEDIUM, 20 min apart so grouping does not interfere: six fire, then the budget is exhausted
    assert fired == [True] * 6 + [False] * 2
    st = A.load_state(tmp_path)
    assert A.fires_today(st, T0 + 160 * MIN) == 6
    assert sum(1 for e in st["alerts"].values() if e["deferred"]) == 2 and len(st["pending"]) == 2
    # a new day starts a fresh budget
    assert raise_(tmp_path, "w", "next-day", "workflow_failed", now=T0 + 86400)["fire"]


def test_high_is_bounded_by_budget_too(tmp_path):
    fired = [raise_(tmp_path, "w", f"h{chr(97 + i)}", "state_corrupt", now=T0 + i)["fire"] for i in range(7)]
    assert fired == [True] * 6 + [False]


def test_same_fingerprint_is_one_incident(tmp_path):
    assert raise_(tmp_path, "w", "s", "workflow_failed", error="Boom 1")["fire"]
    d = raise_(tmp_path, "w", "s", "workflow_failed", now=T0 + 600, error="Boom 99")
    assert not d["fire"] and not d["new"]
    assert A.load_state(tmp_path)["alerts"][d["fingerprint"]]["count"] == 2


def test_low_never_wakes_the_routine(tmp_path):
    d = raise_(tmp_path, "engine", "tick", "transient_fetch")
    assert d["severity"] == "low" and not d["fire"]
    assert A.fires_today(A.load_state(tmp_path), T0) == 0


def test_15_minute_grouping_after_high(tmp_path):
    assert raise_(tmp_path, "engine", "tick", "state_corrupt")["fire"]
    d = raise_(tmp_path, "w", "x", "workflow_failed", now=T0 + 10 * MIN)
    assert not d["fire"] and "grouped" in d["reason"]
    assert A.take_pending(tmp_path)[0]["step"] == "x"
    assert raise_(tmp_path, "w", "y", "workflow_failed", now=T0 + 16 * MIN)["fire"]      # window is over


def test_batch_fires_once(tmp_path):
    items = [{"component": "watchdog", "step": f"c{chr(97 + i)}", "category": "memory_cap"} for i in range(4)]
    r = A.raise_batch(tmp_path, items, now=T0, cfg=CFG)
    assert r["fire"] and A.fires_today(A.load_state(tmp_path), T0) == 1


# ---- reminder ladder, ack, resolved-once -------------------------------------------
def emailed(tmp_path, cat, times, step="s"):
    d = raise_(tmp_path, "w", step, cat)
    for t in times:
        A.record_email(tmp_path, d["fingerprint"], t)
    return d["fingerprint"], A.load_state(tmp_path)["alerts"][d["fingerprint"]]


def test_medium_ladder_1h_4h_then_daily(tmp_path):
    fp, e = emailed(tmp_path, "workflow_failed", [T0])
    assert A.reminder_due_at(e, CFG) == T0 + 3600
    A.record_email(tmp_path, fp, T0 + 3600); e = A.load_state(tmp_path)["alerts"][fp]
    assert A.reminder_due_at(e, CFG) == T0 + 4 * 3600
    A.record_email(tmp_path, fp, T0 + 4 * 3600); e = A.load_state(tmp_path)["alerts"][fp]
    assert A.reminder_due_at(e, CFG) == T0 + 4 * 3600 + 86400
    assert A.should_email(e, T0 + 5 * 3600, CFG)[0] == "HOLD"
    v, mode, _ = A.should_email(e, T0 + 4 * 3600 + 86400, CFG)
    assert (v, mode) == ("SEND", "still-broken")


def test_high_reminds_hourly_and_cap_24(tmp_path):
    fp, e = emailed(tmp_path, "state_corrupt", [T0])
    assert A.reminder_due_at(e, CFG) == T0 + 3600
    assert not A.reminders_due(A.load_state(tmp_path), T0 + 54 * MIN, CFG)
    assert [x["fp"] for x in A.reminders_due(A.load_state(tmp_path), T0 + 55 * MIN, CFG)] == [fp]
    for i in range(1, 25):
        A.record_email(tmp_path, fp, T0 + i * 3600)         # the first email plus 24 reminders
    st = A.load_state(tmp_path)
    assert A.reminders_due(st, T0 + 99 * 3600, CFG) == []
    assert A.should_email(st["alerts"][fp], T0 + 99 * 3600, CFG)[1:] == ("reminder", "reminder cap reached")


def test_ack_stops_reminders_but_keeps_incident_open(tmp_path):
    fp, e = emailed(tmp_path, "state_corrupt", [T0])
    assert A.ack(tmp_path, fp, "email", T0 + 600)
    st = A.load_state(tmp_path)
    assert A.reminders_due(st, T0 + 5 * 3600, CFG) == []
    assert st["alerts"][fp]["status"] == "open" and st["alerts"][fp]["acked_via"] == "email"
    assert A.should_email(st["alerts"][fp], T0 + 5 * 3600, CFG)[0] == "HOLD"


def test_resolved_email_exactly_once(tmp_path):
    fp, _ = emailed(tmp_path, "state_corrupt", [T0])
    A.resolve(tmp_path, fp, T0 + 100)
    e = A.load_state(tmp_path)["alerts"][fp]
    assert A.should_email(e, T0 + 200, CFG)[:2] == ("SEND", "resolved")
    A.mark_resolved_emailed(tmp_path, fp)
    assert A.should_email(A.load_state(tmp_path)["alerts"][fp], T0 + 300, CFG)[0] == "HOLD"


def test_never_emailed_incident_gets_no_resolved_email(tmp_path):
    d = raise_(tmp_path, "w", "s", "workflow_failed")
    A.resolve(tmp_path, d["fingerprint"])
    assert A.should_email(A.load_state(tmp_path)["alerts"][d["fingerprint"]], T0, CFG)[0] == "HOLD"


def test_email_policy_by_severity(tmp_path):
    d = raise_(tmp_path, "w", "h", "state_corrupt"); m = raise_(tmp_path, "w", "m", "workflow_failed", now=T0 + 30 * MIN)
    lo = raise_(tmp_path, "w", "l", "warning")
    st = A.load_state(tmp_path)["alerts"]
    assert A.should_email(st[d["fingerprint"]], T0, CFG)[:2] == ("SEND", "incident")
    assert A.should_email(st[m["fingerprint"]], T0, CFG)[0] == "HOLD"                      # MEDIUM auto-fixes first
    assert A.should_email(st[m["fingerprint"]], T0, CFG, needs_approval=True)[0] == "SEND"
    assert A.should_email(st[m["fingerprint"]], T0, CFG, fix_failed=True)[0] == "SEND"
    assert A.should_email(st[lo["fingerprint"]], T0, CFG, needs_approval=True)[0] == "HOLD"  # LOW never


# ---- acknowledgement detection (GitHub, no AI) -------------------------------------
def c(body, assoc="OWNER", typ="User"):
    return {"body": body, "author_association": assoc, "user": {"type": typ}}


def test_ack_detection():
    open_issue = {"state": "open", "labels": []}
    assert A.issue_acked({"state": "closed", "labels": []}, []) == "closed"
    assert A.issue_acked({"state": "open", "labels": [{"name": "incident"}, {"name": "ack"}]}, []) == "label"
    assert A.issue_acked(open_issue, [c("ack")]) == "comment"
    assert A.issue_acked(open_issue, [c("  ACK, thanks")]) == "comment"
    assert A.issue_acked(open_issue, [c("OK")]) == "comment"
    assert A.issue_acked(open_issue, [c("not ack yet")]) == ""
    assert A.issue_acked(open_issue, [c("ack", assoc="NONE")]) == ""            # a stranger on a public repo
    assert A.issue_acked(open_issue, [c("ack", typ="Bot")]) == ""
    assert A.issue_acked(open_issue, []) == ""


def test_check_acks_records_state(tmp_path):
    d = raise_(tmp_path, "engine", "tick", "state_corrupt")
    A.record_email(tmp_path, d["fingerprint"], T0)
    A.attach_incident(tmp_path, d["fingerprint"], "inc-1", issue=7, issue_url="https://x/7")

    def gh(args):
        return json.dumps([c("ack")] if args[1].endswith("/comments?per_page=100") else {"state": "open", "labels": []})
    assert A.check_acks(tmp_path, "o/r", gh, now=T0 + 10) == [d["fingerprint"]]
    e = A.load_state(tmp_path)["alerts"][d["fingerprint"]]
    assert e["acked"] and e["acked_via"] == "github comment" and e["status"] == "open"


# ---- safe mode ---------------------------------------------------------------------
def test_safe_mode_after_more_than_5_incidents(tmp_path):
    for i in range(5):
        raise_(tmp_path, "w", f"s{chr(97 + i)}", "workflow_failed", now=T0 + i * 20 * MIN)
    assert not A.evaluate_safe_mode(tmp_path, T0 + 100 * MIN, CFG)
    raise_(tmp_path, "w", "sz", "workflow_failed", now=T0 + 110 * MIN)
    assert A.evaluate_safe_mode(tmp_path, T0 + 111 * MIN, CFG) and A.safe_mode_on(tmp_path)
    info = A.safe_mode_info(tmp_path)
    assert info["on"] and info["since"] and "6 distinct" in info["reason"]
    assert A.load_state(tmp_path)["alerts"]["system|safe mode|"]["severity"] == "high"   # the one 'System unstable' alert


def test_safe_mode_when_high_not_fixed_in_30_min(tmp_path):
    raise_(tmp_path, "engine", "tick", "state_corrupt")
    assert not A.evaluate_safe_mode(tmp_path, T0 + 29 * MIN, CFG)
    assert A.evaluate_safe_mode(tmp_path, T0 + 31 * MIN, CFG)


def test_watchdog_stale_open_60_min_never_sets_safe_mode(tmp_path):
    raise_(tmp_path, "watchdog", "watchdog_stale", "watchdog_stale")
    assert not A.evaluate_safe_mode(tmp_path, T0 + 60 * MIN, CFG)
    assert not A.safe_mode_on(tmp_path)
    assert A.load_state(tmp_path)["alerts"]  # still reported


def test_watchdog_stale_not_counted_in_distinct_incidents(tmp_path):
    raise_(tmp_path, "watchdog", "watchdog_stale", "watchdog_stale")
    for i in range(5):
        raise_(tmp_path, "w", f"s{chr(97 + i)}", "workflow_failed", now=T0 + i * 20 * MIN)
    assert not A.evaluate_safe_mode(tmp_path, T0 + 100 * MIN, CFG)     # 5 real incidents + the exempt one


def test_23_oct_cluster_no_longer_sets_safe_mode(tmp_path):
    """The 7 incidents of 23 Oct 2026 (a 148 min GitHub schedule gap): 3 are monitoring noise, so only 4 count."""
    raise_(tmp_path, "engine", "tick", "transient_fetch", now=T0)
    raise_(tmp_path, "engine", "price feed", "data_source_down", now=T0)
    for i, (step, cat) in enumerate([("engine_run_missing", "engine_not_running"), ("guidance_stale", "stale_guidance"),
                                     ("routines_silent", "routines_silent"), ("ticks_stale", "engine_not_running"),
                                     ("watchdog_stale", "watchdog_stale")]):
        raise_(tmp_path, "watchdog", step, cat, now=T0 + 10 * MIN)
    assert len(A.load_state(tmp_path)["alerts"]) == 7
    assert not A.evaluate_safe_mode(tmp_path, T0 + 11 * MIN, CFG)
    assert not A.safe_mode_on(tmp_path)


def test_exempt_noise_categories_are_not_counted_or_escalated(tmp_path):
    for cat in ("transient_fetch", "stale_guidance"):
        assert cat in A.SAFE_MODE_EXEMPT
    for i in range(8):                       # eight distinct noise incidents never switch safe mode on
        raise_(tmp_path, "w", f"n{chr(97 + i)}", "stale_guidance" if i % 2 else "transient_fetch", now=T0 + i * 20 * MIN)
    assert not A.evaluate_safe_mode(tmp_path, T0 + 300 * MIN, CFG)
    assert len(A.load_state(tmp_path)["alerts"]) == 8       # still reported


def test_real_categories_still_counted_after_the_exemption(tmp_path):
    for cat in ("engine_not_running", "data_source_down", "workflow_failed", "state_corrupt"):
        assert cat not in A.SAFE_MODE_EXEMPT


def test_30_oct_routines_silent_no_longer_sets_safe_mode(tmp_path):
    """30 Oct 2026: the Claude routines were silent (usage limit, fewer hourly runs); the engine was healthy and stale
    guidance already counts as neutral, so a routines_silent HIGH left open must not block the 15:10 floor buy."""
    assert "routines_silent" in A.SAFE_MODE_EXEMPT
    raise_(tmp_path, "watchdog", "routines_silent", "routines_silent", now=T0)
    assert not A.evaluate_safe_mode(tmp_path, T0 + 180 * MIN, CFG)
    assert not A.safe_mode_on(tmp_path)
    assert len(A.load_state(tmp_path)["alerts"]) == 1        # still reported


def test_genuine_high_still_sets_safe_mode_next_to_watchdog_stale(tmp_path):
    raise_(tmp_path, "watchdog", "watchdog_stale", "watchdog_stale")
    raise_(tmp_path, "engine", "tick", "engine_not_running")
    assert A.evaluate_safe_mode(tmp_path, T0 + 31 * MIN, CFG)
    assert "engine" in A.safe_mode_info(tmp_path)["reason"] or "tick" in A.safe_mode_info(tmp_path)["reason"]


def test_safe_mode_not_triggered_if_fixed(tmp_path):
    d = raise_(tmp_path, "engine", "tick", "state_corrupt")
    A.resolve(tmp_path, d["fingerprint"], T0 + 600)
    assert not A.evaluate_safe_mode(tmp_path, T0 + 3600, CFG)


def test_safe_mode_file_semantics_and_auto_clear(tmp_path):
    assert not A.safe_mode_on(tmp_path)
    A.set_safe_mode(tmp_path, "test", T0, notify=False)
    assert A.safe_mode_on(tmp_path)
    tomorrow_8am = datetime(2026, 10, 14, 8, 45, tzinfo=IST).timestamp()
    assert not A.auto_clear_safe_mode(tmp_path, T0 + 3600, True)             # same day: stays on
    assert not A.auto_clear_safe_mode(tmp_path, tomorrow_8am, False)         # healthcheck not clean
    assert A.auto_clear_safe_mode(tmp_path, tomorrow_8am, True) and not A.safe_mode_on(tmp_path)
    A.safe_path(tmp_path).write_text("{broken")
    assert A.safe_mode_on(tmp_path)                                          # unreadable fails closed


def test_state_written_atomically(tmp_path):
    A.save_state(tmp_path, A._empty())
    assert not list(tmp_path.glob(".*.tmp")) and json.loads((tmp_path / "alerts.json").read_text())["version"] == 1


# ---- the engine honours safe mode every tick ---------------------------------------
def test_engine_makes_no_buys_in_safe_mode(tmp_path):
    import test_smoke as S
    pf, env, _, _ = S.simulate(tmp_path / "ok", "quiet_rally")
    assert any(r["action"] == "BUY" for r in S.read_log(env))
    (tmp_path / "sm" / "state").mkdir(parents=True)
    A.set_safe_mode(tmp_path / "sm" / "state", "test", notify=False)
    pf, env, floors, _ = S.simulate(tmp_path / "sm", "quiet_rally")
    recs = S.read_log(env)
    assert not any(r["action"] == "BUY" for r in recs) and not pf.positions
    assert any(r["trigger"] == "Safe mode" for r in recs)
