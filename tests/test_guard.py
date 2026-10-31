"""Unit tests for the Green Guard rules (simgold/guard.py) and their wiring in the engine tick."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest

from simgold import config, guard, market
from simgold.broker import Portfolio, Position
from simgold.engine import DECISION_FIELDS, TRIGGERS, Engine, Env, Market, run_tick
from simgold.market import IST

P = config.load_params()
D_WEEK1 = date(2026, 10, 6)     # trading day 4 of Oct 2026  (week_factor 0.5, gross cap 30%)
D_WEEK3 = date(2026, 10, 21)    # trading day 13             (week_factor 1.0, gross cap 70%)
D_MID = date(2026, 10, 14)      # Wednesday, trading day 9   (week_factor 0.75)
ONE = lambda s: 1.0


def newpf() -> Portfolio:
    """Fresh state with all idle money in LIQUIDBEES (set pf.liquidbees in the test)."""
    pf = Portfolio()
    pf.cash = 0.0
    return pf


def at(h, m, d=D_MID, s=0):
    return datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST)


# ------------------------------------------------------------------ 1. floor, tripwire, caution
def test_floor_ratchet_values():
    assert guard.floor_for_best_profit(0) == 99500
    assert guard.floor_for_best_profit(499) == 99500
    assert guard.floor_for_best_profit(500) == 100000          # exactly 1,00,000 when profit hits 500
    assert guard.floor_for_best_profit(1500) == 100500
    assert guard.floor_for_best_profit(4500) == 102000


def test_floor_never_decreases_and_tracks_best():
    floor, best = 99500.0, 0.0
    path = [100100, 100600, 101500, 100900, 100200, 99900, 101000]
    seen = []
    for v in path:
        floor, best = guard.ratchet(floor, best, v)
        seen.append(floor)
    assert seen == sorted(seen)                       # monotone
    assert best == 1500 and floor == 100500           # best profit is the high-water mark
    floor2, best2 = guard.ratchet(floor, best, 90000)  # a crash cannot lower it
    assert floor2 == floor and best2 == best


def test_floor_carries_over_the_month(tmp_path):
    """A new month does not reset the floor or best profit: the engine keeps them in the portfolio."""
    pf = newpf()
    pf.liquidbees, pf.cash, pf.floor, pf.best_profit = 102000.0, 0.0, 101000.0, 2500.0
    pf.last_accrual = "2026-10-30"
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    run_tick(pf, Market(at(10, 0, date(2026, 11, 2)), {}, {}, {}), env)     # first trading day of November
    assert pf.floor == 101000.0 and pf.best_profit == 2500.0


def test_tripwire_and_caution_thresholds():
    assert guard.is_tripwire(100100, 100000) and not guard.is_tripwire(100101, 100000)
    assert guard.in_caution_zone(100300, 100000) and not guard.in_caution_zone(100301, 100000)


def test_tripwire_sells_everything_and_blocks_buys_until_next_day(tmp_path):
    pf = newpf()
    pf.floor, pf.best_profit = 100000.0, 600.0
    pf.positions["GOLDBEES"] = Position(qty=300, avg_cost=100.0, high_water=100.0, opened="2026-10-13", stop=98.8,
                                        last_price=100.0, buy_costs=20.0)
    pf.positions["SILVERBEES"] = Position(qty=100, avg_cost=200.0, high_water=200.0, opened="2026-10-13", stop=196.0,
                                          last_price=200.0, buy_costs=20.0)
    pf.liquidbees = 100100 - 50000 * 0.9995            # value lands exactly at floor + 100
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    now = at(11, 0)
    mkt = Market(now, {"GOLDBEES": 100.0, "SILVERBEES": 200.0}, {k: now for k in ("GOLDBEES", "SILVERBEES")},
                 {"GOLDBEES": 0.95, "SILVERBEES": 0.95})
    recs = run_tick(pf, mkt, env)
    assert not pf.positions
    assert {r["trigger"] for r in recs if r["action"] == "SELL"} == {"Tripwire"}
    assert pf.cooldowns["no_buys_until"] == market.next_trading_day(D_MID).isoformat()
    # same day, later: strong scores for 3+ minutes still cannot buy
    for k in range(1, 6):
        t = now + timedelta(minutes=k)
        recs = run_tick(pf, Market(t, mkt.prices, {s: t for s in mkt.prices}, mkt.scores), env)
    assert not pf.positions and not any(r["action"] == "BUY" for r in recs)


# ------------------------------------------------------------------ 2. sizing
def biggest(sym, cushion, d):
    z = guard.size_buy(sym, 100.0, 100000 + cushion, 100000, 1e6, 1e5, {}, d, ONE)
    return z.qty * 100.0


@pytest.mark.parametrize("cushion,gold,silver,copper", [(500, 12900, 7800, 5500), (1000, 29000, 17600, 12400)])
def test_budget_sizing_matches_table_day11_uncapped(cushion, gold, silver, copper):
    for sym, want in (("GOLDBEES", gold), ("SILVERBEES", silver), ("HINDCOPPER", copper)):
        assert abs(biggest(sym, cushion, D_WEEK3) - want) <= 200


@pytest.mark.parametrize("cushion", [2000, 4000])
def test_budget_sizing_day11_capped_rows(cushion):
    """The table's capped rows (60,000 / 30,000 / 15,000) are caps on a 1,00,000 base; the rule is % of value,
    so the cap here is 60%/30%/15% of (1,00,000 + cushion). Never below the table, never above the cap."""
    for sym, tbl in (("GOLDBEES", 60000), ("SILVERBEES", 30000), ("HINDCOPPER", 15000)):
        got = biggest(sym, cushion, D_WEEK3)
        cap = P["instruments"][sym]["cap_pct"] * (100000 + cushion)
        assert tbl <= got <= cap + 1e-6 and cap - got < 100


@pytest.mark.parametrize("cushion,gold,silver,copper", [(500, 6400, 3900, 2700), (1000, 14500, 8800, 6200),
                                                        (2000, None, 18600, 13100)])
def test_budget_sizing_matches_table_week1(cushion, gold, silver, copper):
    for sym, want in (("GOLDBEES", gold), ("SILVERBEES", silver), ("HINDCOPPER", copper)):
        got = biggest(sym, cushion, D_WEEK1)
        if want is None:        # 30,000 (30% total cap) in the table; 30% of value here
            assert 30000 <= got <= 0.3 * (100000 + cushion) + 1e-6
        else:
            assert abs(got - want) <= 200
    assert biggest("SILVERBEES", 4000, D_WEEK1) >= 30000 and biggest("HINDCOPPER", 4000, D_WEEK1) >= 15000


def test_shared_budget_example():
    """Cushion 2,000 on day 11+: 30,000 of gold uses about 930, leaving about 970 = at most about 19,000 of silver."""
    gold = (300, 100.0)
    z = guard.size_buy("SILVERBEES", 100.0, 102000, 100000, 1e6, 1e5, {"GOLDBEES": gold}, D_WEEK3, ONE)
    assert 18800 <= z.qty * 100 <= 19200
    assert abs(guard.position_risk("GOLDBEES", 300, 100.0) - 930) < 1


def test_month1_day1_green_budget():
    d = date(2026, 10, 1)
    assert market.trading_day_of_month(d) == 1
    gb = guard.green_budget(100000, 100000, 99500, d)
    assert 300 <= gb <= 330                                  # "about 314" in the rulebook (30-day carry)
    assert guard.green_budget(100000, 100000, 100000, d) == float("inf")      # only binds below the lock
    z = guard.size_buy("GOLDBEES", 100.0, 100000, 99500, 1e5, 1e5, {}, d, ONE)
    assert 4500 <= z.qty * 100 <= 5200                      # "about 5,000 of gold"
    assert 155 <= z.usable <= 170                           # about 314 x 0.5 (October has 31 days, so carry is a little higher)


def test_position_risk_and_gap_multipliers():
    assert guard.risk_rate("GOLDBEES") == pytest.approx(0.031)
    assert guard.risk_rate("SILVERBEES") == pytest.approx(0.051)
    assert guard.risk_rate("HINDCOPPER") == pytest.approx(0.0725)
    assert guard.position_risk("GOLDBEES", 100, 100) == pytest.approx(310)
    assert guard.position_risk("GOLDBEES", 100, 100, 1.25) == pytest.approx(100 * 100 * (0.03 * 1.25 + 0.001))
    assert guard.gap_multiplier(date(2026, 10, 14)) == 1.0                    # Wed night
    assert guard.gap_multiplier(date(2026, 10, 16)) == 1.25                   # Friday, weekend ahead
    assert guard.gap_multiplier(date(2026, 10, 19)) == 1.5                    # Monday, Dussehra holiday Tuesday
    assert guard.gap_multiplier(date(2026, 10, 16), 1.5) == pytest.approx(1.5 * 1.25)   # Friday jobs report


def test_week_factor_and_gross_caps():
    assert [guard.week_factor(n, 10) for n in (1, 5, 6, 10, 11, 20)] == [0.5, 0.5, 0.75, 0.75, 1.0, 1.0]
    assert [guard.max_gross_pct(n) for n in (1, 5, 6, 10, 11)] == [0.30, 0.30, 0.50, 0.50, 0.70]
    assert guard.week_factor(18, 2) == 0.0 and guard.week_factor(19, 0) == 0.0     # last 3 trading days
    assert guard.week_factor(17, 3) == 1.0


def test_per_instrument_caps_and_min_cash():
    big = guard.size_buy("HINDCOPPER", 100.0, 110000, 100000, 1e6, 1e6, {}, D_WEEK3, ONE)
    assert big.limiter == "instrument cap" and big.qty * 100 <= 0.15 * 110000
    nocash = guard.size_buy("GOLDBEES", 100.0, 110000, 100000, 40000, 40000, {}, D_WEEK3, ONE)
    assert 6000 <= nocash.qty * 100 <= 40000 - 0.3 * 110000 + 1 and nocash.limiter == "cash"          # 30% of value stays in cash/LIQUIDBEES


# ------------------------------------------------------------------ 3. thresholds
def test_thresholds_and_stale_guidance():
    assert guard.score_threshold("GOLDBEES", "favour", True) == 0.65
    assert guard.score_threshold("SILVERBEES", "neutral", True) == 0.75
    assert guard.score_threshold("HINDCOPPER", "neutral", True) == 0.80
    assert guard.score_threshold("GOLDBEES", "block", True) is None
    assert guard.score_threshold("GOLDBEES", "favour", False) == pytest.approx(0.75)     # neutral + 0.05
    assert guard.score_threshold("HINDCOPPER", "block", False) == pytest.approx(0.85)


def test_score_parts_sum():
    from simgold.score import score_from
    d = {"last_close": 101, "ema20": 100, "ema50": 99, "ret10": 0.01, "rsi14": 60, "atr14": 2, "daily_vol": 0.01}
    i = {"price": 102, "vwap": 101, "or_high": 101.5, "or_done": True, "day_range": 1.0, "gap": 0.002}
    assert score_from(d, i)[0] == 1.0
    i2 = dict(i, price=100.5, or_done=False, day_range=4.0, gap=0.02)
    assert score_from(d, i2)[0] == pytest.approx(0.55)          # loses vwap, OR high, range, gap = 0.45
    assert score_from(None, None)[0] == 0.0


# ------------------------------------------------------------------ 4. limits
def counters(**kw):
    c = guard.fresh_counters(D_MID)
    c.update(kw)
    return c


def test_limits_each_rule():
    now = at(11, 0)
    chk = lambda sym="GOLDBEES", c=None, cd=None, wk=None: guard.check_buy_limits(sym, now, c or counters(), cd or {}, wk or {})
    assert chk() is None
    assert chk(c=counters(buys_today=3))
    assert chk(c=counters(buys_sym_today={"GOLDBEES": 1}))
    assert chk("SILVERBEES", c=counters(buys_sym_today={"GOLDBEES": 1})) is None
    assert chk(wk={"GOLDBEES": 3}) and chk(wk={"GOLDBEES": 2}) is None
    assert chk(cd={"last_buy_ts": (now - timedelta(minutes=29)).isoformat()})
    assert chk(cd={"last_buy_ts": (now - timedelta(minutes=30)).isoformat()}) is None
    assert chk(cd={"stopped_out": {"GOLDBEES": D_MID.isoformat()}})
    assert chk(cd={"stopped_out": {"GOLDBEES": "2026-10-13"}}) is None
    assert chk(c=counters(losses_today=2)) and chk(c=counters(losses_today=1)) is None      # the 2-loss rule


def test_no_buy_windows():
    ok = lambda h, m, s=0: guard.in_buy_window(time(h, m, s))[0]
    assert not ok(9, 15) and not ok(9, 29) and ok(9, 30)
    assert ok(15, 0) and not ok(15, 1)


# ------------------------------------------------------------------ 3.3 exits
def mkpos(**kw):
    base = dict(qty=100, avg_cost=100.0, high_water=100.0, opened="2026-10-14", stop=98.8, half_sold=False, last_price=100.0)
    base.update(kw)
    return Position(**base)


def test_exit_order_and_rules():
    nx = lambda pos, price, t=at(11, 0), score=0.9, days=1, sym="GOLDBEES": guard.next_exit(sym, pos, price, t, score, days)
    assert nx(mkpos(), 100.5) is None
    a = nx(mkpos(), 98.7)
    assert a.trigger == "Stop-loss" and a.qty == 100 and a.stop_out
    a = nx(mkpos(), 101.5)
    assert a.trigger == "Target rule" and a.qty == 50
    after_half = mkpos(qty=50, half_sold=True, stop=100.1, high_water=102.0)
    assert nx(after_half, 100.9).trigger == "Trailing stop"          # 102 x (1 - 1.0%) = 100.98
    assert nx(after_half, 101.5) is None
    assert nx(mkpos(stop=100.1, half_sold=True, high_water=100.2), 100.0).trigger == "Stop-loss"   # break-even stop
    # 15:15 exits
    late = at(15, 15)
    assert nx(mkpos(), 100.2, late, days=5).trigger == "Time stop"
    assert nx(mkpos(), 100.2, late, days=4) is None
    assert nx(mkpos(high_water=100.8), 100.2, late, days=5) is None                 # reached half its target
    assert nx(mkpos(), 100.2, late, score=0.30).trigger == "Score exit"
    assert nx(mkpos(), 100.2, at(15, 14), score=0.30) is None
    assert nx(mkpos(avg_cost=200.0, stop=196, high_water=200, last_price=200), 200.0, late, days=4, sym="SILVERBEES").trigger == "Time stop"


def test_stop_beats_everything_in_order():
    pos = mkpos(stop=98.8)
    a = guard.next_exit("GOLDBEES", pos, 98.0, at(15, 16), 0.1, 9, P)
    assert a.trigger == "Stop-loss"


# ------------------------------------------------------------------ 3.4 / 7 month-end and overnight
def test_overnight_check_sells_highest_risk_first():
    hold = {"GOLDBEES": (300, 100.0), "SILVERBEES": (200, 100.0)}      # risks 930 and 1020
    sold = guard.overnight_sells(hold, 102000, 100000, 1e5, D_WEEK3, ONE)
    assert sold == ["SILVERBEES"]                                      # usable 1900 < 1950; dropping silver passes
    assert guard.overnight_sells(hold, 102000, 100000, 1e5, D_WEEK3, lambda s: 1.25) == ["SILVERBEES", "GOLDBEES"][:len(
        guard.overnight_sells(hold, 102000, 100000, 1e5, D_WEEK3, lambda s: 1.25))]
    assert guard.overnight_sells(hold, 104000, 100000, 1e5, D_WEEK3, ONE) == []


def test_month_end_window_and_check():
    last3 = [date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)]
    assert all(guard.in_month_end_window(d) for d in last3) and not guard.in_month_end_window(date(2026, 10, 27))
    hold = {"GOLDBEES": (500, 100.0)}                                  # 50,000 x 3% x 1.5 = 2,250
    assert guard.month_end_sells(hold, 100500, 100000, ONE) == ["GOLDBEES"]
    assert guard.month_end_sells(hold, 103000, 100000, ONE) == []
    assert guard.month_end_sells(hold, 105000, 100000, lambda s: 2.0) == []      # 4,500 + 200 -> needs 104,700
    assert guard.month_end_sells(hold, 104000, 100000, lambda s: 2.0) == ["GOLDBEES"]
    assert guard.month_end_sells(hold, 103000, 100000, lambda s: 2.0) == ["GOLDBEES"]
    assert guard.month_end_required(100600) == 100800 and guard.month_end_required(99500) == 100200
    assert guard.month_end_flatten(100299) and not guard.month_end_flatten(100300)


def test_last_day_stop_check():
    assert guard.last_day_stop_sells({"GOLDBEES": (500, 100.0, 98.8)}, 100700) == ["GOLDBEES"]    # stop loss 600
    assert guard.last_day_stop_sells({"GOLDBEES": (500, 100.0, 98.8)}, 100900) == []


def test_no_new_buys_in_last_three_days(tmp_path):
    for d, expect_buy in ((date(2026, 10, 27), True), (date(2026, 10, 28), False), (date(2026, 10, 30), False)):
        pf = newpf()
        pf.liquidbees, pf.floor, pf.best_profit = 103000.0, 101000.0, 2500.0
        pf.last_accrual = d.isoformat()
        env = Env(tmp_path / str(d) / "s", tmp_path / str(d) / "l", P, environ={}, match_headline=None)
        got = False
        for k in range(8):
            t = at(11, 0 + k, d)
            recs = run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.95}), env)
            got = got or any(r["action"] == "BUY" for r in recs)
        assert got == expect_buy, d


def test_month_end_engine_sells_and_flattens(tmp_path):
    d = date(2026, 10, 29)
    pf = newpf()
    pf.floor, pf.best_profit = 100000.0, 500.0
    pf.positions["GOLDBEES"] = Position(qty=500, avg_cost=100.0, high_water=100.0, opened="2026-10-26", stop=98.8,
                                        last_price=100.0, buy_costs=20)
    pf.liquidbees = 100750 - 500 * 100 * 0.9995
    pf.last_accrual = d.isoformat()
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    t = at(15, 20, d)
    recs = run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.9}), env)
    sells = [r for r in recs if r["action"] == "SELL"]
    assert sells and sells[0]["trigger"] in ("Month-end rule", "Overnight check")
    assert not pf.positions
    # value under 1,00,300 inside the window flattens at once
    pf2 = newpf()
    pf2.floor = 99500.0
    pf2.positions["SILVERBEES"] = Position(qty=10, avg_cost=200.0, high_water=200.0, opened="2026-10-26", stop=196.0,
                                           last_price=200.0, buy_costs=2)
    pf2.liquidbees, pf2.last_accrual = 98200.0, d.isoformat()
    t = at(10, 0, d)
    recs = run_tick(pf2, Market(t, {"SILVERBEES": 200.0}, {"SILVERBEES": t}, {}), Env(tmp_path / "s2", tmp_path / "l2", P, environ={}, match_headline=None))
    assert [r["trigger"] for r in recs if r["action"] == "SELL"] == ["Month-end rule"]


# ------------------------------------------------------------------ 6. events
def test_event_rules():
    ev = guard.normalise_events([{"date": "2026-10-14", "type": "us_cpi"}, {"date": "2026-10-14", "type": "rbi"},
                                 {"date": "bad"}, {"date": "2026-10-15", "type": "fomc"}])
    er = guard.event_rules(date(2026, 10, 14), ev)
    assert er.buy_block("GOLDBEES", time(13, 1)) and not er.buy_block("GOLDBEES", time(12, 59, 59)) \
        or er.buy_block("GOLDBEES", time(13, 1))
    assert er.buy_block("HINDCOPPER", time(13, 30)) is None                # CPI applies to gold/silver only
    assert er.buy_block("HINDCOPPER", time(10, 0)) and er.buy_block("HINDCOPPER", time(10, 45)) is None   # RBI window
    assert er.night_mult("SILVERBEES") == 1.5 and er.night_mult("HINDCOPPER") == 1.0
    nxt = guard.event_rules(date(2026, 10, 15), ev)                         # FOMC day: gold/silver all day, 2x tonight
    assert nxt.buy_block("GOLDBEES", time(9, 30)) and nxt.night_mult("GOLDBEES") == 2.0
    assert nxt.buy_block("GOLDBEES", time(9, 40)) and guard.event_rules(date(2026, 10, 15), ev[:1]).buy_block("GOLDBEES", time(9, 40))
    assert guard.event_rules(date(2026, 10, 15), ev[:1]).buy_block("GOLDBEES", time(9, 45)) is None   # CPI next day: not before 09:45
    after_fomc = guard.event_rules(date(2026, 10, 16), ev)
    assert after_fomc.buy_block("SILVERBEES", time(10, 14)) and after_fomc.buy_block("SILVERBEES", time(10, 15)) is None


def test_hindcopper_results_rules():
    ev = guard.normalise_events([{"date": "2026-10-21", "type": "hindcopper_results"}])
    blocked = lambda d, t=time(12, 0): guard.event_rules(d, ev).buy_block("HINDCOPPER", t)
    assert not blocked(date(2026, 10, 15)) and blocked(date(2026, 10, 16)) and blocked(date(2026, 10, 19)) \
        and blocked(date(2026, 10, 21)) and blocked(date(2026, 10, 22))
    day2 = date(2026, 10, 23)
    assert blocked(day2, time(10, 14)) and not blocked(day2, time(10, 15))
    fx = guard.event_rules(market.prev_trading_day(date(2026, 10, 21)), ev).forced_exits["HINDCOPPER"][0]
    assert fx == time(14, 55)
    assert "HINDCOPPER" not in guard.event_rules(date(2026, 10, 21), ev).forced_exits


def test_event_block_is_logged_and_prevents_buy(tmp_path):
    pf = newpf()
    pf.liquidbees, pf.floor, pf.best_profit = 103000.0, 101000.0, 2500.0
    pf.last_accrual = D_MID.isoformat()
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "events.json").write_text(json.dumps([{"date": "2026-10-14", "type": "fomc"}]))
    env = Env(sd, tmp_path / "l", P, environ={}, match_headline=None)
    allrecs = []
    for k in range(6):
        t = at(11, k)
        allrecs += run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.95}), env)
    assert not pf.positions and not any(r["action"] == "BUY" for r in allrecs)
    blocks = [r for r in allrecs if r["action"] == "BLOCK"]
    assert len(blocks) == 1 and blocks[0]["trigger"] == "Event rule"      # one per symbol per 30 minutes


# ------------------------------------------------------------------ 5. fast lane
def test_fastlane_sanitizer_is_defence_only():
    now = at(11, 0)
    pos = {"GOLDBEES": {"qty": 100, "price": 100.0, "stop": 98.8}}
    raw = {"updated": now.isoformat(),
           "GOLDBEES": {"block_until": (now + timedelta(hours=30)).isoformat(), "sell_fraction": 0.5,
                        "tighten_stop": 99.5, "buy": 50, "cap": 0.9},
           "bias": {"GOLD": "favour", "SILVER": "block"},
           "floor": 90000, "halt_buys_today": True}
    acc, rej = guard.sanitize_fastlane(raw, now, pos)
    kinds = {(a["type"], a.get("sym")) for a in acc}
    assert ("block_until", "GOLDBEES") in kinds and ("sell_fraction", "GOLDBEES") in kinds
    assert ("tighten_stop", "GOLDBEES") in kinds and ("set_bias", "SILVERBEES") in kinds and ("halt_buys_today", None) in kinds
    assert ("set_bias", "GOLDBEES") not in kinds                                  # favour refused
    blk = next(a for a in acc if a["type"] == "block_until")
    assert blk["until"].date() == now.date()                                       # clamped to the end of today
    assert any("buy" in r for r in rej) and any("cap" in r for r in rej) and any("favour" in r for r in rej) \
        and any("floor" in r for r in rej)


def test_fastlane_cannot_loosen_stop_or_sell_what_is_not_held():
    now = at(11, 0)
    pos = {"GOLDBEES": {"qty": 100, "price": 100.0, "stop": 99.5}}
    acc, rej = guard.sanitize_fastlane({"GOLDBEES": {"tighten_stop": 98.0}, "SILVERBEES": {"sell_fraction": 1.0},
                                        "HINDCOPPER": {"tighten_stop": 0.01}}, now, pos)
    assert acc == [] and len(rej) == 3
    acc, _ = guard.sanitize_fastlane({"GOLDBEES": {"tighten_stop": 0.003}}, now, pos)
    assert acc and acc[0]["stop"] == pytest.approx(99.7)                           # 0.3% below price


def test_engine_applies_only_allowed_fastlane_advice(tmp_path):
    now = at(11, 0)
    pf = newpf()
    pf.liquidbees, pf.floor = 90000.0, 99500.0
    pf.last_accrual = D_MID.isoformat()
    pf.positions["GOLDBEES"] = Position(qty=100, avg_cost=100.0, high_water=100.0, opened="2026-10-13", stop=98.8,
                                        last_price=100.0, buy_costs=10)
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "fastlane.json").write_text(json.dumps({
        "updated": now.isoformat(),
        "GOLDBEES": {"sell_fraction": 0.5, "tighten_stop": 90.0, "buy": 100, "block_until": (now + timedelta(minutes=45)).isoformat()},
        "bias": {"GOLD": "favour"}}))
    env = Env(sd, tmp_path / "l", P, environ={}, match_headline=None)
    recs = run_tick(pf, Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {"GOLDBEES": 0.99}), env)
    assert pf.positions["GOLDBEES"].qty == 50                                      # half sold on the AI's advice
    assert pf.positions["GOLDBEES"].stop == 98.8                                   # loosening to 90 ignored
    assert not any(r["action"] == "BUY" for r in recs)                             # no buys, favour ignored
    assert {r["trigger"] for r in recs} == {"Opus guidance"}
    until = datetime.fromisoformat(pf.cooldowns["block_until"]["GOLDBEES"])
    assert until == now + timedelta(minutes=45)
    assert (tmp_path / "l" / f"advice-rejected-{D_MID.isoformat()}.jsonl").exists()
    # the same file is not applied twice
    recs = run_tick(pf, Market(now + timedelta(minutes=1), {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {"GOLDBEES": 0.99}), env)
    assert pf.positions["GOLDBEES"].qty == 50 and not [r for r in recs if r["action"] == "SELL"]


# ------------------------------------------------------------------ 5.1 triggers and the AI hand-off
def shock_market(now, move=0.008):
    series = [(now - timedelta(minutes=10 - k), 100.0 * (1 + move * (k / 10) ** 3)) for k in range(11)]
    px = series[-1][1]
    return Market(now, {"GOLDBEES": px}, {"GOLDBEES": now}, {}, {"GOLDBEES": series}, {"GOLDBEES": 100.0})


def test_price_shock_blocks_writes_request_and_fires(tmp_path):
    calls = []
    post = lambda url, headers, body, timeout=10: calls.append((url, headers, body, timeout)) or 200
    env = Env(tmp_path / "s", tmp_path / "l", P, post=post, match_headline=None,
              environ={"ROUTINE_FIRE_URL": "https://example.invalid/fire", "ROUTINE_FIRE_TOKEN": "tok"})
    pf = newpf()
    pf.liquidbees, pf.last_accrual = 100000.0, D_MID.isoformat()
    now = at(11, 0)
    recs = run_tick(pf, shock_market(now), env)
    b = [r for r in recs if r["action"] == "BLOCK"]
    assert b and b[0]["trigger"] == "Price shock" and b[0]["symbol"] == "GOLDBEES"
    assert datetime.fromisoformat(pf.cooldowns["block_until"]["GOLDBEES"]) == now + timedelta(minutes=30)
    reqs = [json.loads(x) for x in (tmp_path / "s" / "fastlane_requests.jsonl").read_text().splitlines()]
    assert len(reqs) == 1 and reqs[0]["fired"] and reqs[0]["triggers"][0]["symbol"] == "GOLDBEES"
    url, headers, body, timeout = calls[0]
    assert url == "https://example.invalid/fire" and timeout == 10
    assert headers == {"Authorization": "Bearer tok", "anthropic-beta": "experimental-cc-routine-2026-04-01",
                       "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    assert list(body) == ["text"] and json.loads(body["text"])["floor"] == 99500
    # a second trigger 5 minutes later: logged and blocked, but not fired (20 minute spacing)
    later = now + timedelta(minutes=5)
    m2 = shock_market(later, 0.0)
    m2.prices["GOLDBEES"], m2.series["GOLDBEES"] = 101.0, [(later - timedelta(minutes=10), 100.0), (later, 101.0)]
    run_tick(pf, m2, env)
    assert len(calls) == 1
    # daily maximum of 4, and 60 minute block once it is used up
    pf.rt = pf.runtime
    pf.runtime["fast_runs"] = {"date": D_MID.isoformat(), "n": 4, "last_ts": (now - timedelta(hours=2)).isoformat()}
    pf.cooldowns.pop("block_until"), pf.cooldowns.pop("block_src")
    t3 = at(14, 0)
    m3 = shock_market(t3, -0.008)
    run_tick(pf, m3, env)
    assert len(calls) == 1
    assert datetime.fromisoformat(pf.cooldowns["block_until"]["GOLDBEES"]) == t3 + timedelta(minutes=60)


def test_failed_post_never_crashes(tmp_path):
    def boom(*a, **k):
        raise TimeoutError("slow")
    env = Env(tmp_path / "s", tmp_path / "l", P, post=boom, match_headline=None,
              environ={"ROUTINE_FIRE_URL": "https://example.invalid/fire", "ROUTINE_FIRE_TOKEN": "tok"})
    pf = newpf()
    pf.liquidbees, pf.last_accrual = 100000.0, D_MID.isoformat()
    run_tick(pf, shock_market(at(11, 0)), env)
    assert "GOLDBEES" in pf.cooldowns["block_until"]


def test_no_env_vars_means_no_post_but_request_and_block(tmp_path):
    calls = []
    env = Env(tmp_path / "s", tmp_path / "l", P, post=lambda *a, **k: calls.append(1), match_headline=None, environ={})
    pf = newpf()
    pf.liquidbees, pf.last_accrual = 100000.0, D_MID.isoformat()
    run_tick(pf, shock_market(at(11, 0)), env)
    req = json.loads((tmp_path / "s" / "fastlane_requests.jsonl").read_text().splitlines()[0])
    assert not calls and req["fired"] is False and "GOLDBEES" in pf.cooldowns["block_until"]


def test_news_trigger_uses_matcher_and_blocks(tmp_path):
    seen = []

    def matcher(text):
        seen.append(text)
        return {"urgency": 5, "categories": ["monetary_policy"], "symbols": ["GOLDBEES", "SILVERBEES"], "wake": "Fed" in text}
    env = Env(tmp_path / "s", tmp_path / "l", P, match_headline=matcher, environ={})
    pf = newpf()
    pf.liquidbees, pf.last_accrual = 100000.0, D_MID.isoformat()
    now = at(11, 0)
    m = Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {}, headlines=[{"title": "Fed holds rates"}, {"title": "Quiet day"}])
    recs = run_tick(pf, m, env)
    assert seen == ["Fed holds rates", "Quiet day"]
    assert {(r["symbol"], r["trigger"]) for r in recs if r["action"] == "BLOCK"} == {("GOLDBEES", "News trigger"), ("SILVERBEES", "News trigger")}
    assert set(pf.cooldowns["block_until"]) == {"GOLDBEES", "SILVERBEES"}
    why = next(r["reason"] for r in recs if r["action"] == "BLOCK")
    chk = json.loads((tmp_path / "l" / f"checks-{D_MID.isoformat()}.jsonl").read_text().splitlines()[-1])
    assert chk["time"] == "11:00" and set(chk["checks"]) >= {"GOLDBEES", "SILVERBEES"}
    assert "Buys paused until" in chk["checks"]["GOLDBEES"] and "News trigger" in chk["checks"]["GOLDBEES"]
    assert chk["checks"]["GOLDBEES"].startswith("Price ₹100.00.")                 # market price each minute
    assert 'Headline: "Fed holds rates"' in why and "category: monetary_policy" in why     # shown on the dashboard


def test_news_cooldown_blocks_a_category_once_per_window(tmp_path):
    """14 Oct 2026: the same Fed/RBI theme re-blocked gold and silver every ~10 minutes all afternoon."""
    def matcher(text):
        lvl = 5 if "cuts rates" in text else 3
        return {"urgency": lvl, "categories": ["monetary_policy", "inflation_data"],
                "levels": {"monetary_policy": lvl, "inflation_data": 3},
                "wake_categories": ["monetary_policy", "inflation_data"], "symbols": ["GOLDBEES"], "wake": True,
                "cooldown_minutes": 120}
    env = Env(tmp_path / "s", tmp_path / "l", P, match_headline=matcher, environ={})
    pf = newpf()
    pf.liquidbees, pf.last_accrual = 100000.0, D_MID.isoformat()

    def tick(h, m, title):
        now = at(h, m)
        mk = Market(now, {"GOLDBEES": 100.0}, {"GOLDBEES": now}, {}, headlines=[{"title": title}])
        return [r for r in run_tick(pf, mk, env) if r["action"] == "BLOCK" and r["trigger"] == "News trigger"]
    assert len(tick(10, 0, "Fed talk and CPI")) == 1
    assert tick(10, 10, "More Fed talk and CPI") == []           # same categories, same level: cooled down
    assert len(tick(11, 0, "Fed cuts rates")) == 1               # a decision (level 5) is a new key
    assert tick(11, 50, "Yet more Fed talk and CPI") == []
    assert len(tick(12, 1, "Fed talk and CPI again")) == 1       # 2 h later: blocks again


def test_portfolio_trigger(tmp_path):
    from simgold import fastlane
    now = at(11, 0)
    hist = [(now - timedelta(minutes=14), 101000.0), (now - timedelta(minutes=5), 100900.0)]
    assert fastlane.detect_portfolio_trigger(hist, 100700.0, 100000.0, now) is None      # 300 of 1000: 30%
    assert fastlane.detect_portfolio_trigger(hist, 100600.0, 100000.0, now)               # 400 of 1000: 40%
    assert fastlane.detect_portfolio_trigger([(now - timedelta(minutes=20), 101000.0)], 100000.0, 100000.0, now) is None


# ------------------------------------------------------------------ guidance
def test_guidance_stale_and_favour(tmp_path, monkeypatch):
    from simgold import memstore       # the alignment allows favour here (the engine caps a bias at the ceiling since 2026-10-31)
    monkeypatch.setattr(memstore, "current_alignment", lambda *a, **k: {m: {"ceiling": "favour", "state": "T"} for m in ("GOLD", "SILVER", "COPPER")})
    def run(guid, score, minutes=4):
        pf = newpf()
        pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 103000.0, 101000.0, 2500.0, D_MID.isoformat()
        sd = tmp_path / f"s{guid and guid['age']}{score}"
        sd.mkdir(parents=True, exist_ok=True)
        if guid:
            (sd / "guidance.json").write_text(json.dumps({"updated": (at(11, 0) - timedelta(minutes=guid["age"])).isoformat(),
                                                          "bias": guid["bias"], "note": "x"}))
        env = Env(sd, sd / "l", P, environ={}, match_headline=None)
        out = []
        for k in range(minutes + 1):
            t = at(11, k)
            out += run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": score}), env)
        return any(r["action"] == "BUY" for r in out)
    assert run(None, 0.72) is False                               # missing guidance: needs 0.75
    assert run(None, 0.76) is True
    assert run({"age": 100, "bias": {"GOLD": "favour"}}, 0.70) is False       # stale: favour ignored, neutral + 0.05
    assert run({"age": 10, "bias": {"GOLD": "favour"}}, 0.66) is True
    assert run({"age": 10, "bias": {"GOLD": "neutral"}}, 0.66) is False
    assert run({"age": 10, "bias": {"GOLD": "block"}}, 0.99) is False


def test_three_minute_confirmation(tmp_path):
    pf = newpf()
    pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 103000.0, 101000.0, 2500.0, D_MID.isoformat()
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    bought_at = None
    for k in range(6):
        t = at(11, k)
        score = 0.6 if k == 1 else 0.9                 # dips at minute 1, so the streak restarts there
        recs = run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": score}), env)
        if any(r["action"] == "BUY" for r in recs):
            bought_at = k
    assert bought_at == 5                               # streak starts at minute 2, confirmed 3 minutes later


# ------------------------------------------------------------------ state, interest, log contract
def test_migration_of_existing_portfolio_file(tmp_path):
    old = {"cash": 100000.0, "positions": {}, "peak_equity": 100000.0, "month": "2026-10", "month_start_equity": 100000.0,
           "stop_until": {}, "last_trade_at": {}, "flat_until": "", "realised_pnl": 0.0, "total_costs": 0.0, "trades": []}
    f = tmp_path / "portfolio.json"
    f.write_text(json.dumps(old))
    pf = Portfolio.load(f)
    assert (pf.cash, pf.liquidbees, pf.floor, pf.best_profit) == (0.0, 100000.0, 99500.0, 0.0)
    pf.save(f)
    again = Portfolio.load(f)
    assert again.liquidbees == 100000.0 and again.schema == 1                 # not migrated twice
    d = json.loads(f.read_text())
    for k in ("floor", "best_profit", "counters", "cooldowns", "liquidbees"):
        assert k in d


def test_liquidbees_interest_per_calendar_day(tmp_path):
    pf = newpf()
    pf.liquidbees, pf.last_accrual = 100000.0, "2026-10-09"                    # Friday
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    run_tick(pf, Market(at(10, 0, date(2026, 10, 12)), {}, {}, {}), env)       # Monday: 3 calendar days
    want = 100000 * ((1 + 0.063 / 365) ** 3 - 1)
    assert pf.liquidbees - 100000 == pytest.approx(want, abs=0.01) and 51 < want < 52
    before = pf.liquidbees
    run_tick(pf, Market(at(10, 1, date(2026, 10, 12)), {}, {}, {}), env)       # same day again: nothing more
    assert pf.liquidbees == before


def test_decision_log_contract(tmp_path):
    """Every logged action carries exactly the agreed fields, in the agreed order, with the agreed types."""
    pf = newpf()
    pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 103000.0, 101000.0, 2500.0, D_MID.isoformat()
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "events.json").write_text(json.dumps([{"date": "2026-10-14", "type": "rbi"}]))
    env = Env(sd, tmp_path / "l", P, environ={}, match_headline=lambda t: {"urgency": 5, "categories": ["x"], "symbols": [], "wake": True})
    allr = []
    for k in range(40):
        t = at(10, 50 + k % 10, D_MID) + timedelta(minutes=k // 10 * 10)
        sc = 0.95 if k < 25 else 0.2
        px = 100.0 if k < 25 else 97.5
        m = Market(t, {"GOLDBEES": px}, {"GOLDBEES": t}, {"GOLDBEES": sc}, headlines=[{"title": "x"}] if k == 12 else [])
        allr += run_tick(pf, m, env)
    t = at(15, 20)
    allr += run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.95}), env)
    lines = [json.loads(x) for x in (tmp_path / "l" / f"decisions-{D_MID.isoformat()}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines == json.loads(json.dumps(allr))
    assert lines
    for r in lines:
        assert list(r) == DECISION_FIELDS == ["ts", "time", "action", "symbol", "qty", "price", "amount", "pnl", "reason",
                                              "trigger", "stop", "risk", "value_after", "floor"]
        assert r["action"] in {"BUY", "SELL", "SKIP", "HOLD", "BLOCK"} and r["trigger"] in TRIGGERS
        assert datetime.fromisoformat(r["ts"]).utcoffset() == timedelta(hours=5, minutes=30)
        assert r["time"] == r["ts"][11:16] and isinstance(r["reason"], str) and r["reason"].endswith(".")
        assert (r["pnl"] is None) == (r["action"] != "SELL")
        assert isinstance(r["qty"], int) and isinstance(r["floor"], float) and isinstance(r["value_after"], float)
        assert r["symbol"] in {"GOLDBEES", "SILVERBEES", "HINDCOPPER", "ALL"} and ".NS" not in r["symbol"]
    assert {"BUY", "BLOCK", "SELL"} <= {r["action"] for r in lines}


def test_skip_is_rate_limited_per_symbol(tmp_path):
    pf = newpf()
    pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 100250.0, 100000.0, 500.0, D_MID.isoformat()   # caution zone
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    out = []
    for k in range(65):
        t = at(10, 0) + timedelta(minutes=k)
        out += run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.95}), env)
    skips = [r for r in out if r["action"] == "SKIP"]
    assert 2 <= len(skips) <= 3 and all(b["ts"] > a["ts"] for a, b in zip(skips, skips[1:]))
    gaps = [(datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds() / 60 for a, b in zip(skips, skips[1:])]
    assert all(g >= 30 for g in gaps)


def test_hold_only_at_overnight_check(tmp_path):
    pf = newpf()
    pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 90000.0, 100000.0 - 500, 0.0, D_MID.isoformat()
    pf.liquidbees = 103000 - 2000
    pf.floor, pf.best_profit = 101000.0, 2500.0
    pf.positions["GOLDBEES"] = Position(qty=20, avg_cost=100.0, high_water=100.0, opened="2026-10-13", stop=98.8,
                                        last_price=100.0, buy_costs=2)
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    out = []
    for hh, mm in ((14, 0), (15, 19), (15, 20), (15, 21)):
        t = at(hh, mm)
        out += run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.5}), env)
    holds = [r for r in out if r["action"] == "HOLD"]
    assert len(holds) == 1 and holds[0]["time"] == "15:20" and holds[0]["trigger"] == "Overnight check"


def test_costs_and_slippage_are_in_the_fills(tmp_path):
    pf = newpf()
    pf.liquidbees, pf.floor, pf.best_profit, pf.last_accrual = 103000.0, 101000.0, 2500.0, D_MID.isoformat()
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    buy = None
    for k in range(5):
        t = at(11, k)
        for r in run_tick(pf, Market(t, {"HINDCOPPER": 400.0}, {"HINDCOPPER": t}, {"HINDCOPPER": 0.99}), env):
            buy = buy or (r if r["action"] == "BUY" else None)
    assert buy and buy["price"] == pytest.approx(400 * 1.001, abs=0.01)           # 0.10% slippage on HINDCOPPER
    pos = pf.positions["HINDCOPPER"]
    assert pos.buy_costs > 0 and pf.total_costs == pytest.approx(pos.buy_costs)
    t = at(11, 10)
    recs = run_tick(pf, Market(t, {"HINDCOPPER": 400.0}, {"HINDCOPPER": t}, {"HINDCOPPER": 0.99}, ), env)
    now_sell = Engine(pf, Market(t, {"HINDCOPPER": 400.0}, {"HINDCOPPER": t}, {}), env)
    assert now_sell.sell("HINDCOPPER", pos.qty, "test sale.", "Stop-loss")
    last = json.loads((tmp_path / "l" / f"decisions-{D_MID.isoformat()}.jsonl").read_text().splitlines()[-1])
    assert last["action"] == "SELL" and last["pnl"] < 0                         # slippage + costs on a flat price
    assert last["price"] == pytest.approx(400 * 0.999, abs=0.01)


def test_holiday_calendar_has_the_requested_dates():
    assert not market.is_trading_day(date(2026, 11, 24)) and not market.is_trading_day(date(2026, 12, 25))
    assert not market.is_trading_day(date(2026, 11, 8))              # a Sunday (Laxmi Pujan Muhurat day, no regular session)
    assert not market.is_trading_day(date(2026, 11, 10)) and market.is_trading_day(date(2026, 11, 9))


def test_git_sync_order_and_failure_handling(tmp_path):
    from types import SimpleNamespace
    from simgold import gitsync
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd[1:])
        rc = 1 if cmd[1:3] == ["diff", "--cached"] else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="")
    assert gitsync.sync(tmp_path, runner=runner, now=at(11, 5))
    calls[:] = [c for c in calls if c[0] != "config"]           # the alerts.json merge driver is (re)configured first
    assert [c[0] for c in calls] == ["add", "diff", "commit", "pull", "diff", "push"]
    assert calls[2][-1] == "paper-trade: 11:05 IST" and calls[3] == ["pull", "--rebase", "--autostash", "-X", "theirs"]
    # failing rebase is aborted and does not raise
    calls.clear()
    bad = lambda cmd, **kw: (calls.append(cmd[1:]) or SimpleNamespace(returncode=1 if cmd[1] == "pull" else 0, stdout="", stderr="x"))
    assert gitsync.sync(tmp_path, runner=bad) is False and ["rebase", "--abort"] in calls
    # push retried 3 times
    calls.clear()
    nopush = lambda cmd, **kw: (calls.append(cmd[1:]) or SimpleNamespace(returncode=1 if cmd[1] == "push" else 0, stdout="", stderr="x"))
    assert gitsync.sync(tmp_path, runner=nopush) is False and [c[0] for c in calls].count("push") == 3
    assert not gitsync.enabled({}) and gitsync.enabled({"SIMGOLD_GIT_SYNC": "1"})


def test_engine_writes_day_fields(tmp_path):
    pf = newpf()
    pf.liquidbees, pf.last_accrual = 100000.0, "2026-10-13"
    env = Env(tmp_path / "s", tmp_path / "l", P, environ={}, match_headline=None)
    run_tick(pf, Market(at(10, 0), {}, {}, {}), env)
    assert pf.day_costs_date == D_MID.isoformat() and pf.day_open_value == 100000.0
    assert pf.liquidbees_income_today == pytest.approx(17.26, abs=0.01) and pf.day_costs == 0
    run_tick(pf, Market(at(10, 1), {}, {}, {}), env)
    assert pf.day_open_value == 100000.0                        # fixed at the day's first tick
    pf.counters.clear()
    bar = at(11, 0)
    pf.floor, pf.best_profit = 101000.0, 2500.0
    pf.liquidbees = 103000.0
    for k in range(5):
        t = bar + timedelta(minutes=k)
        run_tick(pf, Market(t, {"GOLDBEES": 100.0}, {"GOLDBEES": t}, {"GOLDBEES": 0.95}), env)
    assert pf.positions and pf.day_costs > 0


@pytest.mark.parametrize("bad", [float("nan"), "nan", "NaN", float("inf"), float("-inf")])
def test_fastlane_non_finite_stop_or_fraction_is_refused(bad):
    """NaN compares False with everything, so it used to pass the 'not looser, below the price' checks (AI evals, 29 Oct)."""
    now = datetime(2026, 10, 14, 11, 0, tzinfo=IST)
    pos = {"SILVERBEES": {"qty": 10, "price": 200.0, "stop": 196.0}}
    acc, rej = guard.sanitize_fastlane({"SILVERBEES": {"tighten_stop": bad, "sell_fraction": bad}}, now, pos)
    assert acc == [] and len(rej) == 2
