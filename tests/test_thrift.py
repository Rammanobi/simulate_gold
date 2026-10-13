"""Thrift mode: tools/budget.py, state/budget.json, the engine's fast-lane cap, the report line, the playbooks' step 0."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import budget as budget_tool    # noqa: E402

from simgold import budget, config, fastlane, report    # noqa: E402
from simgold.market import IST    # noqa: E402

P = config.load_params()
NOW = datetime(2026, 10, 14, 11, 0, tzinfo=IST)


@pytest.mark.parametrize("t,over,mode", [("ccr_promotional", False, "NORMAL"), ("CCR_Promotional", False, "NORMAL"),
                                         ("five_hour", False, "THRIFT"), ("", False, "THRIFT"), (None, False, "THRIFT"),
                                         ("weird", False, "THRIFT"), ("ccr_promotional", True, "THRIFT")])
def test_mode_for(t, over, mode):
    assert budget.mode_for(t, over) == mode


def test_cli_mode_writes_state_and_show_reads_it(tmp_path, capsys):
    assert budget_tool.main(["--root", str(tmp_path), "mode", "--rate-limit-type", "ccr_promotional", "--overage", "false"]) == 0
    assert capsys.readouterr().out.strip() == "NORMAL"
    rec = json.loads((tmp_path / "state" / "budget.json").read_text())
    assert set(rec) == {"mode", "rate_limit_type", "overage", "checked_at"} and rec["mode"] == "NORMAL"
    budget_tool.main(["--root", str(tmp_path), "mode", "--rate-limit-type", "five_hour", "--overage", "true"])
    assert capsys.readouterr().out.strip() == "THRIFT"
    budget_tool.main(["--root", str(tmp_path), "show"])
    assert capsys.readouterr().out.strip() == "THRIFT"
    budget_tool.main(["--root", str(tmp_path), "mode"])          # no type at all: unknown = THRIFT
    assert capsys.readouterr().out.strip() == "THRIFT"


def test_read_mode_missing_corrupt(tmp_path):
    assert budget.read_mode(tmp_path) == "NORMAL"
    (tmp_path / "budget.json").write_text("{not json")
    assert budget.read_mode(tmp_path) == "THRIFT"
    (tmp_path / "budget.json").write_text(json.dumps({"mode": "NORMAL"}))
    assert budget.read_mode(tmp_path) == "NORMAL"


def test_params_have_the_budget_block():
    assert P["budget"] == {"thrift_fastlane_max_per_day": 2, "thrift_hourly_opus": False, "thrift_weekly_short": True,
                           "thrift_incident_opus_only_high": True}
    assert P["fast_lane"]["max_runs_per_day"] == 4


def test_engine_cap_follows_the_mode(tmp_path):
    assert budget.fastlane_cap(P, tmp_path) == 4
    budget.write(tmp_path, "five_hour")
    assert budget.fastlane_cap(P, tmp_path) == 2
    runs = {"date": NOW.date().isoformat(), "n": 2, "last_ts": (NOW - timedelta(hours=2)).isoformat()}
    assert fastlane.can_fire(runs, NOW, P)[0] is True
    ok, why = fastlane.can_fire(runs, NOW, P, cap=2)
    assert not ok and "daily limit" in why
    assert fastlane.limit_used_up(runs, NOW, P, cap=2) and not fastlane.limit_used_up(runs, NOW, P)


def test_engine_uses_thrift_cap_end_to_end(tmp_path):
    from simgold.engine import Engine, Env, Market
    from simgold.broker import Portfolio
    (tmp_path / "state").mkdir()
    budget.write(tmp_path / "state", "five_hour")
    env = Env(tmp_path / "state", tmp_path / "logs", P)
    eng = Engine(Portfolio(), Market(NOW, {}, {}, {}), env)
    assert eng.fast_cap == 2


def _root_for_report(tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_report_shows_thrift_line_only_in_thrift(tmp_path):
    root = _root_for_report(tmp_path)
    day = report.build_day("2026-10-14", root)
    assert day["thrift"] is False
    assert report.THRIFT_LINE not in report.render_email(day) and report.THRIFT_LINE not in report.render_text(day)
    budget.write(root / "state", "weekly_limit")
    day = report.build_day("2026-10-14", root)
    assert day["thrift"] is True
    assert "Thrift mode: running on plan limits" in report.THRIFT_LINE
    assert report.THRIFT_LINE in report.render_email(day) and report.THRIFT_LINE in report.render_text(day)


def test_report_notes_fallback_dates(tmp_path):
    root = _root_for_report(tmp_path)
    (root / "state" / "fallback.json").write_text(json.dumps({"dates": ["2026-10-12", "2026-10-13"]}))
    day = report.build_day("2026-10-14", root)
    assert "Fallback was used on 2026-10-12, 2026-10-13" in report.render_text(day)


@pytest.mark.parametrize("name", ["hourly", "premarket", "weekly", "incident", "fastlane", "daily_report"])
def test_every_playbook_has_step_zero(name):
    s = (ROOT / "routines" / f"{name}.md").read_text(encoding="utf-8")
    assert "## 0. Budget check" in s and "get_session" in s and "tools/budget.py mode" in s
    assert s.index("## 0. Budget check") < s.index("## Steps" if name != "incident" else "## 1. Context")
    assert "unavailable" in s and "THRIFT" in s


def test_thrift_rules_are_in_the_right_playbooks():
    t = lambda n: (ROOT / "routines" / f"{n}.md").read_text(encoding="utf-8")
    assert "do NOT spawn the Opus subagent" in t("hourly")
    assert "no Haiku research fan-out" in t("premarket") and "3 most important" in t("premarket")
    assert "short version" in t("weekly") and "No rule proposals unless the evidence is strong" in t("weekly")
    assert "only for HIGH severity or COMPLEX" in t("incident")
    assert "unchanged" in t("fastlane").split("## 0. Budget check")[1].split("## Steps")[0]


def test_budget_json_is_a_routine_owned_file():
    assert "`state/budget.json`" in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
