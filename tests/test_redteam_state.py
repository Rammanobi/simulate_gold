"""Red-team regression tests: truncated / corrupt / old-schema state files (portfolio, alerts, safe mode, news)."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from simgold import alerts, market
from simgold.broker import Portfolio
from simgold.incidents import load_portfolio_safe
from simgold.market import IST
from simgold.news import NewsBook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import healthcheck as H  # noqa: E402

GOOD = {"cash": 0.0, "liquidbees": 50000.0, "floor": 101000.0, "best_profit": 2500.0, "schema": 1,
        "positions": {"GOLDBEES": {"qty": 500, "avg_cost": 100.0, "stop": 98.8, "last_price": 101.0, "high_water": 101.0}}}


def _load(tmp_path, text, backup=GOOD):
    f = tmp_path / "portfolio.json"
    f.write_text(text)
    b = tmp_path / "portfolio.backup.json"
    if backup is not None:
        b.write_text(json.dumps(backup))
    return load_portfolio_safe(f, b, tmp_path, git_show_fn=lambda *a: None)


@pytest.mark.parametrize("text", [
    '{"cash": 1000',                                                     # truncated mid-write
    "",
    '{"cash":"abc","liquidbees":5,"floor":99500,"positions":{}}',
    '{"cash":NaN,"liquidbees":5,"floor":99500,"positions":{}}',
    '{"cash":null,"liquidbees":5,"floor":99500,"positions":{}}',
    '{"cash":0,"liquidbees":1e400,"floor":99500,"positions":{}}',
    '{"cash":0,"liquidbees":5,"floor":99500,"positions":[]}',
    '{"cash":0,"liquidbees":5,"floor":99500,"positions":{"GOLDBEES":{"qty":-5,"avg_cost":100}}}',
    '{"cash":0,"liquidbees":5,"floor":99500,"positions":{"GOLDBEES":{"qty":2.5,"avg_cost":100}}}',
    '{"cash":-50000,"liquidbees":5,"floor":99500,"positions":{}}',
    '{"cash":0,"liquidbees":5,"floor":99500,"positions":{},"counters":[],"cooldowns":5}',
    '{"cash":0,"liquidbees":5,"floor":"x","positions":{}}',
    "[]",
])
def test_corrupt_portfolio_is_restored_from_backup_never_traded_on(tmp_path, text):
    pf, rec = _load(tmp_path, text)
    assert rec and "restored from state/portfolio.backup.json" in rec
    assert pf.floor == 101000.0 and pf.positions["GOLDBEES"].qty == 500
    assert (tmp_path / "portfolio.json.corrupt").exists()


def test_corrupt_portfolio_without_backup_starts_empty_and_says_so(tmp_path):
    pf, rec = _load(tmp_path, "{", backup=None)
    assert rec and "NO good copy" in rec and pf.liquidbees == 0 and pf.cash == 100000.0


def test_new_file_missing_schema_is_not_migrated(tmp_path):
    """Before the fix: drop "schema" and the legacy migration moved cash into LIQUIDBEES (wiping 50,000 of LIQUIDBEES)
    and reset the floor from 1,01,000 to 99,500."""
    d = {k: v for k, v in GOOD.items() if k != "schema"}
    f = tmp_path / "p.json"
    f.write_text(json.dumps(d))
    pf = Portfolio.load(f)
    assert (pf.cash, pf.liquidbees, pf.floor, pf.schema) == (0.0, 50000.0, 101000.0, 1)


def test_schema1_file_missing_the_floor_is_corrupt_not_a_lower_floor(tmp_path):
    d = {k: v for k, v in GOOD.items() if k != "floor"}
    pf, rec = _load(tmp_path, json.dumps(d))
    assert rec and pf.floor == 101000.0


def test_old_schema_file_still_migrates(tmp_path):
    old = {"cash": 100000.0, "positions": {}, "trades": []}
    f = tmp_path / "p.json"
    f.write_text(json.dumps(old))
    pf = Portfolio.load(f)
    assert (pf.cash, pf.liquidbees, pf.floor) == (0.0, 100000.0, 99500.0)


def test_save_is_atomic_and_one_line(tmp_path):
    f = tmp_path / "portfolio.json"
    pf = Portfolio.load(Path("/nonexistent"))
    pf.save(f)
    text = f.read_text()
    assert text.count("\n") == 1 and json.loads(text)["schema"] == 1
    assert not list(tmp_path.glob(".*tmp"))
    assert Portfolio.load(f).cash == pf.cash


# ------------------------------------------------------------------ alerts.json and safe_mode.json
@pytest.mark.parametrize("text", ["{", "[]", "[1]", '"str"', '{"fires":"x"}', '{"alerts":[]}', '{"alerts":{"a":5}}',
                                  '{"pending":{}}', '{"fires":{"date":"x","n":"many"}}', '{"alerts":{"x":{"emails":5}}}',
                                  '{"last_high_fire":"soon"}'])
def test_corrupt_alerts_json_never_breaks_alerting(tmp_path, text):
    (tmp_path / "alerts.json").write_text(text)
    now = time.time()
    d = alerts.raise_alert(tmp_path, "engine", "tick", "tick_exceptions", "boom", now=now)
    assert d["fire"] and d["new"] and d["severity"] == "high"
    alerts.evaluate_safe_mode(tmp_path, now)
    alerts.reminders_due(alerts.load_state(tmp_path), now + 7200)
    alerts.take_pending(tmp_path)
    assert json.loads((tmp_path / "alerts.json").read_text())["alerts"]          # written back in a sane shape
    assert alerts.main(["--state", str(tmp_path), "gate", "--component", "c", "--step", "s", "--category", "warning"]) == 0


@pytest.mark.parametrize("text", ["{", "[]", "5", "null", '"on"', '{"on": "false"}'])
def test_corrupt_safe_mode_fails_closed_and_never_crashes(tmp_path, text):
    (tmp_path / "safe_mode.json").write_text(text)
    assert alerts.safe_mode_on(tmp_path) is True
    nine = datetime(2026, 10, 15, 8, 45, tzinfo=IST).timestamp()
    assert alerts.auto_clear_safe_mode(tmp_path, nine, True) is False          # unknown 'since': never auto-cleared
    assert alerts.clear_safe_mode(tmp_path, "owner") is True and alerts.safe_mode_on(tmp_path) is False


# ------------------------------------------------------------------ news.json
@pytest.mark.parametrize("text", ["{", "[]", '{"a": 5}', '{"a": {"title": "x", "ts": "y"}}', '{"a": {"title": 5, "ts": 1}}'])
def test_corrupt_news_json_does_not_stop_the_engine_starting(tmp_path, text):
    p = tmp_path / "news.json"
    p.write_text(text)
    nb = NewsBook(p)
    assert nb.items == {}
    nb.sentiment()


def test_news_json_keeps_good_rows(tmp_path):
    p = tmp_path / "news.json"
    p.write_text(json.dumps({"a": {"title": "Gold rises", "ts": time.time(), "id": "a"}, "b": 7}))
    assert list(NewsBook(p).items) == ["a"]


# ------------------------------------------------------------------ healthcheck on corrupt files
def _hc_root(tmp_path, files: dict):
    (tmp_path / "state").mkdir()
    for name, text in files.items():
        (tmp_path / "state" / name).write_text(text)
    return tmp_path


NOW = datetime(2026, 10, 13, 17, 0, tzinfo=IST)       # market closed: only state/invariant checks run


def _checks(root, now=NOW):
    return {p["check"]: p for p in H.run_checks(root, now, lambda a: '{"total_count": 1}', "o/r", lambda r, a: "")}


def test_healthcheck_reports_wrong_shapes_instead_of_crashing(tmp_path):
    root = _hc_root(tmp_path, {"portfolio.json": '{"cash":0,"liquidbees":5,"floor":99500,"positions":[1]}',
                               "alerts.json": "[1]", "safe_mode.json": "{"})
    c = _checks(root)
    assert c["state_values"]["severity"] == "high"
    assert any(p["detail"].startswith("alerts.json") or p["detail"].startswith("safe_mode.json")
               for p in H.run_checks(root, NOW, lambda a: "{}", "o/r", lambda r, a: "") if p["check"] == "state_parse")
    assert not any(k.endswith("_crashed") for k in c)


def test_healthcheck_crash_in_a_check_becomes_a_problem(tmp_path, monkeypatch):
    root = _hc_root(tmp_path, {})
    monkeypatch.setattr(H, "check_memory", lambda r: 1 / 0)
    c = _checks(root)
    assert c["memory_crashed"]["category"] == "workflow_failed"


def test_healthcheck_warns_in_december_when_next_years_holidays_are_missing(tmp_path):
    root = _hc_root(tmp_path, {})
    assert "holiday_calendar_missing" not in _checks(root)
    dec = datetime(2026, 12, 2, 17, 0, tzinfo=IST)
    if not market.holidays_known(2027):
        assert _checks(root, dec)["holiday_calendar_missing"]["severity"] == "medium"
