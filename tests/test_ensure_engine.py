"""tools/ensure_engine.py via simgold.relay.ensure: idempotent restart source."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from simgold import relay
from tests.test_relay import FakeApi, at, run_

ROOT = Path(__file__).resolve().parent.parent


def go(api, now, dry=False):
    out: list[str] = []
    res = relay.ensure(api, "o/r", now, dry, out.append)
    return res, out


def test_nothing_to_do_when_a_relay_or_engine_is_active():
    for runs in ({"engine-relay.yml": [run_("queued")]}, {"market-hours.yml": [run_("in_progress")]},
                 {"engine-relay.yml": [run_("in_progress")], "market-hours.yml": [run_("queued")]}):
        api = FakeApi(runs)
        assert go(api, at(2026, 11, 13, 11, 0))[0] == "ok" and api.posts == []


def test_trading_day_session_hours_dispatch_the_engine():
    api = FakeApi({"engine-relay.yml": [run_("completed")], "market-hours.yml": [run_("completed")]})
    assert go(api, at(2026, 11, 13, 11, 0))[0] == "engine"
    assert api.posts == [("market-hours.yml", {"ref": "main", "inputs": {"session": "auto"}})]


def test_before_start_and_after_close_dispatch_the_relay():
    for now in (at(2026, 11, 13, 7, 0), at(2026, 11, 13, 15, 25), at(2026, 11, 13, 20, 0)):
        api = FakeApi()
        assert go(api, now)[0] == "relay" and api.dispatched() == ["engine-relay.yml"]


def test_weekend_and_holiday_dispatch_the_relay_not_the_engine():
    for now in (at(2026, 11, 14, 11, 0), at(2026, 11, 10, 11, 0), at(2026, 12, 25, 11, 0)):
        api = FakeApi()
        assert go(api, now)[0] == "relay" and api.dispatched() == ["engine-relay.yml"]


def test_dry_run_dispatches_nothing_and_idempotent():
    api = FakeApi()
    res, out = go(api, at(2026, 11, 13, 11, 0), dry=True)
    assert res == "engine" and api.posts == [] and "dry-run" in out[0]
    api2 = FakeApi({"market-hours.yml": [run_("queued")]})        # the second call sees the first one's dispatch
    assert go(api2, at(2026, 11, 13, 11, 0))[0] == "ok" and api2.posts == []


def test_list_failure_skips_and_dispatch_failure_does_not_raise():
    assert go(FakeApi(fail_list=True), at(2026, 11, 13, 11, 0))[0] == "skip"
    assert go(FakeApi(fail_post=True), at(2026, 11, 13, 11, 0))[0] == "skip"


def test_cli_always_exits_zero(monkeypatch):
    spec = importlib.util.spec_from_file_location("ensure_cli", ROOT / "tools" / "ensure_engine.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["ensure_cli"] = m
    spec.loader.exec_module(m)
    monkeypatch.setattr(relay, "ensure", lambda *a, **k: "skip")
    assert m.main(["--dry-run"]) == 0
