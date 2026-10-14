"""Red-team regression tests: engine git sync racing routine/watchdog pushes, with real local bare repositories.

The engine must never crash the loop, never lose portfolio state, and never leave conflict markers or invalid JSON."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from simgold import alerts, gitsync
from simgold.broker import Portfolio

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def g(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r


def _clone(td: Path, origin: Path, name: str) -> Path:
    d = td / name
    g(td, "clone", "-q", str(origin), str(d))
    g(d, "config", "user.email", f"{name}@example.invalid")
    g(d, "config", "user.name", name)
    g(d, "config", "commit.gpgsign", "false")
    return d


def _alerts(extra: dict | None = None) -> dict:
    st = alerts._empty()
    st["fires"] = {"date": "2026-10-14", "n": 1, "ts": [1.0]}
    st["alerts"]["engine|tick|"] = {**alerts._ENTRY_DEFAULTS, "fp": "engine|tick|", "status": "open", "emails": [],
                                    "first_seen": 1.0, "last_seen": 1.0, "count": 1, "severity": "high"}
    st["alerts"].update(extra or {})
    return st


def _write_alerts(d: Path, st: dict):
    (d / "state" / "alerts.json").write_text(json.dumps(st, indent=1, sort_keys=True))


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    """origin (bare), engine clone, routine clone; the seed has the real folder layout and .gitattributes."""
    monkeypatch.setenv("PYTHONPATH", str(ROOT))          # the merge driver runs `python -m simgold.alerts`
    origin = tmp_path / "origin.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = _clone(tmp_path, origin, "seed")
    for sub in ("state", "logs", "memory/daily", "reports/daily"):
        (seed / sub).mkdir(parents=True)
        (seed / sub / ".gitkeep").write_text("")
    if (ROOT / ".gitattributes").exists():
        shutil.copy(ROOT / ".gitattributes", seed / ".gitattributes")
    _write_alerts(seed, _alerts())
    pf = Portfolio()
    pf.cash, pf.liquidbees = 0.0, 100000.0
    pf.save(seed / "state" / "portfolio.json")
    (seed / "memory" / "brief.md").write_text("brief v0\n")
    (seed / "logs" / "decisions-2026-09-01.jsonl").write_text('{"action":"BUY"}\n')
    g(seed, "add", "-A")
    g(seed, "commit", "-qm", "seed")
    g(seed, "push", "-q", "origin", "main")
    return origin, _clone(tmp_path, origin, "engine"), _clone(tmp_path, origin, "routine")


def _origin_file(origin: Path, path: str) -> str:
    return g(origin, "show", f"main:{path}").stdout


def _no_markers(d: Path):
    for f in d.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            assert "<<<<<<<" not in f.read_text(errors="replace"), f


def test_routine_guidance_push_races_engine_state_push(repos):
    origin, eng, rout = repos
    (rout / "state" / "guidance.json").write_text('{"updated": "2026-10-14T10:15:00+05:30", "bias": {"GOLD": "neutral"}}')
    g(rout, "add", "state/guidance.json"); g(rout, "commit", "-qm", "routine: bias"); g(rout, "push", "-q")
    pf = Portfolio.load(eng / "state" / "portfolio.json")
    pf.liquidbees = 60000.0
    pf.save(eng / "state" / "portfolio.json")
    assert gitsync.sync(eng)
    assert json.loads(_origin_file(origin, "state/portfolio.json"))["liquidbees"] == 60000.0
    assert "GOLD" in _origin_file(origin, "state/guidance.json")
    assert (eng / "state" / "guidance.json").exists()                  # pulled in for the next tick
    _no_markers(eng)


def test_alerts_json_concurrent_edits_merge_as_json(repos):
    """Before the fix a line merge produced valid-looking JSON that moved the routine's 'resolved' onto the engine's
    new alert (or invalid JSON with conflict markers when the autostash path was hit)."""
    origin, eng, rout = repos
    a = _alerts({"wd|ticks stale|": {**alerts._ENTRY_DEFAULTS, "fp": "wd|ticks stale|", "emails": [9.0], "first_seen": 5.0,
                                     "last_seen": 5.0, "count": 1}})
    a["alerts"]["engine|tick|"].update(status="resolved", resolved_at=6.0)
    a["fires"] = {"date": "2026-10-14", "n": 2, "ts": [1.0, 5.0]}
    _write_alerts(rout, a)
    g(rout, "add", "-A"); g(rout, "commit", "-qm", "watchdog"); g(rout, "push", "-q")
    b = _alerts({"engine|git sync|": {**alerts._ENTRY_DEFAULTS, "fp": "engine|git sync|", "first_seen": 3.0,
                                      "last_seen": 3.0, "count": 2}})
    b["alerts"]["engine|tick|"]["count"] = 4
    b["fires"] = {"date": "2026-10-14", "n": 2, "ts": [1.0, 3.0]}
    _write_alerts(eng, b)
    assert gitsync.sync(eng)
    m = json.loads(_origin_file(origin, "state/alerts.json"))
    assert set(m["alerts"]) == {"engine|tick|", "wd|ticks stale|", "engine|git sync|"}
    assert m["alerts"]["engine|tick|"]["status"] == "resolved" and m["alerts"]["engine|tick|"]["count"] == 4
    assert m["alerts"]["engine|git sync|"]["status"] == "open"
    assert m["fires"]["ts"] == [1.0, 3.0, 5.0] and m["fires"]["n"] == 3
    _no_markers(eng)


def test_two_engines_never_splice_portfolios(repos):
    """A second engine (e.g. a local run) pushed a different portfolio: the result is one whole, valid portfolio."""
    origin, eng, rout = repos
    other = Portfolio.load(rout / "state" / "portfolio.json")
    other.liquidbees, other.floor = 70000.0, 100100.0
    other.save(rout / "state" / "portfolio.json")
    g(rout, "add", "-A"); g(rout, "commit", "-qm", "other engine"); g(rout, "push", "-q")
    mine = Portfolio.load(eng / "state" / "portfolio.json")
    mine.liquidbees, mine.floor = 40000.0, 100200.0
    mine.save(eng / "state" / "portfolio.json")
    assert gitsync.sync(eng)
    got = json.loads(_origin_file(origin, "state/portfolio.json"))
    assert (got["liquidbees"], got["floor"]) == (40000.0, 100200.0)    # the syncing engine's whole file, never a mix
    _no_markers(eng)


def test_rebase_conflict_on_routine_owned_file_resolves(repos):
    origin, eng, rout = repos
    (rout / "memory" / "brief.md").write_text("brief from the daily report\n")
    g(rout, "add", "-A"); g(rout, "commit", "-qm", "routine brief"); g(rout, "push", "-q")
    (eng / "memory" / "brief.md").write_text("brief from the engine\n")
    assert gitsync.sync(eng)
    assert _origin_file(origin, "memory/brief.md") == "brief from the engine\n"
    _no_markers(eng)


def test_modify_delete_conflict_falls_back_to_a_merge_and_keeps_engine_files(repos):
    """archive.py deleted a log the engine had appended to: the rebase cannot finish. Before the fix every sync
    failed for the rest of the day (and the final push too), so the session's portfolio state never reached main."""
    origin, eng, rout = repos
    g(rout, "rm", "-q", "logs/decisions-2026-09-01.jsonl"); g(rout, "commit", "-qm", "archive"); g(rout, "push", "-q")
    with open(eng / "logs" / "decisions-2026-09-01.jsonl", "a") as fh:
        fh.write('{"action":"SELL"}\n')
    pf = Portfolio.load(eng / "state" / "portfolio.json")
    pf.liquidbees = 55555.0
    pf.save(eng / "state" / "portfolio.json")
    assert gitsync.sync(eng)
    assert json.loads(_origin_file(origin, "state/portfolio.json"))["liquidbees"] == 55555.0
    assert "SELL" in _origin_file(origin, "logs/decisions-2026-09-01.jsonl")
    assert g(origin, "rev-list", "--merges", "main").stdout.strip()       # a merge commit, history not rewritten
    _no_markers(eng)


