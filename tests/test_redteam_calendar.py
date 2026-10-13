"""Red-team regression tests for the NSE calendar: holidays, weekends, Muhurat, month ends, the year boundary,
and an early close / dead feed at 15:15."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from simgold import config, guard, market
from simgold.broker import Portfolio, Position
from simgold.market import IST

P = config.load_params()


@pytest.mark.parametrize("month,window", [
    (3, ["2026-03-25", "2026-03-27", "2026-03-30"]),     # 26th and 31st are holidays: the window skips them
    (5, ["2026-05-26", "2026-05-27", "2026-05-29"]),     # 28th holiday
    (6, ["2026-06-25", "2026-06-29", "2026-06-30"]),     # 26th holiday, then a weekend
    (10, ["2026-10-28", "2026-10-29", "2026-10-30"]),    # the 31st is a Saturday
    (11, ["2026-11-26", "2026-11-27", "2026-11-30"]),    # 24th holiday; month ends on a Monday
    (12, ["2026-12-29", "2026-12-30", "2026-12-31"]),    # Christmas Friday holiday before it
])
def test_last_three_trading_days_skip_weekends_and_holidays(month, window):
    days = market.trading_days_in_month(2026, month)
    got = [d.isoformat() for d in days if guard.in_month_end_window(d)]
    assert got == window
    assert market.is_last_trading_day_of_month(date.fromisoformat(window[-1]))
    for d in got:      # no new buys there, ever
        dd = date.fromisoformat(d)
        assert guard.week_factor(market.trading_day_of_month(dd), market.trading_days_left_in_month(dd), P) == 0.0
    before = market.prev_trading_day(date.fromisoformat(window[0]))
    assert guard.week_factor(market.trading_day_of_month(before), market.trading_days_left_in_month(before), P) == 1.0


def test_every_month_of_2026_has_exactly_three_protected_days():
    for m in range(1, 13):
        days = market.trading_days_in_month(2026, m)
        assert sum(guard.in_month_end_window(d) for d in days) == 3
        assert all(d.weekday() < 5 and d not in market.NSE_HOLIDAYS for d in days)


def test_weekends_holidays_and_muhurat_are_not_sessions():
    assert not market.is_trading_day(date(2026, 10, 17))        # Saturday
    assert not market.is_trading_day(date(2026, 10, 2))         # Gandhi Jayanti
    assert not market.is_trading_day(date(2026, 11, 8))         # Sunday Muhurat evening session: not modelled
    assert not market.is_market_open(datetime(2026, 11, 8, 18, 15, tzinfo=IST))


def test_gap_multiplier_before_holidays_and_weekends():
    assert market.overnight_gap_kind(date(2026, 11, 9)) == "holiday"        # NSE shut, world open: 1.5x
    assert market.overnight_gap_kind(date(2026, 12, 24)) == "weekend"       # Christmas: world shut too, then a weekend
    assert market.overnight_gap_kind(date(2026, 10, 16)) == "weekend"
    assert guard.gap_multiplier(date(2026, 11, 9), 1.0, P) == 1.5


def test_year_boundary_rolls_into_2027():
    assert market.next_trading_day(date(2026, 12, 31)) == date(2027, 1, 1)
    assert guard.in_month_end_window(date(2026, 12, 31)) and market.is_last_trading_day_of_month(date(2026, 12, 31))
    assert market.trading_day_of_month(date(2027, 1, 1)) == 1


@pytest.mark.xfail(strict=True, reason="OPEN: NSE 2027 holiday list not in simgold/market.py yet (owner to add from the "
                                       "official circular); the healthcheck warns from 1 Dec 2026")
def test_2027_republic_day_is_a_holiday():
    assert not market.is_trading_day(date(2027, 1, 26))


def test_missing_2027_list_is_detectable():
    assert market.holidays_known(2026)
    assert market.holidays_known(2027) is any(d.year == 2027 for d in market.NSE_HOLIDAYS)


# ------------------------------------------------------------------ early close / dead feed at 15:15
def _live(tmp_path, monkeypatch, now, bars):
    import run_live
    for name in ("STATE", "LOGS"):
        monkeypatch.setattr(run_live, name, tmp_path / name.lower())
    monkeypatch.setattr(run_live, "ROOT", tmp_path)
    monkeypatch.setattr(run_live, "PF_PATH", tmp_path / "state" / "portfolio.json")
    monkeypatch.setattr(run_live, "PF_BACKUP", tmp_path / "state" / "portfolio.backup.json")
    monkeypatch.setattr(run_live, "NEWS_PATH", tmp_path / "state" / "news.json")
    monkeypatch.setattr(run_live, "intraday_bars", lambda tickers: bars)
    monkeypatch.setattr(run_live, "now_ist", lambda: now)
    live = run_live.Live(force=False, tick_seconds=60)
    monkeypatch.setattr(live.news, "refresh", lambda **k: 0)
    return live


def _overweight(pf: Portfolio):
    """A silver position far bigger than tonight's risk budget allows (cushion ~300)."""
    pf.cash, pf.liquidbees, pf.floor, pf.best_profit = 0.0, 70300.0, 100000.0, 600.0
    pf.last_accrual = "2026-10-16"
    pf.positions["SILVERBEES"] = Position(qty=150, avg_cost=200.0, high_water=200.0, opened="2026-10-16", stop=196.0,
                                          last_price=200.0)


@pytest.mark.parametrize("hh,mm,runs", [(11, 0, False), (15, 21, True)])
def test_dead_feed_after_1515_still_runs_the_overnight_check(tmp_path, monkeypatch, hh, mm, runs):
    now = datetime(2026, 10, 16, hh, mm, tzinfo=IST)            # a Friday: weekend gap multiplier tonight
    live = _live(tmp_path, monkeypatch, now, {})
    _overweight(live.pf)
    live.tick()
    assert ("SILVERBEES" not in live.pf.positions) is runs
    if runs:
        assert (tmp_path / "state" / "portfolio.json").exists()


def test_stale_feed_after_an_early_close_runs_protective_exits_without_buying(tmp_path, monkeypatch):
    import pandas as pd
    now = datetime(2026, 10, 16, 15, 21, tzinfo=IST)
    t0 = datetime(2026, 10, 16, 13, 0, tzinfo=IST)               # the session ended at 13:00
    idx = pd.DatetimeIndex([t0 - timedelta(minutes=k) for k in range(5)][::-1])
    bar = pd.DataFrame({"open": 200.0, "high": 200.0, "low": 200.0, "close": 200.0, "volume": 1000}, index=idx)
    live = _live(tmp_path, monkeypatch, now, {"SILVERBEES.NS": bar})
    _overweight(live.pf)
    live.tick()
    assert "SILVERBEES" not in live.pf.positions
    assert not any(t["side"] == "BUY" for t in live.pf.trades)
