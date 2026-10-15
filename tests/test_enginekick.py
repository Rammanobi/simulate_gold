"""simgold.enginekick: when the push-triggered engine-kick workflow may dispatch market-hours.yml."""
from __future__ import annotations

from datetime import datetime

import pytest

from simgold import enginekick
from simgold.market import IST


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


def test_premarket_push_with_no_run_dispatches():
    # 15 Oct 2026: premarket pushed at 08:53 IST, its dispatch was denied, no market-hours run existed until 10:17
    go, why = enginekick.decide(at(2026, 10, 15, 8, 53), "")
    assert go and "no market-hours run" in why


@pytest.mark.parametrize("active", ["in_progress", "queued", "pending", "in_progress,pending", "waiting", " requested "])
def test_any_unfinished_run_means_skip(active):
    go, why = enginekick.decide(at(2026, 10, 15, 8, 53), active)
    assert not go and "already active" in why


@pytest.mark.parametrize("when", [at(2026, 10, 17, 8, 53),      # Saturday
                                  at(2026, 10, 20, 8, 53)])     # NSE holiday
def test_no_dispatch_on_closed_days(when):
    go, why = enginekick.decide(when, "")
    assert not go and "not an NSE trading day" in why


@pytest.mark.parametrize("hm,expected", [((8, 29), False), ((8, 30), True), ((12, 16), True),
                                         ((15, 19), True), ((15, 20), False), ((15, 50), False), ((2, 0), False)])
def test_window(hm, expected):
    assert enginekick.decide(at(2026, 10, 15, *hm), "")[0] is expected


def test_utc_input_is_read_as_ist():
    from datetime import timezone
    assert enginekick.decide(datetime(2026, 10, 15, 3, 23, tzinfo=timezone.utc), "")[0]      # 08:53 IST


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(enginekick, "now_ist", lambda: at(2026, 10, 15, 8, 53))
    assert enginekick.main(["--active", ""]) == 0
    assert capsys.readouterr().out.startswith("DISPATCH: ")
    assert enginekick.main(["--active", "in_progress"]) == 3
    assert capsys.readouterr().out.startswith("SKIP: ")
