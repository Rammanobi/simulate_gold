import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location("hc", Path(__file__).parent.parent / "tools" / "healthcheck.py")
H = importlib.util.module_from_spec(spec); sys.modules["hc"] = H; spec.loader.exec_module(H)
IST = H.IST
NOW = datetime(2026, 10, 13, 11, 0, tzinfo=IST)          # a Tuesday, market open
CLOSED = datetime(2026, 10, 13, 17, 0, tzinfo=IST)
no_git = lambda root, args: (_ for _ in ()).throw(RuntimeError("no git"))


def gh_running(args): return json.dumps({"total_count": 1})
def gh_idle(args): return json.dumps({"total_count": 0})


def tick_line(t, value=100300, floor=99500, cash=0, lb=70000):
    return f"{t:%H:%M},{value},{floor},{cash},{lb}\n"


def setup(tmp_path, tick_age_min=1, value=100300, floor=99500, health_age_min=5):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(); (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "ticks-2026-10-13.csv").write_text(
        "time,value,floor,cash,liquidbees\n" + tick_line(NOW - timedelta(minutes=tick_age_min), value, floor))
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps(
        {"cash": 0, "liquidbees": 70000, "floor": floor, "positions": {}}))
    (tmp_path / "state" / "health.json").write_text(json.dumps(
        {"last_run": (NOW - timedelta(minutes=health_age_min)).isoformat(), "results": [], "all_green": True}))
    return tmp_path


def run(root, now=NOW, gh=gh_running, git=no_git):
    return H.run_checks(root, now, gh, "o/r", git)


def checks(probs):
    return {p["check"] for p in probs}


def test_healthy(tmp_path):
    assert run(setup(tmp_path)) == []


def test_engine_missing_and_stale_ticks_are_high(tmp_path):
    p = run(setup(tmp_path, tick_age_min=15), gh=gh_idle)
    assert {"engine_run_missing", "ticks_stale"} <= checks(p)
    assert all(x["severity"] == "high" and x["category"] == "engine_not_running" for x in p)


def test_a_tick_row_one_sync_interval_old_is_not_stale(tmp_path):
    """14 Oct 2026, 15:16 IST: a row 6 min old (normal with a 5-min git sync) raised ticks_stale and a duplicate run."""
    assert "ticks_stale" not in checks(run(setup(tmp_path, tick_age_min=7)))


def test_ticks_row_is_hhmm_ist(tmp_path):
    root = setup(tmp_path)
    t = H.last_tick(root, NOW)
    assert t["time"] == NOW - timedelta(minutes=1) and t["value"] == 100300 and t["floor"] == 99500


def test_outside_hours_skips_engine_checks(tmp_path):
    root = setup(tmp_path, tick_age_min=500, health_age_min=900)
    (root / "reports" / "daily").mkdir(parents=True)
    (root / "reports" / "daily" / "2026-10-13.json").write_text("{}")      # Claude's report is there
    (root / "logs" / "ticks-2026-10-13.csv").write_text("time,value,floor,cash,liquidbees\n" + "09:15,100300,99500,0,70000\n" * 150)   # a full session
    assert run(root, now=CLOSED, gh=gh_idle) == []


def test_gh_failure_is_low(tmp_path):
    def boom(a): raise RuntimeError("no net")
    assert [x["severity"] for x in run(setup(tmp_path), gh=boom)] == ["low"]


def test_state_files_real_schemas(tmp_path):
    root = setup(tmp_path)
    (root / "state" / "guidance.json").write_text("{oops")
    (root / "state" / "events.json").write_text(json.dumps([{"date": "2026-10-14", "type": "fomc"}]))      # a list is valid
    (root / "state" / "fastlane.json").write_text(json.dumps({"updated": NOW.isoformat(), "actions": []}))
    assert checks(run(root)) == {"state_parse"}
    (root / "state" / "guidance.json").write_text(json.dumps({"bias": {}}))
    (root / "state" / "portfolio.json").write_text(json.dumps({"cash": 1}))
    c = run(root)
    assert checks(c) == {"state_keys"} and all(p["severity"] == "high" for p in c)


