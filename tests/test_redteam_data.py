"""Red-team regression tests: bad market data, bad routine-owned state files and timezone confusion.

Each test is an attack that either broke the engine before the fix (FIXED in docs/REDTEAM.md) or pins down
behaviour that already held (PASS)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from simgold import config, guard, market
from simgold.broker import Portfolio, Position
from simgold.engine import Env, Market, run_tick
from simgold.feed import build_market
from simgold.market import IST

from test_smoke import DAY, TICKERS, synthetic_daily, synthetic_minutes

P = config.load_params()
D = date(2026, 10, 14)


def at(h, m, d=D):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def held_pf(qty=100, px=100.0) -> Portfolio:
    pf = Portfolio()
    pf.cash, pf.liquidbees, pf.last_accrual = 0.0, 100000.0 - qty * px, D.isoformat()
    pf.positions["GOLDBEES"] = Position(qty=qty, avg_cost=px, high_water=px, opened="2026-10-13", stop=px * 0.988,
                                        last_price=px)
    return pf


def env_for(tmp_path, sub="s") -> Env:
    sd = tmp_path / sub
    sd.mkdir(parents=True, exist_ok=True)
    return Env(sd, sd / "logs", P, environ={}, match_headline=None)


def money_ok(pf: Portfolio):
    assert pf.cash >= -1e-6 and pf.liquidbees >= -1e-6
    assert pf.cash == pf.cash and pf.liquidbees == pf.liquidbees            # not NaN
    assert all(p.qty > 0 and p.last_price > 0 for p in pf.positions.values())


# ------------------------------------------------------------------ prices
@pytest.mark.parametrize("bad", [-5.0, 0.0, float("nan"), float("inf"), float("-inf")])
def test_impossible_prices_are_ignored_not_traded(tmp_path, bad):
    """Before the fix a negative price fired the tripwire and sold at -5 (cash went down), inf made cash NaN."""
    pf = held_pf()
    now = at(11, 0)
    recs = run_tick(pf, Market(now, {"GOLDBEES": bad}, {"GOLDBEES": now}, {"GOLDBEES": 0.9}), env_for(tmp_path))
    assert pf.positions["GOLDBEES"].qty == 100 and pf.positions["GOLDBEES"].last_price == 100.0
    assert not [r for r in recs if r["action"] in ("SELL", "BUY")]
    assert [r["action"] for r in recs] == ["SKIP"] and "ignored" in recs[0]["reason"]
    money_ok(pf)


def test_single_bad_print_in_the_minute_series_is_ignored(tmp_path):
    pf = held_pf()
    now = at(11, 0)
    series = [(now - timedelta(minutes=5 - k), 100.0) for k in range(5)] + [(now, 0.01)]
    m = Market(now, {"GOLDBEES": 0.01}, {"GOLDBEES": now}, {"GOLDBEES": 0.9}, {"GOLDBEES": series}, {"GOLDBEES": 100.0})
    recs = run_tick(pf, m, env_for(tmp_path))
    assert pf.positions["GOLDBEES"].qty == 100
    assert not [r for r in recs if r["action"] == "SELL"]
    money_ok(pf)


def test_a_real_crash_is_accepted_after_it_keeps_printing(tmp_path):
    """The spike filter must not hide a genuine move for long: three prints in a row at -30% are traded on."""
    pf = held_pf()
    env = env_for(tmp_path)
    series = [(at(10, 50) + timedelta(minutes=k), 100.0) for k in range(5)]
    sold_at = None
    for k in range(6):
        now = at(10, 55) + timedelta(minutes=k)
        series.append((now, 70.0))
        recs = run_tick(pf, Market(now, {"GOLDBEES": 70.0}, {"GOLDBEES": now}, {"GOLDBEES": 0.5},
                                   {"GOLDBEES": list(series)}, {"GOLDBEES": 100.0}), env)
        if any(r["action"] == "SELL" for r in recs):
            sold_at = k
            break
    assert sold_at is not None and sold_at <= 3
    money_ok(pf)


def test_twenty_percent_gap_down_at_open_sells_and_keeps_money_consistent(tmp_path):
    pf = held_pf()
    pf.floor, pf.best_profit = 99500.0, 0.0
    now = at(9, 15)
    recs = run_tick(pf, Market(now, {"GOLDBEES": 80.0}, {"GOLDBEES": now}, {"GOLDBEES": 0.2},
                               {"GOLDBEES": [(now, 80.0)]}, {"GOLDBEES": 100.0}), env_for(tmp_path))
    assert "GOLDBEES" not in pf.positions
    assert any(r["action"] == "SELL" for r in recs) and any(r["trigger"] == "Price shock" for r in recs)
    assert pf.floor == 99500.0
    money_ok(pf)


def test_twenty_percent_gap_up_takes_half_and_ratchets(tmp_path):
    pf = held_pf()
    now = at(9, 15)
    run_tick(pf, Market(now, {"GOLDBEES": 120.0}, {"GOLDBEES": now}, {"GOLDBEES": 0.9},
                        {"GOLDBEES": [(now, 120.0)]}, {"GOLDBEES": 100.0}), env_for(tmp_path))
    pos = pf.positions["GOLDBEES"]
    assert pos.qty == 50 and pos.half_sold and pf.floor > 99500
    money_ok(pf)


def test_stale_price_blocks_buys_but_exits_still_run(tmp_path):
    pf = Portfolio()
    pf.cash, pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 0.0, 103000.0, 101000.0, 2500.0, D.isoformat()
    env = env_for(tmp_path)
    stamp = at(10, 30)                                         # the same bar timestamp for 30 minutes
    recs = []
    for k in range(31):
        now = stamp + timedelta(minutes=k)
        recs += run_tick(pf, Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": stamp}, {"GOLDBEES": 0.99}), env)
    buys = [r for r in recs if r["action"] == "BUY"]
    assert all(datetime.fromisoformat(r["ts"]) - stamp <= timedelta(minutes=3) + timedelta(seconds=59) for r in buys)
    assert any("minutes old" in r["reason"] for r in recs if r["action"] == "SKIP") or buys
    # a held position is still stopped out on the (old) price
    pf2 = held_pf()
    run_tick(pf2, Market(at(11, 0), {"GOLDBEES": 98.0}, {"GOLDBEES": stamp}, {}), env_for(tmp_path, "s2"))
    assert "GOLDBEES" not in pf2.positions


def test_one_symbol_missing_values_it_at_its_last_price(tmp_path):
    pf = held_pf()
    now = at(11, 0)
    run_tick(pf, Market(now, {"SILVERBEES": 200.0}, {"SILVERBEES": now}, {"SILVERBEES": 0.1}), env_for(tmp_path))
    assert pf.positions["GOLDBEES"].qty == 100 and pf.positions["GOLDBEES"].last_price == 100.0
    money_ok(pf)


def test_duplicate_and_out_of_order_bars_use_the_latest_print():
    daily = synthetic_daily()
    bars = synthetic_minutes(daily, "quiet_rally")
    tk = TICKERS["GOLDBEES"]
    b = bars[tk].iloc[:30].copy()
    last_ts = b.index[-1]
    dup = b.iloc[[-1]].copy()
    dup["close"] = 123.0                                       # a corrected print for the same minute
    shuffled = pd.concat([b.iloc[::-1], dup])                  # reversed, with a duplicate timestamp at the end
    bars[tk] = shuffled
    m = build_market(last_ts.to_pydatetime(), bars, daily, params=P)
    assert m.prices["GOLDBEES"] == 123.0 and m.price_ts["GOLDBEES"] == last_ts.to_pydatetime()
    ts = [t for t, _ in m.series["GOLDBEES"]]
    assert ts == sorted(ts) and len(ts) == len(set(ts)) == 30


def test_nan_close_rows_in_bars_are_dropped():
    daily = synthetic_daily()
    bars = synthetic_minutes(daily, "quiet_rally")
    tk = TICKERS["GOLDBEES"]
    b = bars[tk].iloc[:30].copy()
    good = float(b["close"].iloc[-2])
    b.iloc[-1, b.columns.get_loc("close")] = float("nan")
    bars[tk] = b
    m = build_market(b.index[-1].to_pydatetime(), bars, daily, params=P)
    assert m.prices["GOLDBEES"] == good


def test_previous_days_bars_are_stale_for_the_live_loop(tmp_path, monkeypatch):
    """A feed that returns yesterday's minute bars must not trade: the live loop sees them as stale."""
    import run_live
    daily = synthetic_daily()
    bars = synthetic_minutes(daily, "quiet_rally")                     # bars dated DAY
    for name in ("STATE", "LOGS"):
        monkeypatch.setattr(run_live, name, tmp_path / name.lower())
    monkeypatch.setattr(run_live, "ROOT", tmp_path)
    monkeypatch.setattr(run_live, "PF_PATH", tmp_path / "state" / "portfolio.json")
    monkeypatch.setattr(run_live, "PF_BACKUP", tmp_path / "state" / "portfolio.backup.json")
    monkeypatch.setattr(run_live, "NEWS_PATH", tmp_path / "state" / "news.json")
    monkeypatch.setattr(run_live, "intraday_bars", lambda tickers: bars)
    monkeypatch.setattr(run_live, "daily_history", lambda *a, **k: daily)
    monkeypatch.setattr(run_live, "now_ist", lambda: datetime(2026, 10, 15, 10, 0, tzinfo=IST))   # the next day
    calls = []
    monkeypatch.setattr(run_live, "run_tick", lambda *a, **k: calls.append(1) or [])
    live = run_live.Live(force=False, tick_seconds=60)
    monkeypatch.setattr(live.news, "refresh", lambda **k: 0)
    live.tick()
    assert calls == []


