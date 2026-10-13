"""The free 'Claude is not running' alarm: healthcheck routines_silent + the watchdog workflow's issue steps."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hc_rs", ROOT / "tools" / "healthcheck.py")
H = importlib.util.module_from_spec(spec); sys.modules["hc_rs"] = H; spec.loader.exec_module(H)
IST = H.IST


def at(h, m, d=13):
    return datetime(2026, 10, d, h, m, tzinfo=IST)


def root_with(tmp_path, updated=None, note="", report=None):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    if updated:
        (tmp_path / "state" / "guidance.json").write_text(json.dumps({"updated": updated.isoformat(), "bias": {}, "note": note}))
    if report is not None:
        (tmp_path / "reports" / "daily").mkdir(parents=True, exist_ok=True)
        (tmp_path / "reports" / "daily" / "2026-10-13.json").write_text(json.dumps(report))
    return tmp_path


def silent(root, now):
    g = None
    p = root / "state" / "guidance.json"
    if p.exists():
        g = json.loads(p.read_text())
    return H.check_routines_silent(root, g, now)


def test_guidance_older_than_3h_after_1130_is_high(tmp_path):
    root = root_with(tmp_path, updated=at(8, 50))
    out = silent(root, at(12, 0))
    assert [p["check"] for p in out] == ["routines_silent"]
    assert out[0]["severity"] == "high" and out[0]["category"] == "routines_silent"


def test_fresh_guidance_is_fine(tmp_path):
    assert silent(root_with(tmp_path, updated=at(11, 40)), at(12, 0)) == []


def test_before_1130_never_alarms_on_guidance(tmp_path):
    assert silent(root_with(tmp_path, updated=at(8, 0)), at(11, 29)) == []


def test_no_guidance_file_no_guidance_alarm(tmp_path):
    assert silent(root_with(tmp_path), at(13, 0)) == []


def test_fallback_written_guidance_still_counts_as_silent(tmp_path):
    root = root_with(tmp_path, updated=at(11, 55), note="[fallback: DeepSeek] neutral")
    assert [p["check"] for p in silent(root, at(12, 0))] == ["routines_silent"]


def test_missing_daily_report_after_1630_is_high(tmp_path):
    root = root_with(tmp_path, updated=at(15, 0))
    assert [p["check"] for p in silent(root, at(16, 45))] == ["routines_silent"]
    assert silent(root, at(16, 29)) == []
    root_with(tmp_path, report={"date": "2026-10-13"})
    assert silent(root, at(16, 45)) == []


def test_fallback_report_does_not_count_as_claude(tmp_path):
    root = root_with(tmp_path, report={"fallback": True})
    assert [p["check"] for p in silent(root, at(16, 45))] == ["routines_silent"]


def test_weekend_and_holiday_never_alarm(tmp_path):
    root = root_with(tmp_path, updated=at(1, 0, 10))
    assert silent(root, at(17, 0, 10)) == []          # Saturday


def test_run_checks_includes_it(tmp_path):
    root = root_with(tmp_path, updated=at(8, 50))
    probs = H.run_checks(root, at(12, 0), gh=lambda a: json.dumps({"total_count": 1}), repo="o/r", git=lambda r, a: (_ for _ in ()).throw(RuntimeError()))
    assert "routines_silent" in {p["check"] for p in probs}


def test_watchdog_workflow_opens_comments_and_closes_the_issue():
    text = (ROOT / ".github" / "workflows" / "watchdog.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    on = doc.get("on", doc.get(True))
    crons = [c["cron"] for c in on["schedule"]]
    assert "15 11 * * 1-5" in crons                                  # 16:45 IST
    assert doc["permissions"]["issues"] == "write"
    assert "Claude routines not running (usage limit or credits?)" in text
    assert "routines-silent" in text and "gh api -X POST" in text and "state=closed" in text
    assert "issues/$num/comments" in text                           # comments when one is already open
    assert "fallback.yml/dispatches" in text