def test_floor_invariants(tmp_path):
    root = setup(tmp_path, value=99350, floor=99500)             # more than 100 under the floor
    assert "below_floor" in checks(run(root))
    root2 = setup(tmp_path / "b", value=99550, floor=99500)      # within 100 of the floor: HIGH
    p = [x for x in run(root2) if x["check"] == "near_floor"]
    assert p and p[0]["severity"] == "high"


def test_floor_never_lower_than_yesterday_from_git(tmp_path):
    root = setup(tmp_path, floor=99500)

    def git(r, args):
        if args[0] == "log":
            return "aaa 2026-10-13T09:00:00+05:30\nbbb 2026-10-12T15:40:00+05:30\n"
        assert args == ["show", "bbb:state/portfolio.json"]          # the newest commit from BEFORE today
        return json.dumps({"floor": 100000})
    assert "floor_lowered" in checks(run(root, git=git))
    assert "floor_lowered" not in checks(run(root, git=lambda r, a: "bbb 2026-10-12T15:40:00+05:30\n" if a[0] == "log" else json.dumps({"floor": 99500})))


def test_negative_and_liquidbees_share(tmp_path):
    root = setup(tmp_path)
    (root / "state" / "portfolio.json").write_text(json.dumps({
        "cash": -5, "liquidbees": 1000, "floor": 99500, "positions": {"GOLDBEES": {"qty": -1, "last_price": 100}}}))
    c = checks(run(root))
    assert {"negative_cash", "negative_qty", "liquid_low"} <= c
    (root / "state" / "portfolio.json").write_text(json.dumps({
        "cash": 0, "liquidbees": 1000, "floor": 99500, "positions": {"GOLDBEES": {"qty": 1}}, "cooldowns": {"no_buys_until": "2026-10-14"}}))
    assert "liquid_low" not in checks(run(root))                 # the tripwire fired


def test_memory_caps(tmp_path):
    root = setup(tmp_path)
    (root / "memory").mkdir()
    (root / "memory" / "brief.md").write_text("x" * (H.BRIEF_MAX + 1))
    (root / "memory" / "lessons.md").write_text("\n".join(f"- l{i}" for i in range(61)))
    (root / "tools").mkdir()
    (root / "tools" / "context.py").write_text("print('y' * 30000)")
    p = run(root)
    assert {"memory_brief_size", "memory_lessons_size", "context_size"} <= checks(p) and all(x["severity"] == "medium" for x in p)


def test_stale_guidance_is_medium(tmp_path):
    root = setup(tmp_path)
    (root / "state" / "guidance.json").write_text(json.dumps({"updated": (NOW - timedelta(hours=4)).isoformat(), "bias": {}}))
    p = run(root)
    assert [(x["check"], x["severity"]) for x in p] == [("guidance_stale", "medium")]
    (root / "state" / "guidance.json").write_text(json.dumps({"updated": (NOW - timedelta(hours=2)).isoformat(), "bias": {}}))
    assert run(root) == []


def test_watchdog_stale_is_medium_after_75_min(tmp_path):
    assert run(setup(tmp_path, health_age_min=74)) == []
    p = run(setup(tmp_path / "x", health_age_min=76))
    assert [(x["check"], x["severity"]) for x in p] == [("watchdog_stale", "medium")]
    root = setup(tmp_path / "y")
    (root / "state" / "health.json").write_text("{broken")
    assert "watchdog_stale" in checks(run(root))                 # an unreadable file counts
    (root / "state" / "health.json").unlink()
    assert "watchdog_stale" not in checks(run(root))             # first ever run: no baseline yet
    assert "watchdog_stale" not in checks(run(root, now=CLOSED, gh=gh_idle))     # only judged during market hours


