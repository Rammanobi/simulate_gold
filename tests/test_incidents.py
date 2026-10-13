import json
from datetime import datetime

from simgold import alerts as A, gitsync
from simgold.incidents import IncidentMonitor, load_portfolio_safe
from simgold.market import IST

T0 = datetime(2026, 10, 13, 10, 0, tzinfo=IST).timestamp()
ENV = {"ROUTINE_INCIDENT_URL": "https://example.invalid/fire", "ROUTINE_INCIDENT_TOKEN": "tok"}


def mk(tmp_path, env=ENV):
    sent = []
    m = IncidentMonitor(tmp_path / "logs", tmp_path / "state", poster=lambda text, u, t: sent.append(json.loads(text)) or True,
                        clock=lambda: T0, environ=env)
    return m, sent


def lines(tmp_path):
    f = next((tmp_path / "logs").glob("incidents-*.jsonl"))
    return [json.loads(x) for x in f.read_text().splitlines()]


def test_tick_exceptions_threshold_and_low_first(tmp_path):
    m, sent = mk(tmp_path)
    m.tick_failed(RuntimeError("boom 1"), now=T0)
    assert not sent and lines(tmp_path)[0]["severity"] == "low"          # a single transient error is LOW, logged only
    m.tick_failed(RuntimeError("boom 2"), now=T0)
    assert not sent
    m.tick_failed(RuntimeError("boom 3"), now=T0)
    assert len(sent) == 1 and sent[0]["category"] == "tick_exceptions" and sent[0]["severity"] == "high"
    m.tick_failed(RuntimeError("boom 4"), now=T0)                        # same streak, same incident
    assert len(sent) == 1


def test_success_resets_streak_and_resolves(tmp_path):
    m, sent = mk(tmp_path)
    for _ in range(3):
        m.tick_failed(RuntimeError("x"), now=T0)
    fp = sent[0]["fingerprint"]
    A.record_email(tmp_path / "state", fp, T0)          # pretend the routine emailed it
    m.tick_ok(now=T0 + 60)
    assert m.tick_failures == 0
    assert A.load_state(tmp_path / "state")["alerts"][fp]["status"] == "resolved"
    assert sent[-1]["mode"] == "resolved"               # the routine is told once, to send the Resolved email


def test_no_prices_only_in_market_hours_after_10_min(tmp_path):
    m, sent = mk(tmp_path)
    m.check_feed(True, now=T0 + 9 * 60)
    assert not sent
    m.check_feed(True, now=T0 + 11 * 60)
    assert len(sent) == 1 and sent[0]["category"] == "data_source_down" and sent[0]["severity"] == "medium"
    m2, sent2 = mk(tmp_path / "b")
    m2.check_feed(False, now=T0 + 3600)                  # a closed market never alarms
    m2.check_feed(True, now=T0 + 3600 + 60)
    assert not sent2


def test_git_push_failing_for_15_min(tmp_path):
    m, sent = mk(tmp_path)
    m.sync_result(False, "rejected", now=T0)
    m.sync_result(False, "rejected", now=T0 + 5 * 60)
    m.sync_result(False, "rejected", now=T0 + 10 * 60)
    assert not sent
    m.sync_result(False, "rejected", now=T0 + 15 * 60)
    assert len(sent) == 1 and sent[0]["category"] == "git_push_failing" and sent[0]["severity"] == "high"
    m.sync_result(True, now=T0 + 20 * 60)
    m.sync_result(False, "again", now=T0 + 21 * 60)       # the streak restarted
    assert len(sent) == 1


def test_gitsync_reports_to_monitor(tmp_path):
    class R:
        def __init__(self, rc): self.returncode, self.stdout, self.stderr = rc, "", "denied"
    seen = []

    class Mon:
        def sync_result(self, ok, detail="", now=None): seen.append(ok)
    assert not gitsync.sync(tmp_path, runner=lambda *a, **k: R(1), monitor=Mon())
    assert seen == [False]
    assert gitsync.sync(tmp_path, runner=lambda *a, **k: R(0), monitor=Mon()) and seen == [False, True]

    class Boom:
        def sync_result(self, *a, **k): raise RuntimeError("alerting broke")
    assert gitsync.sync(tmp_path, runner=lambda *a, **k: R(0), monitor=Boom())    # never breaks trading


def test_budget_applies_to_engine_posts(tmp_path):
    m, sent = mk(tmp_path)
    for i in range(8):
        m.report(f"k{chr(97 + i)}", "state_corrupt", f"step-{chr(97 + i)}", "x", now=T0 + i)
    assert len(sent) == 6 and len(lines(tmp_path)) == 8        # everything is logged, six wake the routine


def test_unconfigured_and_never_crashes(tmp_path):
    m, sent = mk(tmp_path, env={})
    m.report("a", "state_corrupt", "s", "x", now=T0)
    assert not sent and len(lines(tmp_path)) == 1

    def bad(*a):
        raise OSError("net down")
    m2 = IncidentMonitor(tmp_path / "z" / "logs", tmp_path / "z" / "state", poster=bad, clock=lambda: T0, environ=ENV)
    m2.report("a", "state_corrupt", "s", "x", now=T0)             # must not raise
    m2.tick_failed(ValueError("x")); m2.sync_result(False); m2.check_feed(True)


def test_state_corruption_sets_safe_mode(tmp_path):
    m, sent = mk(tmp_path)
    m.state_corrupt("portfolio.json unreadable", now=T0)
    assert A.safe_mode_on(tmp_path / "state") and sent[0]["category"] == "state_corrupt"


GOOD = {"cash": 1000.0, "floor": 99500.0, "liquidbees": 5.0, "schema": 1,
        "positions": {"GOLDBEES": {"qty": 5, "avg_cost": 10.0, "high_water": 10.0, "opened": "2026-10-01"}}}


def test_state_recovery_from_backup(tmp_path):
    pf, bk = tmp_path / "portfolio.json", tmp_path / "portfolio.backup.json"
    pf.write_text("{not json"); bk.write_text(json.dumps(GOOD))
    p, rec = load_portfolio_safe(pf, bk, tmp_path, git_show_fn=lambda *a: None)
    assert p.cash == 1000.0 and p.qty("GOLDBEES") == 5 and "backup" in rec
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