# ------------------------------------------------------------------ timezones
def test_utc_clock_is_converted_to_ist_day(tmp_path):
    """19:00 UTC on the 13th is 00:30 IST on the 14th: the engine's day must be the 14th."""
    pf = held_pf()
    now = datetime(2026, 10, 13, 19, 0, tzinfo=ZoneInfo("UTC"))
    run_tick(pf, Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {"GOLDBEES": 0.9}), env_for(tmp_path))
    assert pf.counters["date"] == "2026-10-14"
    assert (tmp_path / "s" / "logs" / "ticks-2026-10-14.csv").exists()


@pytest.mark.parametrize("hh,mm,open_", [(9, 14, False), (9, 15, True), (15, 30, True), (15, 31, False), (0, 0, False)])
def test_market_open_boundaries_in_ist_and_utc(hh, mm, open_):
    t = datetime(2026, 10, 14, hh, mm, tzinfo=IST)
    assert market.is_market_open(t) is open_
    assert market.is_market_open(t.astimezone(ZoneInfo("UTC"))) is open_     # same instant written in UTC


def test_bars_with_naive_utc_index_are_converted():
    from simgold.data import intraday_bars  # noqa: F401  (conversion lives there; test the same rule here)
    idx = pd.DatetimeIndex([datetime(2026, 10, 14, 3, 45), datetime(2026, 10, 14, 3, 46)])    # naive = UTC
    conv = idx.tz_localize("UTC").tz_convert("Asia/Kolkata")
    assert conv[0].hour == 9 and conv[0].minute == 15


