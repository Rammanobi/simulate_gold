import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location("hc", Path(__file__).parent.parent / "tools" / "healthcheck.py")
H = importlib.util.module_from_spec(spec); sys.modules["hc"] = H; spec.loader.exec_module(H)
IST = H.IST
NOW = datetime(2026, 10, 13, 11, 0, tzinfo=IST)          # a Tuesday, market open
CLOSED = datetime(2026, 10, 13, 17, 0, tzinfo=IST)


def gh_running(args):
    return json.dumps({"total_count": 1})


def gh_idle(args):
    return json.dumps({"total_count": 0})


def setup(tmp_path, tick_age_min=1):
    (tmp_path / "state").mkdir(); (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "ticks-2026-10-13.csv").write_text(
        "ts,value\n" + (NOW - timedelta(minutes=tick_age_min)).isoformat() + ",100000\n")
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps(
        {"cash": 50000, "positions": {}, "value": 100000, "floor": 90000}))
    return tmp_path


def checks(probs):
    return {p["check"] for p in probs}


def test_healthy(tmp_path):
    assert H.run_checks(setup(tmp_path), NOW, gh_running, "o/r") == []


def test_engine_missing_and_stale_ticks(tmp_path):
    p = H.run_checks(setup(tmp_path, tick_age_min=9), NOW, gh_idle, "o/r")
    assert {"engine_run_missing", "ticks_stale"} <= checks(p)
    assert all(x["severity"] == "high" for x in p)


def test_outside_hours_skips_engine_checks(tmp_path):
    assert H.run_checks(setup(tmp_path, tick_age_min=500), CLOSED, gh_idle, "o/r") == []


def test_gh_failure_is_low_severity(tmp_path):
    def boom(a): raise RuntimeError("no net")
    p = H.run_checks(setup(tmp_path), NOW, boom, "o/r")
    assert [x["severity"] for x in p] == ["low"]


def test_bad_state_files(tmp_path):
    root = setup(tmp_path)
    (root / "state" / "guidance.json").write_text("{oops")
    (root / "state" / "portfolio.json").write_text(json.dumps({"cash": 1}))
    c = checks(H.run_checks(root, NOW, gh_running, "o/r"))
    assert "state_parse" in c and "state_keys" in c


def test_invariants(tmp_path):
    root = setup(tmp_path)
    (root / "state" / "equity.csv").write_text("2026-10-12,100000,95000\n")
    (root / "state" / "portfolio.json").write_text(json.dumps({
        "cash": -5, "value": 89000, "floor": 90000, "tripwire": False,
        "positions": {"GOLD": {"qty": -1}, "LIQUIDBEES": {"qty": 1, "value": 10000}}}))
    c = checks(H.run_checks(root, NOW, gh_running, "o/r"))
    assert {"negative_cash", "negative_qty", "below_floor", "floor_lowered", "liquid_low"} <= c


def test_tripwire_waives_liquid_rule(tmp_path):
    root = setup(tmp_path)
    (root / "state" / "portfolio.json").write_text(json.dumps({
        "cash": 1, "value": 100000, "floor": 90000, "tripwire": True, "positions": {"LIQUIDBEES": {"qty": 1, "value": 1}}}))
    assert "liquid_low" not in checks(H.run_checks(root, NOW, gh_running, "o/r"))


def test_memory_caps(tmp_path):
    root = setup(tmp_path)
    (root / "memory").mkdir()
    (root / "memory" / "brief.md").write_text("x" * (H.BRIEF_MAX + 1))
    (root / "memory" / "lessons.md").write_text("\n".join(f"- l{i}" for i in range(61)))
    (root / "tools").mkdir()
    (root / "tools" / "context.py").write_text("print('y' * 30000)")
    c = checks(H.run_checks(root, NOW, gh_running, "o/r"))
    assert {"memory_brief_size", "memory_lessons_size", "context_size"} <= c


def test_guidance_stale_and_incident_limits(tmp_path):
    root = setup(tmp_path)
    old = (NOW - timedelta(hours=4)).isoformat()
    (root / "state" / "guidance.json").write_text(json.dumps({"updated_at": old}))
    t = NOW.timestamp()
    (root / "logs" / "incidents-2026-10-13.jsonl").write_text(
        "\n".join(json.dumps({"notified_at": t + i * 60}) for i in range(2)) + "\n")
    p = H.run_checks(root, NOW, gh_running, "o/r")
    assert {"guidance_stale", "incident_rate_limit"} <= checks(p)
    assert all(x["severity"] == "low" for x in p)


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(H, "run_checks", lambda *a, **k: [])
    assert H.main([]) == 0 and capsys.readouterr().out.startswith("OK")
    monkeypatch.setattr(H, "run_checks", lambda *a, **k: [H.problem("x", "high", "d")])
    assert H.main([]) == 4
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["check"] == "x"