def test_failed_commit_does_not_pull_and_leaves_no_conflict_markers(repos):
    origin, eng, rout = repos
    _write_alerts(rout, _alerts({"x|y|": {**alerts._ENTRY_DEFAULTS, "fp": "x|y|"}}))
    g(rout, "add", "-A"); g(rout, "commit", "-qm", "watchdog"); g(rout, "push", "-q")
    hook = eng / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    _write_alerts(eng, _alerts({"engine|z|": {**alerts._ENTRY_DEFAULTS, "fp": "engine|z|"}}))
    assert gitsync.sync(eng) is False
    json.loads((eng / "state" / "alerts.json").read_text())             # still valid, still the engine's copy
    assert "engine|z|" in (eng / "state" / "alerts.json").read_text()
    _no_markers(eng)
    hook.unlink()
    assert gitsync.sync(eng)                                            # recovers on the next sync


def test_missing_sync_folder_does_not_disable_git_add(repos):
    origin, eng, rout = repos
    shutil.rmtree(eng / "reports")
    pf = Portfolio.load(eng / "state" / "portfolio.json")
    pf.liquidbees = 12345.0
    pf.save(eng / "state" / "portfolio.json")
    assert gitsync.sync(eng)
    assert json.loads(_origin_file(origin, "state/portfolio.json"))["liquidbees"] == 12345.0


