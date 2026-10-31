"""tools/report_retry.py: when to retry the missed daily report (exponential backoff, at most 8 attempts)."""
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import report_retry as rr  # noqa: E402
from simgold.market import IST  # noqa: E402

D = date(2026, 10, 15)     # Thursday, a trading day


def at(h, m, day=15):
    return datetime(2026, 10, day, h, m, tzinfo=IST)


def test_backoff_schedule_is_30_60_then_120_and_stops_after_8():
    state = {"date": D.isoformat(), "attempts": [], "status": "new"}
    assert rr.decide_retry(state, D, at(16, 10))[0] == "WAIT"
    times = []
    now = at(16, 20)
    for n in range(1, 9):
        verdict, k = rr.decide_retry(state, D, now)
        assert (verdict, k) == ("FIRE", n)
        state["attempts"].append(now.isoformat())
        times.append(now)
        gap = rr.wait_before(n + 1) if n < 8 else 0
        assert rr.decide_retry(state, D, now)[0] in ("WAIT", "GIVE_UP")
        now = now.replace() + __import__("datetime").timedelta(minutes=gap)
    assert [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])] == [60, 120, 120, 120, 120, 120, 120]
    assert rr.decide_retry(state, D, now) == ("WAIT", 8)            # right after attempt 8: give the routine 90 min


def test_report_day_is_today_after_close_else_previous_trading_day():
    assert rr.report_day(at(16, 0)) == D
    assert rr.report_day(at(2, 0, day=16)) == D                       # Friday 02:00 -> Thursday's report
    assert rr.report_day(datetime(2026, 10, 19, 9, 0, tzinfo=IST)) == date(2026, 10, 16)   # Monday morning -> Friday


def git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


def test_step_fires_until_the_routine_commit_appears(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "paper-trade: 15:30 IST")
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "tools" / "report_retry.py"), *a, "--root", str(tmp_path)],
                                    capture_output=True, text=True).stdout.strip()
    assert run("step", "--date", "2026-10-15", "--now", "2026-10-15T16:21:00+05:30") == "FIRE 1 2026-10-15"
    assert run("step", "--date", "2026-10-15", "--now", "2026-10-15T16:29:00+05:30") == "WAIT 2 2026-10-15"
    assert json.loads((tmp_path / "state" / "report_retry.json").read_text())["attempts"] == ["2026-10-15T16:21:00+05:30"]
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "routine(daily_report): 2026-10-15")
    assert run("step", "--date", "2026-10-15", "--now", "2026-10-15T17:30:00+05:30") == "SENT 2026-10-15"
    assert run("check", "--date", "2026-10-15") == "SENT"
    assert json.loads((tmp_path / "state" / "report_retry.json").read_text())["status"] == "sent"


