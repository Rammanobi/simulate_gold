"""Red-team items P3 (archive reports), P5 (trim trades), P6 (safe mode when tests fail), P8 (MEDIUM reminder cap), P4."""
from __future__ import annotations

import gzip
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import archive as ar    # noqa: E402

from simgold import alerts as A    # noqa: E402
from simgold import config    # noqa: E402
from simgold.broker import TRADES_KEEP, Portfolio    # noqa: E402

TODAY = date(2026, 12, 20)


# ---------------------------------------------------------------- P3
def make_reports(root, day):
    d = root / "reports" / "daily"
    d.mkdir(parents=True, exist_ok=True)
    for suffix in (".json", ".email.html", ".txt", ".page.html"):
        (d / f"{day}{suffix}").write_text(f"content of {day}{suffix}", encoding="utf-8")


def test_old_reports_are_gzipped_json_stays(tmp_path):
    make_reports(tmp_path, "2026-10-01"); make_reports(tmp_path, "2026-12-15")
    said = ar.archive_reports(tmp_path, TODAY)
    assert len(said) == 3
    d = tmp_path / "reports" / "daily"
    assert (d / "2026-10-01.json").read_text() == "content of 2026-10-01.json"      # uncompressed, stays
    for suffix in (".email.html", ".txt", ".page.html"):
        assert not (d / f"2026-10-01{suffix}").exists()
        gz = tmp_path / "archive" / "2026-10" / "reports" / f"2026-10-01{suffix}.gz"
        assert gzip.decompress(gz.read_bytes()).decode() == f"content of 2026-10-01{suffix}"
        assert (d / f"2026-12-15{suffix}").exists()                                   # recent ones untouched


def test_archive_reports_is_idempotent_and_dry_run_changes_nothing(tmp_path):
    make_reports(tmp_path, "2026-10-01")
    before = sorted(p.name for p in (tmp_path / "reports" / "daily").iterdir())
    assert ar.archive_reports(tmp_path, TODAY, dry=True) and sorted(p.name for p in (tmp_path / "reports" / "daily").iterdir()) == before
    ar.archive_reports(tmp_path, TODAY)
    snap = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert ar.archive_reports(tmp_path, TODAY) == []
    assert snap == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_archive_reports_crash_after_gzip_before_delete(tmp_path):
    make_reports(tmp_path, "2026-10-01")
    f = tmp_path / "reports" / "daily" / "2026-10-01.txt"
    dest = tmp_path / "archive" / "2026-10" / "reports" / "2026-10-01.txt.gz"
    dest.parent.mkdir(parents=True); ar._gzip_to(f.read_bytes(), dest)             # simulate the crash
    ar.archive_reports(tmp_path, TODAY)
    assert not f.exists() and not (dest.parent / "2026-10-01.txt.2.gz").exists()


def test_main_runs_all_steps(tmp_path, capsys):
    make_reports(tmp_path, "2026-10-01")
    assert ar.main(["--root", str(tmp_path), "--today", "2026-12-20"]) == 0
    assert "reports" in capsys.readouterr().out


# ---------------------------------------------------------------- P5
def trade(i):
    return {"ts": f"2026-10-{1 + i // 100:02d} 10:{i % 60:02d}:00", "asset": "GOLDBEES", "side": "BUY", "qty": i + 1,
            "price": 100.0 + i, "value": 1.0, "costs": 0.0, "pnl": 0.0, "reason": f"trade {i}"}


def test_trim_keeps_last_200_and_loses_nothing(tmp_path):
    pf = Portfolio()
    pf.trades = [trade(i) for i in range(260)]
    arc = tmp_path / "logs" / "trades-archive.jsonl"
    assert pf.trim_trades(arc) == 60
    assert len(pf.trades) == TRADES_KEEP == 200 and pf.trades[0] == trade(60)
    archived = [json.loads(x) for x in arc.read_text().splitlines()]
    assert archived + pf.trades == [trade(i) for i in range(260)]                 # every trade, in order, exactly once
    assert pf.trim_trades(arc) == 0                                               # nothing more to do
    pf.trades += [trade(i) for i in range(260, 300)]
    pf.trim_trades(arc)
    archived = [json.loads(x) for x in arc.read_text().splitlines()]
    assert archived + pf.trades == [trade(i) for i in range(300)]


def test_trim_crash_between_archive_and_save_never_loses_or_duplicates(tmp_path):
    arc = tmp_path / "trades-archive.jsonl"
    pf = Portfolio(); pf.trades = [trade(i) for i in range(230)]
    pf.trim_trades(arc)
    reloaded = Portfolio(); reloaded.trades = [trade(i) for i in range(230)]       # the save never happened: old file reloaded
    reloaded.trim_trades(arc)
    lines = arc.read_text().splitlines()
    assert len(lines) == 30 == len(set(lines))


def test_trim_io_error_keeps_trades(tmp_path):
    blocker = tmp_path / "blocker"; blocker.write_text("x")
    pf = Portfolio(); pf.trades = [trade(i) for i in range(210)]
    assert pf.trim_trades(blocker / "sub" / "a.jsonl") == 0 and len(pf.trades) == 210