def test_push_rejected_repeatedly_keeps_state_and_recovers(repos):
    origin, eng, rout = repos
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n")
    hook.chmod(0o755)
    pf = Portfolio.load(eng / "state" / "portfolio.json")
    pf.liquidbees = 77777.0
    pf.save(eng / "state" / "portfolio.json")

    class Mon:
        seen = []

        def sync_result(self, ok, detail=""):
            self.seen.append(ok)
    mon = Mon()
    for _ in range(3):
        assert gitsync.sync(eng, monitor=mon) is False
    assert mon.seen == [False, False, False]
    assert Portfolio.load(eng / "state" / "portfolio.json").liquidbees == 77777.0       # nothing lost locally
    hook.unlink()
    assert gitsync.sync(eng, monitor=mon) and mon.seen[-1] is True
    assert json.loads(_origin_file(origin, "state/portfolio.json"))["liquidbees"] == 77777.0


def test_git_timeouts_never_raise(tmp_path):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 90)
    assert gitsync.sync(tmp_path, runner=boom) is False


def test_merge_driver_is_robust_to_garbage(tmp_path):
    base, ours, theirs = (tmp_path / n for n in ("b", "o", "t"))
    base.write_text("{")
    ours.write_text("not json")
    theirs.write_text(json.dumps(_alerts()))
    assert alerts.main(["merge-file", str(base), str(ours), str(theirs)]) == 0
    assert "engine|tick|" in json.loads(ours.read_text())["alerts"]