def test_marker_required_from_16_oct_commit_alone_is_not_sent(tmp_path):
    """16 Oct 2026: the routine committed but Gmail was never called; the commit must not count as sent."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "routine(daily_report): 2026-10-16")
    assert rr.sent(tmp_path, "2026-10-16") is False
    (tmp_path / "reports" / "daily").mkdir(parents=True)
    (tmp_path / "reports" / "daily" / "2026-10-16.sent.json").write_text('{"date": "2026-10-16"}')
    assert rr.sent(tmp_path, "2026-10-16") is True
    assert rr.sent(tmp_path, "2026-10-19") is False          # a marker is per date
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "tools" / "report_retry.py"), *a, "--root", str(tmp_path)],
                                    capture_output=True, text=True).stdout.strip()
    assert run("check", "--date", "2026-10-16") == "SENT"
    assert run("step", "--date", "2026-10-19", "--now", "2026-10-19T16:21:00+05:30") == "FIRE 1 2026-10-19"


def test_fallback_commit_does_not_count_as_sent(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "fallback: DeepSeek guidance/report 18:50 IST")
    assert rr.sent(tmp_path, "2026-10-15") is False


def test_give_up_90_minutes_after_attempt_8():
    from datetime import timedelta
    last = datetime(2026, 10, 16, 5, 45, tzinfo=IST)
    state = {"date": D.isoformat(), "attempts": [(last - timedelta(hours=k)).isoformat() for k in range(7, -1, -1)], "status": "attempt 8 of 8"}
    assert rr.decide_retry(state, D, last + timedelta(minutes=30)) == ("WAIT", 8)
    assert rr.decide_retry(state, D, last + timedelta(minutes=80)) == ("WAIT", 8)
    assert rr.decide_retry(state, D, last + timedelta(minutes=90)) == ("GIVE_UP", 8)
    assert rr.decide_retry(state, D, last + timedelta(hours=5)) == ("GIVE_UP", 8)
    assert rr.decide(state, D, last + timedelta(minutes=30))[0] == "BACKUP"   # the 16:30 rule comes first


def test_cron_reaches_attempt_8_and_the_backup_window():
    """First retry ~16:45, then +60, +120 x6: attempt 8 ~05:45 IST, GIVE_UP ~07:15 IST; both need a scheduled run."""
    import yaml
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "report-retry.yml").read_text(encoding="utf-8"))
    crons = [c["cron"] for c in doc.get("on", doc.get(True))["schedule"]]
    utc_hours = set()
    for c in crons:
        h = c.split()[1]
        a, b = (int(x) for x in h.split("-")) if "-" in h else (int(h), int(h))
        utc_hours |= set(range(a, b + 1))
    assert {0, 1} <= utc_hours            # 05:45 and 07:15 IST are 00:15 and 01:45 UTC


def test_step_status_and_mark_backup(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "paper-trade: 15:30 IST")
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "tools" / "report_retry.py"), *a, "--root", str(tmp_path)],
                                    capture_output=True, text=True).stdout.strip()
    tries = [f"2026-10-15T{h:02d}:45:00+05:30" for h in (16, 17, 19, 21, 23)] + [f"2026-10-16T{h:02d}:45:00+05:30" for h in (1, 3, 5)]
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "report_retry.json").write_text(json.dumps({"date": "2026-10-15", "attempts": tries, "status": "attempt 8 of 8"}))
    assert run("step", "--now", "2026-10-16T06:00:00+05:30") == "BACKUP 2026-10-15"     # 16:30 rule wins over the old GIVE_UP
    assert run("status", "--date", "2026-10-15") == "attempt 8 of 8"
    assert run("mark-backup", "--date", "2026-10-15", "--now", "2026-10-16T07:17:00+05:30") == "BACKUP_SENT 2026-10-15"
    st = json.loads((tmp_path / "state" / "report_retry.json").read_text())
    assert st["status"] == "backup_sent" and st["attempts"] == tries and st["backup_sent_at"].startswith("2026-10-16T07:17")
    assert run("step", "--now", "2026-10-16T07:46:00+05:30") == "BACKUP_SENT 2026-10-15"
    assert run("status", "--date", "2026-10-15") == "backup_sent"         # GIVE_UP does not reset it: no second email


# ---- 16:30 backup (report.backup_after) ----
RUN = lambda root, *a: subprocess.run([sys.executable, str(ROOT / "tools" / "report_retry.py"), *a, "--root", str(root)],
                                      capture_output=True, text=True).stdout.strip()


def test_backup_after_comes_from_params_default_1630():
    assert rr.backup_after() == __import__("datetime").time(16, 30)
    assert rr.backup_after({"report": {"backup_after": "17:05"}}) == __import__("datetime").time(17, 5)
    assert rr.backup_after({"report": {"backup_after": "junk"}}) == __import__("datetime").time(16, 30)


def test_before_1630_is_wait_or_fire_as_before_and_after_it_is_backup():
    state = {"date": D.isoformat(), "attempts": [], "status": "new"}
    assert rr.decide(state, D, at(16, 10))[0] == "WAIT"
    assert rr.decide(state, D, at(16, 20)) == ("FIRE", 1)
    assert rr.decide(state, D, at(16, 29))[0] == "FIRE"
    assert rr.decide(state, D, at(16, 31))[0] == "BACKUP"
    state["attempts"] = [at(16, 20).isoformat()]
    assert rr.decide(state, D, at(16, 31))[0] == "BACKUP"       # no further Claude retry once 16:30 has passed
    assert rr.decide(state, D, at(2, 0, day=16))[0] == "BACKUP"  # a very late run still qualifies


def test_backup_sent_means_no_second_send_and_no_retries():
    state = {"date": D.isoformat(), "attempts": [], "status": "backup_sent"}
    assert rr.decide(state, D, at(17, 30))[0] == "BACKUP_SENT"
    assert rr.decide(state, D, at(23, 0))[0] == "BACKUP_SENT"


def test_step_cli_backup_sent_skip_and_sent(tmp_path):
    marker = tmp_path / "reports" / "daily"
    assert RUN(tmp_path, "step", "--date", "2026-10-21", "--now", "2026-10-21T16:31:00+05:30") == "BACKUP 2026-10-21"
    assert RUN(tmp_path, "mark-backup", "--date", "2026-10-21", "--now", "2026-10-21T16:32:00+05:30") == "BACKUP_SENT 2026-10-21"
    assert RUN(tmp_path, "step", "--date", "2026-10-21", "--now", "2026-10-21T17:00:00+05:30") == "BACKUP_SENT 2026-10-21"
    marker.mkdir(parents=True)                                   # Claude sends later anyway: SENT, nothing else
    (marker / "2026-10-21.sent.json").write_text("{}")
    assert RUN(tmp_path, "step", "--date", "2026-10-21", "--now", "2026-10-21T18:00:00+05:30") == "SENT 2026-10-21"
    assert RUN(tmp_path, "step", "--date", "2026-10-20", "--now", "2026-10-20T17:00:00+05:30").startswith("SKIP")   # holiday


def test_sent_by_claude_before_1630_is_sent_not_backup(tmp_path):
    (tmp_path / "reports" / "daily").mkdir(parents=True)
    (tmp_path / "reports" / "daily" / "2026-10-27.sent.json").write_text("{}")
    assert RUN(tmp_path, "step", "--date", "2026-10-27", "--now", "2026-10-27T16:45:00+05:30") == "SENT 2026-10-27"