def test_write_health_and_daily_log(tmp_path):
    root = setup(tmp_path)
    H.write_health(root, NOW, [])
    H.write_health(root, NOW + timedelta(minutes=10), [H.problem("ticks_stale", "engine_not_running", "x")])
    h = json.loads((root / "state" / "health.json").read_text())
    assert h["all_green"] is False and h["results"][0]["check"] == "ticks_stale" and h["last_run"].startswith("2026-10-13T11:10")
    lines = (root / "logs" / "health-2026-10-13.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["all_green"] is True


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(H, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(H, "write_health", lambda *a, **k: None)
    assert H.main([]) == 0 and capsys.readouterr().out.startswith("OK")
    monkeypatch.setattr(H, "run_checks", lambda *a, **k: [H.problem("x", "workflow_failed", "d")])
    assert H.main(["--no-write"]) == 4
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["severity"] == "medium"


# ---- session_missed / relay_missing (12 Nov 2026: a whole session was missed and nothing alarmed) ----
def _day_root(tmp_path, day, rows):
    root = tmp_path
    (root / "state").mkdir(parents=True, exist_ok=True); (root / "logs").mkdir(exist_ok=True)
    if rows is not None:
        (root / "logs" / f"ticks-{day}.csv").write_text("time,value,floor,cash,liquidbees\n" + "09:15,100300,75000,0,0\n" * rows)
    return root


def test_session_missed_fires_at_16_09_on_12_nov_with_no_ticks_file(tmp_path):
    now = datetime(2026, 11, 12, 16, 9, tzinfo=IST)
    p = H.check_session_missed(_day_root(tmp_path, "2026-11-12", None), now)
    assert [(x["check"], x["severity"], x["category"]) for x in p] == [("session_missed", "high", "session_missed")]
    assert "session_missed" in checks(run(_day_root(tmp_path, "2026-11-12", None), now=now, gh=gh_idle))


def test_session_missed_on_a_tiny_ticks_file_but_not_on_a_full_one(tmp_path):
    now = datetime(2026, 11, 12, 16, 9, tzinfo=IST)
    assert H.check_session_missed(_day_root(tmp_path / "a", "2026-11-12", 99), now)
    assert H.check_session_missed(_day_root(tmp_path / "b", "2026-11-12", 100), now) == []
    assert H.check_session_missed(_day_root(tmp_path / "c", "2026-11-12", 375), now) == []


def test_session_missed_not_on_holiday_weekend_or_before_1540(tmp_path):
    root = _day_root(tmp_path, "2026-11-10", None)
    assert H.check_session_missed(root, datetime(2026, 11, 10, 16, 9, tzinfo=IST)) == []        # Balipratipada holiday
    assert H.check_session_missed(root, datetime(2026, 11, 14, 16, 9, tzinfo=IST)) == []        # Saturday
    assert H.check_session_missed(root, datetime(2026, 11, 12, 15, 39, tzinfo=IST)) == []        # session may still be finishing
    assert H.check_session_missed(root, datetime(2026, 11, 12, 15, 40, tzinfo=IST))


def _gh_relays(statuses):
    return lambda args: json.dumps({"workflow_runs": [{"id": i, "status": s} for i, s in enumerate(statuses)]})


def test_relay_window():
    d = lambda y, m, dd, hh, mm: datetime(y, m, dd, hh, mm, tzinfo=IST)
    assert H.relay_should_exist(d(2026, 11, 12, 16, 30)) and H.relay_should_exist(d(2026, 11, 13, 8, 49))
    assert not H.relay_should_exist(d(2026, 11, 13, 8, 50)) and not H.relay_should_exist(d(2026, 11, 13, 16, 29))
    assert H.relay_should_exist(d(2026, 11, 14, 11, 0)) and H.relay_should_exist(d(2026, 11, 10, 11, 0))   # weekend / holiday: all day


def test_relay_missing_is_medium_when_no_run_is_queued_or_in_progress():
    night = datetime(2026, 11, 12, 21, 0, tzinfo=IST)
    p = H.check_relay(night, _gh_relays(["completed", "completed"]), "o/r")
    assert [(x["check"], x["severity"]) for x in p] == [("relay_missing", "medium")]
    assert H.check_relay(night, _gh_relays([]), "o/r")
    for ok in (["completed", "in_progress"], ["queued"], ["pending"]):
        assert H.check_relay(night, _gh_relays(ok), "o/r") == []
    assert H.check_relay(datetime(2026, 11, 12, 11, 0, tzinfo=IST), _gh_relays([]), "o/r") == []      # session hours: not judged
    assert H.check_relay(night, lambda a: json.dumps({"total_count": 0}), "o/r") == []                # no run list: cannot judge


def test_relay_query_failure_is_only_a_low_warning():
    def boom(args): raise RuntimeError("gh missing")
    p = H.check_relay(datetime(2026, 11, 12, 21, 0, tzinfo=IST), boom, "o/r")
    assert [(x["check"], x["severity"]) for x in p] == [("relay_check_unknown", "low")]


def test_new_categories_never_switch_safe_mode_on_and_are_still_reported():
    from simgold import alerts
    assert {"session_missed", "relay_missing"} <= alerts.SAFE_MODE_EXEMPT
    assert alerts.severity_of("session_missed") == "high" and alerts.severity_of("relay_missing") == "medium"
    probs = [H.problem("session_missed", "session_missed", "x"), H.problem("relay_missing", "relay_missing", "y")]
    assert H.clean_for_safe_mode(probs)


# ---- review fixes (PR 57) ----
def test_session_missed_after_midnight_still_sees_the_previous_trading_day(tmp_path):
    root = _day_root(tmp_path, "2026-11-11", 375)                       # the engine ran on 11 Nov, never on 12 Nov
    for now in (datetime(2026, 11, 13, 0, 6, tzinfo=IST), datetime(2026, 11, 13, 9, 0, tzinfo=IST), datetime(2026, 11, 13, 15, 39, tzinfo=IST)):
        p = H.check_session_missed(root, now)
        assert [x["check"] for x in p] == ["session_missed"] and "2026-11-12" in p[0]["detail"]
    assert H.check_session_missed(root, datetime(2026, 11, 13, 15, 40, tzinfo=IST))                 # now it is 13 Nov itself (no ticks)


def test_session_missed_weekend_and_monday_look_at_friday(tmp_path):
    root = _day_root(tmp_path, "2026-11-12", 375)
    (root / "logs" / "ticks-2026-11-13.csv").write_text("time,value,floor,cash,liquidbees\n" + "09:15,1,1,0,0\n" * 375)
    for now in (datetime(2026, 11, 14, 11, 0, tzinfo=IST), datetime(2026, 11, 16, 3, 0, tzinfo=IST)):
        assert H.check_session_missed(root, now) == []
    (root / "logs" / "ticks-2026-11-13.csv").unlink()
    assert H.check_session_missed(root, datetime(2026, 11, 15, 11, 0, tzinfo=IST))
    assert H.check_session_missed(root, datetime(2026, 11, 10, 16, 9, tzinfo=IST)) == []            # 10 Nov: prev trading day 9 Nov has no older log


def test_session_missed_previous_day_check_needs_earlier_history(tmp_path):
    """A fresh checkout (no older ticks file) must not alarm in the morning."""
    root = _day_root(tmp_path, "2026-11-13", 5)
    assert H.check_session_missed(root, datetime(2026, 11, 13, 9, 0, tzinfo=IST)) == []


def test_relay_check_unknown_is_low_and_safe_mode_exempt():
    from simgold import alerts
    assert "relay_check_unknown" in alerts.SAFE_MODE_EXEMPT and alerts.severity_of("relay_check_unknown") == "low"
    def boom(args): raise RuntimeError("503")
    p = H.check_relay(datetime(2026, 11, 13, 8, 30, tzinfo=IST), boom, "o/r")
    assert [x["category"] for x in p] == ["relay_check_unknown"] and H.clean_for_safe_mode(p)


def test_relay_check_ignores_test_runs_and_names_the_last_conclusion():
    night = datetime(2026, 11, 12, 21, 0, tzinfo=IST)
    gh = lambda a: json.dumps({"workflow_runs": [
        {"status": "in_progress", "display_title": "engine-relay TEST"}, {"status": "completed", "conclusion": "failure", "display_title": "engine-relay"}]})
    p = H.check_relay(night, gh, "o/r")
    assert [x["check"] for x in p] == ["relay_missing"] and "ended 'failure'" in p[0]["detail"] or "failure" in p[0]["detail"]
