"""simgold.livepush against a real local bare repo, and the engine's live wiring (run_live.Live)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from simgold import gitsync, livepush
from simgold.market import IST

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
ENV = {"SIMGOLD_GIT_SYNC": "1", "SIMGOLD_LIVE_PUSH": "1"}
NOW = datetime(2026, 10, 22, 11, 30, tzinfo=IST)


def g(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r


class Rec:
    """Records every git command and runs it for real."""
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd, **kw):
        self.cmds.append(list(cmd))
        return subprocess.run(cmd, **kw)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


@pytest.fixture()
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    eng = tmp_path / "engine"
    g(tmp_path, "clone", "-q", str(origin), str(eng))
    for k, v in (("user.email", "e@example.invalid"), ("user.name", "e"), ("commit.gpgsign", "false")):
        g(eng, "config", k, v)
    (eng / "state").mkdir()
    (eng / "state" / "portfolio.json").write_text("{}\n")
    g(eng, "add", "-A"); g(eng, "commit", "-qm", "seed"); g(eng, "push", "-q", "origin", "HEAD:main")
    return origin, eng


def pub(eng, rec=None, clock=None, env=ENV):
    return livepush.LivePublisher(eng, runner=rec or subprocess.run, environ=env, clock=clock or Clock(), min_interval=0)


def snapshot(eng):
    return (g(eng, "rev-parse", "HEAD").stdout, g(eng, "rev-parse", "main").stdout, g(eng, "status", "--porcelain").stdout,
            g(eng, "ls-files", "-s").stdout)


def test_first_publish_creates_live_then_fast_forwards_and_leaves_main_alone(repos):
    origin, eng = repos
    before = snapshot(eng)
    main_before = g(origin, "rev-parse", "main").stdout
    rec = Rec()
    p = pub(eng, rec)
    p.start()
    assert p.parent is None
    assert p.publish(b'{"v":1,"n":1}\n', NOW) is True
    first = g(origin, "rev-parse", "live").stdout.strip()
    assert g(origin, "rev-list", "--count", "live").stdout.strip() == "1"
    assert p.publish(b'{"v":1,"n":2}\n', NOW) is True
    assert g(origin, "rev-parse", "live^").stdout.strip() == first                      # fast-forward: the first is the parent
    assert g(origin, "show", "live:now.json").stdout == '{"v":1,"n":2}\n'
    assert snapshot(eng) == before and g(origin, "rev-parse", "main").stdout == main_before
    assert not (eng / "now.json").exists()
    for c in rec.cmds:
        assert "--force" not in c and "-f" not in c and not any(a.startswith("+") for a in c), c
        assert c[1] in {"fetch", "rev-parse", "hash-object", "mktree", "commit-tree", "push"}


def test_second_run_continues_from_the_remote_tip(repos):
    origin, eng = repos
    pub(eng).publish(b"a\n", NOW)
    p2 = pub(eng)                                   # a new engine process
    p2.start()
    assert p2.parent == g(origin, "rev-parse", "live").stdout.strip()
    assert p2.publish(b"b\n", NOW)
    assert g(origin, "rev-list", "--count", "live").stdout.strip() == "2"


def test_rejected_push_refetches_and_retries_without_force(repos, tmp_path):
    origin, eng = repos
    rec = Rec()
    p = pub(eng, rec)
    assert p.publish(b"one\n", NOW)
    other = tmp_path / "other"                      # someone else moves `live`
    g(tmp_path, "clone", "-q", "-b", "live", str(origin), str(other))
    g(other, "config", "user.email", "o@example.invalid"); g(other, "config", "user.name", "o")
    (other / "now.json").write_text("moved\n")
    g(other, "commit", "-qam", "moved"); g(other, "push", "-q", "origin", "live")
    moved = g(origin, "rev-parse", "live").stdout.strip()
    assert p.publish(b"two\n", NOW) is True
    assert g(origin, "rev-parse", "live^").stdout.strip() == moved
    assert g(origin, "show", "live:now.json").stdout == "two\n"
    assert not any("--force" in c or any(a.startswith("+") for a in c) for c in rec.cmds)


def test_failures_never_raise_and_back_off(tmp_path):
    clock = Clock()
    calls = []

    def boom(cmd, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 15)
    p = livepush.LivePublisher(tmp_path, runner=boom, environ=ENV, clock=clock, min_interval=0)
    p.start()                                       # never raises
    for _ in range(3):
        assert p.publish(b"x", NOW) is False
        clock.t += 60
    n = len(calls)
    assert p.publish(b"x", NOW) is False and len(calls) == n          # backed off: no git at all
    clock.t += 301
    p.publish(b"x", NOW)
    assert len(calls) > n


def test_kill_switch_and_missing_git_are_quiet(tmp_path):
    rec = Rec()
    for env in ({}, {"SIMGOLD_LIVE_PUSH": "1"}, {"SIMGOLD_GIT_SYNC": "1"}, {"SIMGOLD_GIT_SYNC": "1", "SIMGOLD_LIVE_PUSH": "0"}):
        p = livepush.LivePublisher(tmp_path, runner=rec, environ=env)
        p.start()
        assert p.publish(b"x", NOW) is False
    assert rec.cmds == []


def test_unreachable_remote_returns_false(tmp_path):
    d = tmp_path / "r"; d.mkdir()
    g(d, "init", "-q"); g(d, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    assert pub(d).publish(b"x", NOW) is False


def test_min_interval_throttles(repos):
    _, eng = repos
    clock = Clock()
    p = livepush.LivePublisher(eng, environ=ENV, clock=clock, min_interval=50)
    assert p.publish(b"a\n", NOW)
    clock.t += 10
    assert p.publish(b"b\n", NOW) is False
    clock.t += 60
    assert p.publish(b"c\n", NOW) is True


def test_live_now_on_main_conflict_engine_version_wins(repos, tmp_path):
    origin, eng = repos
    (eng / "state" / "live_now.json").write_text('{"v":1,"who":"base"}\n')
    g(eng, "add", "-A"); g(eng, "commit", "-qm", "base"); g(eng, "push", "-q", "origin", "HEAD:main")
    other = tmp_path / "o2"
    g(tmp_path, "clone", "-q", str(origin), str(other))
    g(other, "config", "user.email", "o@example.invalid"); g(other, "config", "user.name", "o")
    (other / "state" / "live_now.json").write_text('{"v":1,"who":"other"}\n')
    g(other, "commit", "-qam", "other"); g(other, "push", "-q", "origin", "HEAD:main")
    (eng / "state" / "live_now.json").write_text('{"v":1,"who":"engine"}\n')
    assert gitsync.sync(eng)
    assert json.loads(g(origin, "show", "main:state/live_now.json").stdout)["who"] == "engine"


# ---------------------------------------------------------------- engine wiring
class FakeIdx:
    def __init__(self, ts): self.ts = ts
    def __getitem__(self, i): return self
    def to_pydatetime(self): return self.ts


class FakeBars:
    def __init__(self, ts): self.index = FakeIdx(ts)


def make_live(tmp_path, monkeypatch, pubber):
    import run_live
    from simgold import config
    from simgold.broker import Portfolio
    from simgold.engine import Market
    st, lg = tmp_path / "state", tmp_path / "logs"
    st.mkdir(); lg.mkdir()
    monkeypatch.setattr(run_live, "STATE", st); monkeypatch.setattr(run_live, "LOGS", lg)
    monkeypatch.setattr(run_live, "PF_PATH", st / "portfolio.json"); monkeypatch.setattr(run_live, "PF_BACKUP", st / "pb.json")
    monkeypatch.setattr(run_live, "LIVE_PATH", st / "live_now.json"); monkeypatch.setattr(run_live, "EQUITY_PATH", st / "equity.csv")
    monkeypatch.setattr(run_live, "TRADES_ARCHIVE", lg / "ta.jsonl")
    ts = datetime.now(IST)
    monkeypatch.setattr(run_live, "now_ist", lambda: ts)
    monkeypatch.setattr(run_live, "intraday_bars", lambda t: {"X": FakeBars(ts)})
    monkeypatch.setattr(run_live, "build_market", lambda *a, **k: Market(ts, {"GOLDBEES": 100.0}, {"GOLDBEES": ts}, {}, prev_close={"GOLDBEES": 99.0}))
    monkeypatch.setattr(run_live, "run_tick", lambda pf, mkt, env: [])
    live = run_live.Live.__new__(run_live.Live)
    live.force, live.p, live.pf = True, config.load_params(), Portfolio(cash=0.0, liquidbees=1000.0)
    live.tickers, live.last_tick_day, live.env = ["X"], None, None
    live.hist, live.hist_at, live.hist_fail = object(), 1e18, 0

    class Mon:
        def mark_fresh(self): pass
    class News:
        last_new = []
        def refresh(self, **k): pass
    live.mon, live.news = Mon(), News()
    live.livepub, live.series, live.live_prev, live.live_ts = pubber, [], {}, {}
    return run_live, live, st


def test_tick_survives_a_publisher_that_raises(tmp_path, monkeypatch):
    class Bad:
        def publish(self, *a, **k): raise RuntimeError("boom")
    run_live, live, st = make_live(tmp_path, monkeypatch, Bad())
    live.tick()
    assert (st / "portfolio.json").exists() and live.last_tick_day is not None
    assert json.loads((st / "live_now.json").read_text())["v"] == 1


def test_tick_survives_a_builder_that_raises(tmp_path, monkeypatch):
    run_live, live, st = make_live(tmp_path, monkeypatch, livepush.LivePublisher(tmp_path, environ={}))
    monkeypatch.setattr(run_live.livepnl, "build", lambda *a, **k: 1 / 0)
    live.tick()
    assert (st / "portfolio.json").exists()


def test_publish_is_skipped_when_the_tick_used_the_budget(tmp_path, monkeypatch):
    seen = []

    class Rec2:
        def publish(self, payload, now): seen.append(payload); return True
    run_live, live, st = make_live(tmp_path, monkeypatch, Rec2())
    live._publish_live(datetime.now(IST), None, True, t0=0.0)           # 1970: far over budget
    assert seen == [] and (st / "live_now.json").exists()               # the local file is still written
    live._publish_live(datetime.now(IST), None, True, t0=__import__("time").time())
    assert len(seen) == 1


def test_publish_runs_after_the_portfolio_is_saved(tmp_path, monkeypatch):
    order = []

    class Rec3:
        def publish(self, payload, now): order.append("publish")
    run_live, live, st = make_live(tmp_path, monkeypatch, Rec3())
    orig = live._save_pf
    live._save_pf = lambda: (orig(), order.append("save"))
    live.tick()
    assert order == ["save", "publish"]