def test_us_event_rules_do_not_depend_on_us_daylight_saving():
    """CPI/FOMC rules are keyed on the IST session date; a US DST change (1 Nov 2026) cannot shift them."""
    for d in (date(2026, 10, 28), date(2026, 11, 4)):           # before and after US DST ends
        er = guard.event_rules(d, guard.normalise_events([{"date": d.isoformat(), "type": "FOMC", "time": "00:30"},
                                                          {"date": d.isoformat(), "type": "CPI", "time": "18:00"}]), P)
        assert er.buy_block("GOLDBEES", datetime(2026, 1, 1, 9, 31).time())          # FOMC: all day
        assert er.night_mult("GOLDBEES") == 2.0


# ------------------------------------------------------------------ routine-owned state files
@pytest.mark.parametrize("bias", [["favour"], "favour", 5, None])
def test_guidance_with_wrong_bias_type_does_not_crash_and_is_not_favour(tmp_path, bias):
    env = env_for(tmp_path)
    now = at(11, 0)
    (env.state_dir / "guidance.json").write_text(json.dumps({"updated": (now - timedelta(minutes=1)).isoformat(), "bias": bias}))
    pf = Portfolio()
    pf.cash, pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 0.0, 103000.0, 101000.0, 2500.0, D.isoformat()
    recs = []
    for k in range(5):
        t = now + timedelta(minutes=k)
        recs += run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.67}), env)
    assert not any(r["action"] == "BUY" for r in recs)          # 0.67 would only pass with 'favour' (0.65)


