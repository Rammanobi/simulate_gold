"""The shadow twins must not change a single engine decision. The stored minute ticks of 13-20 Nov 2026 (logs/ticks-DATE.csv; 21 Nov was a
Saturday) are replayed through the real tick with `twins.enabled` false and true (with a working NIFTYBEES feed, with a feed that raises,
and with no feed), and everything the engine writes is compared byte for byte: decisions, ticks, checks, the portfolio, the runtime
state, and the params themselves. Only the twins' own two files may differ."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

import v3_replay as R
from simgold import twins

DAYS = ["2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]


def feed_ok(ticker):
    assert ticker == "NIFTYBEES.NS"
    return 271.5


def feed_boom(ticker):
    raise RuntimeError("yahoo down")


def params(enabled: bool, mode: str | None = None):
    p = R.shipped_params(mode)
    p["twins"]["enabled"] = enabled
    return p


def engine_files(tmp: Path) -> dict[str, bytes]:
    """Every file the engine wrote, except the twins' own."""
    out = {}
    for sub in ("state", "logs"):
        for f in sorted((tmp / sub).rglob("*")):
            if f.is_file() and not f.name.startswith("twins"):
                out[f"{sub}/{f.name}"] = f.read_bytes()
    return out


def portfolio_json(pf) -> str:
    return json.dumps(asdict(pf), sort_keys=True, default=str)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("off")
    pf, env = R.replay(tmp, params(False), days=DAYS)
    return tmp, pf, env


@pytest.mark.parametrize("label,env_kw", [
    ("feed works", {"twin_feed": feed_ok}),
    ("feed raises", {"twin_feed": feed_boom}),
    ("no feed", {}),
])
def test_decisions_and_state_are_byte_identical_with_twins_on(tmp_path, baseline, label, env_kw):
    tmp0, pf0, env0 = baseline
    p_on = params(True)
    p_snapshot = copy.deepcopy(p_on)
    pf1, env1 = R.replay(tmp_path, p_on, days=DAYS, env_kw=env_kw)
    assert R.decisions_text(env1, DAYS) == R.decisions_text(env0, DAYS), label
    assert '"BUY"' in R.decisions_text(env0, DAYS)                                    # the comparison is about real trades
    assert R.runtime_text(pf1) == R.runtime_text(pf0), label
    assert portfolio_json(pf1) == portfolio_json(pf0), label
    assert engine_files(tmp_path) == engine_files(tmp0), label                       # decisions, ticks, checks, last_prices ... all bytes
    assert p_on == p_snapshot                                                         # the hook did not edit the params
    # ... while the twins really ran: one record per checked day
    st = json.loads((tmp_path / "state" / "twins.json").read_text())
    assert st["days"] == len(DAYS) and st["last_day"] == DAYS[-1] and set(st["books"]) >= {"T0", "T1", "T3", "T4", "T5"}
    assert len(twins.read_rows(tmp_path / "logs")) == len(twins.read_rows(tmp_path / "logs", last_days=99)) >= 5 * len(DAYS)
    # T0 mirrors the real book on the last day
    t0 = st["books"]["T0"]
    assert t0["units"] == {s: p.qty for s, p in pf1.positions.items()} and t0["cash"] == pytest.approx(pf1.cash + pf1.liquidbees, abs=0.01)
    if env_kw.get("twin_feed") is feed_ok:
        assert set(st["books"]) == set(twins.TWINS) and st["nifty"]["status"] == "ok"
    else:
        assert "T2" not in st["books"] and st["nifty"]["status"] == "pending"


def test_with_twins_off_nothing_is_written_and_no_feed_is_called(tmp_path, baseline):
    tmp0, _pf, _env = baseline
    calls = []
    R.replay(tmp_path, params(False), days=DAYS[:2], env_kw={"twin_feed": lambda t: calls.append(t)})
    assert calls == [] and not (tmp_path / "state" / "twins.json").exists() and not list((tmp_path / "logs").glob("twins-*"))
    assert not (tmp0 / "state" / "twins.json").exists()


def test_the_feed_is_asked_once_a_day_at_most(tmp_path):
    calls = []
    R.replay(tmp_path, params(True), days=DAYS[:3], env_kw={"twin_feed": lambda t: calls.append(t) or 271.5})
    assert len(calls) == 3


@pytest.mark.parametrize("mode", ["v3_alloc"])
def test_identical_under_the_v3_shadow_mode_too(tmp_path, mode):
    pf0, env0 = R.replay(tmp_path / "off", params(False, mode), days=DAYS[:3])
    pf1, env1 = R.replay(tmp_path / "on", params(True, mode), days=DAYS[:3], env_kw={"twin_feed": feed_ok})
    assert R.decisions_text(env1, DAYS[:3]) == R.decisions_text(env0, DAYS[:3])
    assert R.runtime_text(pf1) == R.runtime_text(pf0) and portfolio_json(pf1) == portfolio_json(pf0)
    assert (tmp_path / "on" / "state" / "twins.json").exists()


def test_a_twins_failure_cannot_reach_the_engine(tmp_path, baseline, monkeypatch):
    tmp0, pf0, env0 = baseline

    def boom(**kw):
        raise RuntimeError("twins bug")
    monkeypatch.setattr(twins, "daily_hook", boom)
    pf1, env1 = R.replay(tmp_path, params(True), days=DAYS, env_kw={"twin_feed": feed_ok})
    assert R.decisions_text(env1, DAYS) == R.decisions_text(env0, DAYS) and portfolio_json(pf1) == portfolio_json(pf0)


def test_an_unwritable_twins_file_cannot_reach_the_engine(tmp_path, baseline):
    tmp0, pf0, env0 = baseline
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "twins.json").mkdir()                                       # a directory where the file should be: every write fails
    pf1, env1 = R.replay(tmp_path, params(True), days=DAYS, env_kw={"twin_feed": feed_ok})
    assert R.decisions_text(env1, DAYS) == R.decisions_text(env0, DAYS) and portfolio_json(pf1) == portfolio_json(pf0)
