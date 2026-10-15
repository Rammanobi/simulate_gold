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
    assert rr.decide(state, D, at(16, 10))[0] == "WAIT"
    times = []
    now = at(16, 20)
    for n in range(1, 9):
        verdict, k = rr.decide(state, D, now)
        assert (verdict, k) == ("FIRE", n)
        state["attempts"].append(now.isoformat())
        times.append(now)
        gap = rr.wait_before(n + 1) if n < 8 else 0
        assert rr.decide(state, D, now)[0] in ("WAIT", "GIVE_UP")
        now = now.replace() + __import__("datetime").timedelta(minutes=gap)
    assert [int((b - a).total_seconds() // 60) for a, b in zip(times, times[1:])] == [60, 120, 120, 120, 120, 120, 120]
    assert rr.decide(state, D, now)[0] == "GIVE_UP"


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
    assert run("step", "--date", "2026-10-15", "--now", "2026-10-15T16:51:00+05:30") == "WAIT 2 2026-10-15"
    assert json.loads((tmp_path / "state" / "report_retry.json").read_text())["attempts"] == ["2026-10-15T16:21:00+05:30"]
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "routine(daily_report): 2026-10-15")
    assert run("step", "--date", "2026-10-15", "--now", "2026-10-15T17:30:00+05:30") == "SENT 2026-10-15"
    assert run("check", "--date", "2026-10-15") == "SENT"
    assert json.loads((tmp_path / "state" / "report_retry.json").read_text())["status"] == "sent"


def test_fallback_commit_does_not_count_as_sent(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "fallback: DeepSeek guidance/report 18:50 IST")
    assert rr.sent(tmp_path, "2026-10-15") is False