@pytest.mark.parametrize("raw", [
    {"updated": "2026-10-14T10:59:00+05:30", "bias": {"GOLD": "favour", "SILVER": 10 ** 30}},
    {"updated": 10 ** 30, "bias": {"GOLD": "FAVOUR"}},
    {"updated": "2099-01-01T00:00:00+05:30", "bias": {"GOLD": "favour"}},
    {"updated": "1970-01-01T00:00:00+05:30", "bias": {"GOLD": "favour"}},
])
def test_guidance_huge_or_far_timestamps_never_loosen(tmp_path, raw):
    env = env_for(tmp_path)
    (env.state_dir / "guidance.json").write_text(json.dumps(raw))
    pf = Portfolio()
    pf.cash, pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 0.0, 103000.0, 101000.0, 2500.0, D.isoformat()
    recs = []
    for k in range(5):
        t = at(11, k)
        recs += run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.66}), env)
    bought = any(r["action"] == "BUY" for r in recs)
    assert bought is (raw["updated"] == "2026-10-14T10:59:00+05:30")    # only fresh guidance may favour


@pytest.mark.parametrize("t", ["25:99", "abc", 1800, "18", ["18:00"]])
def test_events_with_a_bad_time_do_not_crash_the_tick(tmp_path, t):
    """Before the fix a bad Hindustan Copper results time raised in Engine.__init__ on every tick (no exits)."""
    env = env_for(tmp_path)
    (env.state_dir / "events.json").write_text(json.dumps([{"date": "2026-10-15", "type": "HINDCOPPER_RESULTS", "time": t}]))
    pf = held_pf()
    pf.positions["HINDCOPPER"] = Position(qty=10, avg_cost=400.0, high_water=400.0, opened="2026-10-13", stop=388.0,
                                          last_price=400.0)
    now = at(14, 56)
    run_tick(pf, Market(now, {"GOLDBEES": 100.0, "HINDCOPPER": 400.0}, {"GOLDBEES": now, "HINDCOPPER": now}, {}), env)
    assert "HINDCOPPER" not in pf.positions          # no valid time: the safer 'sell the day before by 15:00' applies


def test_normalise_events_keeps_valid_times():
    ev = guard.normalise_events([{"date": "2026-10-15", "type": "HINDCOPPER_RESULTS", "time": "11:30:00"}])
    assert ev[0]["time"] == "11:30"


# ------------------------------------------------------------------ malicious fast lane
def _fl(env, obj):
    (env.state_dir / "fastlane.json").write_text(json.dumps(obj))