# ------------------------------------------------------------------ two engine runs at once
def test_second_engine_run_cannot_take_the_lock(tmp_path):
    import run_live
    a = run_live.acquire_lock(tmp_path / ".engine.lock")
    assert a is not None
    assert run_live.acquire_lock(tmp_path / ".engine.lock") is None
    code = ("import sys; sys.path.insert(0, %r); import run_live, pathlib; "
            "print(run_live.acquire_lock(pathlib.Path(%r)) is None)") % (str(ROOT), str(tmp_path / ".engine.lock"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert out.stdout.strip() == "True"
    a.close()
    b = run_live.acquire_lock(tmp_path / ".engine.lock")
    assert b is not None
    b.close()


def test_lock_and_temp_files_are_never_committed():
    ign = (ROOT / ".gitignore").read_text()
    assert "state/.engine.lock" in ign and "state/.*.tmp" in ign
    assert "state/alerts.json merge=simgold-alerts" in (ROOT / ".gitattributes").read_text()


# ------------------------------------------------------------------ append-only logs (14 Oct 2026)
HEAD_T = "time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER\n"


def _tick(hhmm: str) -> str:
    return f"{hhmm},100017.26,99500.00,0.00,100017.26,120.15,204.65,467.55\n"


def _dec(hhmm: str, action: str = "BLOCK") -> str:
    return json.dumps({"ts": f"2026-10-14T{hhmm}:00+05:30", "time": hhmm, "action": action}) + "\n"


def test_stale_run_never_replaces_the_morning_ticks(repos):
    """14 Oct 2026: a run queued behind the morning session started on the 10:16 commit (no ticks file yet), wrote
    its own ticks from 12:21, and its first sync (`pull --rebase -X theirs`) replaced the 124 morning rows on main."""
    origin, eng, rout = repos
    morning = rout          # plays the morning engine run, which pushed first
    (morning / "logs" / "ticks-2026-10-14.csv").write_text(HEAD_T + "".join(_tick(f"10:{m:02d}") for m in range(17, 60)))
    (morning / "logs" / "decisions-2026-10-14.jsonl").write_text(_dec("10:20") + _dec("11:05", "SKIP"))
    g(morning, "add", "-A"); g(morning, "commit", "-qm", "paper-trade: 12:20 IST"); g(morning, "push", "-q")
    # the stale afternoon run: its checkout predates the file (add/add on the rebase)
    (eng / "logs" / "ticks-2026-10-14.csv").write_text(HEAD_T + _tick("12:21") + _tick("12:22"))
    (eng / "logs" / "decisions-2026-10-14.jsonl").write_text(_dec("12:21"))
    assert gitsync.sync(eng)
    rows = _origin_file(origin, "logs/ticks-2026-10-14.csv").splitlines()
    assert rows[0] == HEAD_T.strip() and rows.count(HEAD_T.strip()) == 1
    times = [r.split(",")[0] for r in rows[1:]]
    assert times[0] == "10:17" and times[-2:] == ["12:21", "12:22"] and len(times) == 43 + 2
    assert times == sorted(times)
    dec = _origin_file(origin, "logs/decisions-2026-10-14.jsonl").splitlines()
    assert [json.loads(x)["time"] for x in dec] == ["10:20", "11:05", "12:21"]
    _no_markers(eng)


def test_concurrent_appends_to_the_same_ticks_file_keep_both_sides(repos):
    """14 Oct 2026, 14:59: a run started on the 14:25 commit appended from 14:54 while main had 14:26-14:49 from the
    previous run; `-X theirs` kept only one side, leaving a 14:26-14:53 gap."""
    origin, eng, rout = repos
    for d in (eng, rout):
        g(d, "pull", "-q")
    (rout / "logs" / "ticks-2026-10-14.csv").write_text(HEAD_T + _tick("14:25"))
    g(rout, "add", "-A"); g(rout, "commit", "-qm", "14:25"); g(rout, "push", "-q")
    g(eng, "pull", "-q")
    with open(rout / "logs" / "ticks-2026-10-14.csv", "a") as fh:
        fh.write(_tick("14:26") + _tick("14:49"))
    g(rout, "add", "-A"); g(rout, "commit", "-qm", "14:49"); g(rout, "push", "-q")
    with open(eng / "logs" / "ticks-2026-10-14.csv", "a") as fh:
        fh.write(_tick("14:54") + _tick("14:55"))
    assert gitsync.sync(eng)
    times = [r.split(",")[0] for r in _origin_file(origin, "logs/ticks-2026-10-14.csv").splitlines()[1:]]
    assert times == ["14:25", "14:26", "14:49", "14:54", "14:55"]
    _no_markers(eng)


def test_engine_start_pulls_main_before_loading_state(repos):
    origin, eng, rout = repos
    pf = Portfolio.load(rout / "state" / "portfolio.json")
    pf.liquidbees = 42424.0
    pf.save(rout / "state" / "portfolio.json")
    (rout / "logs" / "ticks-2026-10-14.csv").write_text(HEAD_T + _tick("10:17"))
    g(rout, "add", "-A"); g(rout, "commit", "-qm", "morning"); g(rout, "push", "-q")
    assert gitsync.pull_latest(eng)
    assert Portfolio.load(eng / "state" / "portfolio.json").liquidbees == 42424.0
    assert (eng / "logs" / "ticks-2026-10-14.csv").exists()


def test_pull_latest_never_raises(tmp_path):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 90)
    assert gitsync.pull_latest(tmp_path, runner=boom) is False


def test_append_logs_use_the_union_driver():
    attrs = (ROOT / ".gitattributes").read_text()
    for pat in ("logs/ticks-*.csv", "logs/decisions-*.jsonl", "logs/health-*.jsonl"):
        assert f"{pat} merge=simgold-append" in attrs
    for wf in ("market-hours", "watchdog", "reminders", "incident"):
        assert "merge.simgold-append.driver" in (ROOT / ".github" / "workflows" / f"{wf}.yml").read_text()
