import json
from datetime import datetime
from pathlib import Path

from simgold import incidents as inc
from simgold.incidents import IncidentMonitor, load_portfolio_safe
from simgold.market import IST

T0 = datetime(2026, 10, 13, 10, 0, tzinfo=IST).timestamp()


def mk(tmp_path, monkeypatch, url=True):
    if url:
        monkeypatch.setenv("ROUTINE_INCIDENT_URL", "https://example.invalid/fire")
        monkeypatch.setenv("ROUTINE_INCIDENT_TOKEN", "tok")
    else:
        monkeypatch.delenv("ROUTINE_INCIDENT_URL", raising=False)
    sent = []
    m = IncidentMonitor(tmp_path, poster=lambda text, u, t: sent.append(text) or True, clock=lambda: T0)
    return m, sent


def lines(tmp_path):
    f = next(tmp_path.glob("incidents-*.jsonl"))
    return [json.loads(x) for x in f.read_text().splitlines()]


def test_tick_exceptions_threshold(tmp_path, monkeypatch):
    m, sent = mk(tmp_path, monkeypatch)
    for _ in range(inc.TICK_EXC_THRESHOLD - 1):
        m.tick_failed(RuntimeError("boom"), now=T0)
    assert not sent and not list(tmp_path.glob("*.jsonl"))
    m.tick_failed(RuntimeError("boom"), now=T0)
    assert len(sent) == 1 and lines(tmp_path)[0]["kind"] == "tick_exceptions"
    m.tick_failed(RuntimeError("boom"), now=T0)          # same streak: no duplicate
    assert len(sent) == 1


def test_tick_success_resets_streak(tmp_path, monkeypatch):
    m, sent = mk(tmp_path, monkeypatch)
    for _ in range(2):
        m.tick_failed(RuntimeError("x"), now=T0)
    m.tick_ok()
    m.tick_failed(RuntimeError("x"), now=T0)
    assert not sent


def test_no_prices_only_during_market_hours(tmp_path, monkeypatch):
    m, sent = mk(tmp_path, monkeypatch)
    m.check_feed(True, now=T0 + 9 * 60)
    assert not sent
    m.check_feed(True, now=T0 + 11 * 60)
    assert len(sent) == 1 and lines(tmp_path)[0]["kind"] == "no_prices"
    m2, sent2 = mk(tmp_path / "b", monkeypatch)
    m2.check_feed(False, now=T0 + 3600)                  # closed market never alarms
    m2.check_feed(True, now=T0 + 3600 + 60)
    assert not sent2


def test_git_sync_three_in_a_row(tmp_path, monkeypatch):
    m, sent = mk(tmp_path, monkeypatch)
    m.sync_result(False, "x", now=T0); m.sync_result(False, "x", now=T0); m.sync_result(True)
    m.sync_result(False, "x", now=T0); m.sync_result(False, "x", now=T0)
    assert not sent
    m.sync_result(False, "rejected", now=T0)
    assert len(sent) == 1


def test_rate_limit_30min_and_3_per_day(tmp_path, monkeypatch):
    m, sent = mk(tmp_path, monkeypatch)
    m.report("a", "high", "1", now=T0)
    m.report("b", "high", "2", now=T0 + 10 * 60)        # too soon: logged, not posted
    m.report("c", "high", "3", now=T0 + 31 * 60)
    m.report("d", "high", "4", now=T0 + 62 * 60)
    m.report("e", "high", "5", now=T0 + 95 * 60)        # 4th post of the day: blocked
    assert len(sent) == 3
    assert len(lines(tmp_path)) == 5                      # everything is logged
    assert [bool(x.get("notified_at")) for x in lines(tmp_path)] == [True, False, True, True, False]


def test_never_crashes_and_unconfigured(tmp_path, monkeypatch):
    m, sent = mk(tmp_path, monkeypatch, url=False)
    m.report("a", "high", "x", now=T0)
    assert not sent and len(lines(tmp_path)) == 1
    monkeypatch.setenv("ROUTINE_INCIDENT_URL", "u"); monkeypatch.setenv("ROUTINE_INCIDENT_TOKEN", "t")

    def bad(*a):
        raise OSError("net down")
    m2 = IncidentMonitor(tmp_path / "z", poster=bad, clock=lambda: T0)
    m2.report("a", "high", "x", now=T0)                  # must not raise


def test_payload_capped():
    assert len(inc.build_text({"detail": "x" * 50_000, "a": 1})) <= inc.MAX_TEXT


GOOD = {"cash": 1000.0, "positions": {"GOLD": {"qty": 5, "avg_cost": 10.0, "high_water": 10.0, "opened": "2026-10-01"}}}


def test_state_recovery_from_backup(tmp_path):
    pf, bk = tmp_path / "portfolio.json", tmp_path / "portfolio.backup.json"
    pf.write_text("{not json"); bk.write_text(json.dumps(GOOD))
    p, rec = load_portfolio_safe(pf, bk, tmp_path, git_show_fn=lambda *a: None)
    assert p.cash == 1000.0 and p.qty("GOLD") == 5 and "backup" in rec
    assert (tmp_path / "portfolio.json.corrupt").exists()


def test_state_recovery_from_git_then_fresh(tmp_path):
    pf, bk = tmp_path / "portfolio.json", tmp_path / "none.json"
    pf.write_text("")
    p, rec = load_portfolio_safe(pf, bk, tmp_path, git_show_fn=lambda r, spec: json.dumps(GOOD))
    assert p.cash == 1000.0 and "HEAD~1" in rec
    pf.write_text("garbage")
    p, rec = load_portfolio_safe(pf, bk, tmp_path, git_show_fn=lambda *a: "also garbage")
    assert "NO good copy" in rec


def test_healthy_state_has_no_recovery(tmp_path):
    pf = tmp_path / "portfolio.json"
    pf.write_text(json.dumps(GOOD))
    assert load_portfolio_safe(pf, tmp_path / "b", tmp_path)[1] is None