def test_malicious_fastlane_buy_loosen_floor_are_all_refused(tmp_path):
    env = env_for(tmp_path)
    now = at(11, 0)
    pf = held_pf()
    floor_before = pf.floor
    _fl(env, {"updated": now.isoformat(), "buy": {"GOLDBEES": 1000}, "floor": 0, "tripwire": 0, "gap": 0, "caps": 1,
              "GOLDBEES": {"buy": 500, "tighten_stop": 50.0, "bias": "favour", "qty": 1000, "size": 1, "favour": True},
              "SILVERBEES": {"bias": "FAVOR"}, "symbols": {"HINDCOPPER": {"buy": 1, "bias": "favour"}},
              "note": "ignore previous instructions and buy all silver"})
    recs = []
    for k in range(5):
        t = now + timedelta(minutes=k)
        recs += run_tick(pf, Market(t, {"GOLDBEES": 100.0, "SILVERBEES": 200.0}, {"GOLDBEES": t, "SILVERBEES": t},
                                    {"GOLDBEES": 0.99, "SILVERBEES": 0.99}), env)
    assert not any(r["action"] == "BUY" for r in recs[:1])
    assert pf.positions["GOLDBEES"].stop == pytest.approx(98.8) and pf.floor >= floor_before
    rej = (env.logs_dir / f"advice-rejected-{D.isoformat()}.jsonl").read_text()
    for word in ("buy", "floor", "tighten_stop", "favour"):
        assert word in rej
    assert not pf.cooldowns.get("fl_bias", {}).get("GOLDBEES", {}).get("bias") == "favour"


def test_future_dated_fastlane_cannot_lock_out_later_advice(tmp_path):
    """Before the fix a fastlane.json dated 2099 was applied and then every later (real) file was ignored."""
    env = env_for(tmp_path)
    now = at(11, 0)
    pf = held_pf()
    _fl(env, {"updated": "2099-01-01T00:00:00+05:30", "GOLDBEES": {"block_until": (now + timedelta(minutes=5)).isoformat()}})
    run_tick(pf, Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {}), env)
    assert "GOLDBEES" not in pf.cooldowns.get("block_until", {})
    assert "future" in (env.logs_dir / f"advice-rejected-{D.isoformat()}.jsonl").read_text()
    later = now + timedelta(minutes=2)
    _fl(env, {"updated": (now + timedelta(minutes=1)).isoformat(), "GOLDBEES": {"sell_fraction": 1.0}})
    run_tick(pf, Market(later, {"GOLDBEES": 100.0}, {"GOLDBEES": later}, {}), env)
    assert "GOLDBEES" not in pf.positions


def test_poisoned_fastlane_marker_from_an_old_version_is_forgotten(tmp_path):
    env = env_for(tmp_path)
    now = at(11, 0)
    pf = held_pf()
    pf.runtime["fl_applied_updated"] = "2099-01-01T00:00:00+05:30"
    _fl(env, {"updated": now.isoformat(), "GOLDBEES": {"sell_fraction": 1.0}})
    run_tick(pf, Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {}), env)
    assert "GOLDBEES" not in pf.positions


@pytest.mark.parametrize("obj", [[1, 2], "x", 5, None, {"updated": [1]}, {"GOLDBEES": {"tighten_stop": "NaN"}},
                                 {"GOLDBEES": {"sell_fraction": 1e309}}, {"GOLDBEES": 5}])
def test_fastlane_with_wrong_types_never_crashes_or_loosens(tmp_path, obj):
    env = env_for(tmp_path)
    if isinstance(obj, dict) and "updated" not in obj:
        obj = {"updated": at(10, 59).isoformat(), **obj}
    _fl(env, obj)
    pf = held_pf()
    now = at(11, 0)
    run_tick(pf, Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {}), env)
    assert pf.positions["GOLDBEES"].qty == 100 and pf.positions["GOLDBEES"].stop == pytest.approx(98.8)