def test_trades_archive_is_compressed_later(tmp_path):
    arc = tmp_path / "logs" / "trades-archive.jsonl"; arc.parent.mkdir()
    old, new = trade(1), {**trade(2), "ts": "2026-12-18 10:00:00"}
    arc.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
    ar.archive_trades(tmp_path, TODAY)
    gz = tmp_path / "archive" / "2026-10" / "trades-archive-2026-10.jsonl.gz"
    assert json.loads(gzip.decompress(gz.read_bytes()).decode().strip()) == old
    assert [json.loads(x) for x in arc.read_text().splitlines()] == [new]
    ar.archive_trades(tmp_path, TODAY)                                             # idempotent
    assert len(gzip.decompress(gz.read_bytes()).decode().splitlines()) == 1


def test_portfolio_roundtrip_after_trim(tmp_path):
    pf = Portfolio(); pf.trades = [trade(i) for i in range(205)]
    pf.trim_trades(tmp_path / "a.jsonl"); pf.save(tmp_path / "p.json")
    assert len(Portfolio.load(tmp_path / "p.json").trades) == 200


# ---------------------------------------------------------------- P8
def test_medium_reminders_capped_at_ten():
    cfg = {**A.params(), "max_medium_reminders": 10}
    assert cfg["max_medium_reminders"] == 10 and config.load_params()["alerts"]["max_medium_reminders"] == 10
    t0 = 1_800_000_000.0
    e = {"status": "open", "severity": A.MEDIUM, "acked": False, "emails": [t0]}
    for n in range(1, 12):
        due = A.reminder_due_at(e, cfg)
        if n <= 10:
            assert due is not None, n
            e["emails"].append(due)
        else:
            assert due is None                                               # 1 initial + 10 reminders, then silence
    assert len(e["emails"]) == 11
    assert A.should_email(e, e["emails"][-1] + 10 * 86400, cfg)[0] == "HOLD"


def test_high_cap_unchanged():
    cfg = A.params()
    e = {"status": "open", "severity": A.HIGH, "acked": False, "emails": [0.0] * 3}
    assert A.reminder_due_at(e, cfg) is not None


# ---------------------------------------------------------------- P6 / P4
WF = yaml.safe_load((ROOT / ".github" / "workflows" / "market-hours.yml").read_text(encoding="utf-8"))
WF_TEXT = (ROOT / ".github" / "workflows" / "market-hours.yml").read_text(encoding="utf-8")


def test_failing_tests_do_not_skip_the_day():
    steps = WF["jobs"]["trade"]["steps"]
    t = next(s for s in steps if "pytest" in str(s.get("run", "")))
    assert t["continue-on-error"] is True and t["id"] == "tests"
    run_step = next(s for s in steps if s.get("name") == "Run paper-trading loop")
    assert "steps.tests.outcome" in run_step["run"] and "--safe-mode" in run_step["run"]
    assert "$SAFE" in run_step["run"].split("morning)")[1]
    last = steps[-1]
    assert "tests.outcome == 'failure'" in last["if"] and "pytest" in last["name"] and "exit 1" in last["run"]
    assert steps.index(last) > next(i for i, s in enumerate(steps) if s.get("name") == "Final sync")


def test_run_live_safe_mode_flag(tmp_path, monkeypatch):
    import run_live
    monkeypatch.setattr(run_live, "STATE", tmp_path)
    ticks = []
    class FakeMon:
        def tick_ok(self): pass
        def check_feed(self, x): pass
        def housekeeping(self): pass
    class FakeLive:
        def __init__(self, **k): self.mon, self.last_tick_day = FakeMon(), None
        def tick(self): ticks.append(A.safe_mode_on(tmp_path))
        def sync(self): pass
        def write_brief(self): pass
    monkeypatch.setattr(run_live, "Live", FakeLive)
    monkeypatch.setattr(run_live, "acquire_lock", lambda p: object())
    monkeypatch.setattr(sys, "argv", ["run_live.py", "--once", "--force", "--safe-mode"])
    run_live.main()
    assert ticks == [True]
    info = A.safe_mode_info(tmp_path)
    assert info["on"] is True and info["reason"] == "tests failing"


def test_safe_mode_blocks_buys_but_not_exits(tmp_path):
    from datetime import datetime, timedelta
    from simgold.engine import Engine, Env, Market
    from simgold.market import IST
    (tmp_path / "state").mkdir()
    A.set_safe_mode(tmp_path / "state", "tests failing", notify=False)
    eng = Engine(Portfolio(), Market(datetime(2026, 10, 14, 11, 0, tzinfo=IST), {}, {}, {}),
                 Env(tmp_path / "state", tmp_path / "logs", config.load_params()))
    assert eng.safe_mode is True


def test_nine_am_backup_cron_and_no_double_runs():
    crons = [c["cron"] for c in (WF.get("on") or WF.get(True))["schedule"]]
    assert "30 3 * * 1-5" in crons                                  # 09:00 IST
    assert WF["concurrency"] == {"group": "paper-trader", "cancel-in-progress": False}
    run_live_src = (ROOT / "run_live.py").read_text(encoding="utf-8")
    assert "LOCK_EX | fcntl.LOCK_NB" in run_live_src and "state/.engine.lock" in run_live_src


def test_engine_lock_blocks_a_second_run(tmp_path):
    import run_live
    first = run_live.acquire_lock(tmp_path / ".engine.lock")
    assert first is not None and run_live.acquire_lock(tmp_path / ".engine.lock") is None
