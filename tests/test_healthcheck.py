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
    p = run(setup(tmp_path, tick_age_min=9), gh=gh_idle)
    assert {"engine_run_missing", "ticks_stale"} <= checks(p)
    assert all(x["severity"] == "high" and x["category"] == "engine_not_running" for x in p)


def test_ticks_row_is_hhmm_ist(tmp_path):
    root = setup(tmp_path)
    t = H.last_tick(root, NOW)
    assert t["time"] == NOW - timedelta(minutes=1) and t["value"] == 100300 and t["floor"] == 99500


def test_outside_hours_skips_engine_checks(tmp_path):
    root = setup(tmp_path, tick_age_min=500, health_age_min=900)
    (root / "reports" / "daily").mkdir(parents=True)
    (root / "reports" / "daily" / "2026-10-13.json").write_text("{}")      # Claude's report is there
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


def test_watchdog_stale_is_high_after_75_min(tmp_path):
    assert run(setup(tmp_path, health_age_min=74)) == []
    p = run(setup(tmp_path / "x", health_age_min=76))
    assert [(x["check"], x["severity"]) for x in p] == [("watchdog_stale", "high")]
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
