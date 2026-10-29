"""PR #18 review fixes (strategy v2 engine): per-symbol checks with fresh prices, exit catch-up, no re-buy after a fast-lane exit,
AI stops enforced every tick, hard account stop, history failure, mode switch, first v2 day."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta

import numpy as np

import test_engine_v2 as T
from conftest import v2_params
from simgold import alerts, config, fastlane, trend
from simgold.broker import Portfolio, Position
from simgold.engine import Market, run_tick
from simgold.market import IST

SYMS = T.SYMS
UP = {s: T.series("up", s) for s in SYMS}


def mk(closes, i, hh, mm, ages=None, prices=None, hist=True):
    """A market snapshot for day index i at hh:mm. ages: {sym: minutes the price is old}."""
    day = T.DAYS[i].to_pydatetime()
    now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    ages = ages or {}
    pr = prices if prices is not None else {s: float(closes[s][i]) for s in SYMS}
    h = {}
    if hist:
        for s in SYMS:
            c = closes[s][:i]
            h[s] = trend.clean_bars(c.tolist(), (c * 1.004).tolist(), (c * 0.996).tolist())
    return Market(now=now, prices=pr, price_ts={s: now - timedelta(minutes=ages.get(s, 0)) for s in pr}, scores={},
                  prev_close={s: float(closes[s][i - 1]) for s in SYMS}, hist=h)


def decs(env, i=None):
    return T.all_decisions(env) if i is None else T.read_decisions(env, i)


def fresh(tmp_path, cash=100000.0, params=None):
    return T.fresh_pf(cash), T.make_env(tmp_path, params)


def fastlane_file(env, i, body, hh=11, mm=0):
    day = T.DAYS[i]
    now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    env.state_dir.mkdir(parents=True, exist_ok=True)
    (env.state_dir / "fastlane.json").write_text(json.dumps({"updated": now.isoformat(), **body}))


def crash(closes, i, factor=0.8):
    out = {s: c.copy() for s, c in closes.items()}
    for s in SYMS:
        out[s][i:] = closes[s][i - 1] * factor
    return out


# ------------------------------------------------------------------ 1. per-symbol checks, fresh prices only
def test_stale_price_at_1510_is_retried_and_buys_at_the_first_fresh_minute(tmp_path):
    pf, env = fresh(tmp_path)
    run_tick(pf, mk(UP, 300, 15, 10, ages={s: 5 for s in SYMS}), env)
    assert not pf.positions                                           # nothing bought on a 5 minute old price
    assert not [d for d in decs(env, 300) if d["action"] in ("BUY", "SELL", "TARGET")]
    run_tick(pf, mk(UP, 300, 15, 11), env)
    assert set(pf.positions) == set(SYMS)
    buys = [d for d in decs(env, 300) if d["action"] == "BUY"]
    assert len(buys) == 3 and all(d["time"] == "15:11" for d in buys)


def test_each_symbol_is_checked_when_its_own_price_is_fresh(tmp_path):
    pf, env = fresh(tmp_path)
    run_tick(pf, mk(UP, 300, 15, 10, ages={"SILVERBEES": 6}), env)
    assert "GOLDBEES" in pf.positions and "HINDCOPPER" in pf.positions and "SILVERBEES" not in pf.positions
    run_tick(pf, mk(UP, 300, 15, 11, ages={"SILVERBEES": 6}), env)
    assert "SILVERBEES" not in pf.positions
    run_tick(pf, mk(UP, 300, 15, 12), env)
    assert "SILVERBEES" in pf.positions
    times = {d["symbol"]: d["time"] for d in decs(env, 300) if d["action"] == "BUY"}
    assert times == {"GOLDBEES": "15:10", "HINDCOPPER": "15:10", "SILVERBEES": "15:12"}


def test_never_fresh_gives_one_plain_skip_per_symbol_at_1525_and_no_trade(tmp_path):
    pf, env = fresh(tmp_path)
    for m in range(10, 26):
        run_tick(pf, mk(UP, 300, 15, m, ages={s: 9 for s in SYMS}), env)
    assert not pf.positions
    sk = [d for d in decs(env, 300) if d["action"] == "SKIP"]
    assert sorted(d["symbol"] for d in sk) == sorted(SYMS) and all(d["time"] == "15:25" and "No fresh price" in d["reason"] for d in sk)
    run_tick(pf, mk(UP, 300, 15, 27, ages={s: 9 for s in SYMS}), env)
    assert len([d for d in decs(env, 300) if d["action"] == "SKIP"]) == 3


def test_a_pause_that_ends_before_1525_keeps_the_symbol_pending(tmp_path):
    pf, env = fresh(tmp_path)
    until = datetime(T.DAYS[300].year, T.DAYS[300].month, T.DAYS[300].day, 15, 15, tzinfo=IST)
    pf.cooldowns["block_until"] = {"GOLDBEES": until.isoformat()}
    pf.cooldowns["block_src"] = {"GOLDBEES": "Price shock"}
    run_tick(pf, mk(UP, 300, 15, 10), env)
    assert "GOLDBEES" not in pf.positions and "SILVERBEES" in pf.positions
    for m in (11, 12):
        run_tick(pf, mk(UP, 300, 15, m), env)
    assert len([d for d in decs(env, 300) if d["action"] == "BLOCK" and d["symbol"] == "GOLDBEES"]) == 1     # logged once
    run_tick(pf, mk(UP, 300, 15, 16), env)
    assert "GOLDBEES" in pf.positions


# ------------------------------------------------------------------ 2. exits are caught up
def held_on_day(tmp_path, i=300):
    pf, env = fresh(tmp_path)
    run_tick(pf, mk(UP, i, 15, 10), env)
    assert set(pf.positions) == set(SYMS)
    return pf, env


def test_first_tick_after_the_window_sells_a_trend_exit(tmp_path):
    pf, env = held_on_day(tmp_path)
    run_tick(pf, mk(crash(UP, 301), 301, 15, 26), env)
    assert not pf.positions
    assert all(d["trigger"] == "Trend exit" for d in decs(env, 301) if d["action"] == "SELL")


def test_a_missed_day_is_caught_up_on_the_next_days_first_tick_exits_only(tmp_path):
    pf, env = held_on_day(tmp_path)
    cr = crash(UP, 302)
    run_tick(pf, mk(cr, 302, 9, 20), env)                             # day 301 had no tick at all
    assert not pf.positions
    assert not [d for d in decs(env, 302) if d["action"] == "BUY"]
    assert pf.runtime["v2"]["last_full"] == T.DAYS[301].date().isoformat()


def test_catch_up_never_buys(tmp_path):
    pf, env = fresh(tmp_path)
    run_tick(pf, mk(UP, 299, 15, 10, ages={"SILVERBEES": 9, "HINDCOPPER": 9}), env)     # only GOLDBEES done on day 299
    run_tick(pf, mk(UP, 301, 9, 20), env)                             # next-next morning, trends ON, SILVER and COPPER flat
    assert "SILVERBEES" not in pf.positions and "HINDCOPPER" not in pf.positions


def test_exit_for_a_priceless_symbol_retries_until_a_price_arrives(tmp_path):
    pf, env = held_on_day(tmp_path)
    cr = crash(UP, 301)
    px = {s: float(cr[s][301]) for s in SYMS if s != "SILVERBEES"}
    run_tick(pf, mk(cr, 301, 15, 10, prices=px), env)
    assert "SILVERBEES" in pf.positions and "GOLDBEES" not in pf.positions
    run_tick(pf, mk(cr, 301, 15, 27, prices=px), env)
    assert "SILVERBEES" in pf.positions
    run_tick(pf, mk(cr, 301, 15, 28), env)
    assert "SILVERBEES" not in pf.positions


# ------------------------------------------------------------------ 3. fast-lane exits are not undone
def test_fast_lane_sale_is_not_bought_back_the_same_day_but_may_be_next_day(tmp_path):
    pf, env = held_on_day(tmp_path)
    fastlane_file(env, 301, {"GOLDBEES": {"sell_fraction": 1.0}})
    run_tick(pf, mk(UP, 301, 11, 0), env)
    assert "GOLDBEES" not in pf.positions
    run_tick(pf, mk(UP, 301, 15, 10), env)
    assert "GOLDBEES" not in pf.positions
    blk = [d for d in decs(env, 301) if d["action"] == "BLOCK" and d["symbol"] == "GOLDBEES"]
    assert blk and "not bought back before" in blk[0]["reason"] and blk[0]["trigger"] == "Opus guidance"
    run_tick(pf, mk(UP, 302, 15, 10), env)
    assert "GOLDBEES" in pf.positions


# ------------------------------------------------------------------ 4. AI stops
def test_tighten_stop_is_logged_kept_apart_and_enforced_every_tick(tmp_path):
    pf, env = held_on_day(tmp_path)
    px = float(UP["SILVERBEES"][301])
    stop = round(px * 0.99, 2)
    fastlane_file(env, 301, {"SILVERBEES": {"tighten_stop": stop}})
    run_tick(pf, mk(UP, 301, 11, 0), env)
    pos = pf.positions["SILVERBEES"]
    assert pos.ai_stop == stop and pos.stop == 0.0                      # a separate stop; the trend level is not a stop
    assert any(d["action"] == "HOLD" and "tightened" in d["reason"] for d in decs(env, 301))
    low = {s: float(UP[s][301]) for s in SYMS}
    low["SILVERBEES"] = stop - 0.01
    run_tick(pf, mk(UP, 301, 11, 5, prices=low), env)
    assert "SILVERBEES" in pf.positions                                 # D5-S: one price under the stop is not enough
    run_tick(pf, mk(UP, 301, 11, 6, prices=low), env)
    assert "SILVERBEES" not in pf.positions
    sells = [d for d in decs(env, 301) if d["action"] == "SELL" and d["symbol"] == "SILVERBEES"]
    assert sells and sells[0]["trigger"] == "Opus guidance" and sells[0]["time"] == "11:06"
    assert "two fresh prices" in sells[0]["reason"] and " × " in sells[0]["reason"] and " = ₹" in sells[0]["reason"]
    run_tick(pf, mk(UP, 301, 15, 10), env)
    assert "SILVERBEES" not in pf.positions                             # and not bought back that day


def test_decisions_show_exit_level_not_a_fake_stop(tmp_path):
    pf, env = held_on_day(tmp_path)
    assert all(p.stop == 0.0 and p.exit_level > 0 for p in pf.positions.values())
    run_tick(pf, mk(UP, 301, 15, 10), env)
    t = [d for d in decs(env, 301) if d["action"] == "TARGET"]
    assert t and all(d["stop"] is None for d in decs(env) if d["action"] in ("BUY", "SELL"))
    assert any("exit_level" in d for d in decs(env, 300) if d["action"] == "BUY")


def test_a_stop_cannot_be_lowered(tmp_path):
    pf, env = held_on_day(tmp_path)
    px = float(UP["SILVERBEES"][301])
    fastlane_file(env, 301, {"SILVERBEES": {"tighten_stop": round(px * 0.99, 2)}})
    run_tick(pf, mk(UP, 301, 11, 0), env)
    first = pf.positions["SILVERBEES"].ai_stop
    fastlane_file(env, 301, {"SILVERBEES": {"tighten_stop": round(px * 0.97, 2)}}, 11, 10)
    run_tick(pf, mk(UP, 301, 11, 11), env)
    assert pf.positions["SILVERBEES"].ai_stop == first


# ------------------------------------------------------------------ 6. hard account stop
def test_account_stop_sells_everything_latches_and_alerts_once(tmp_path):
    pf, env = fresh(tmp_path)
    pf.liquidbees = 0.0
    pf.positions["GOLDBEES"] = Position(qty=1000, avg_cost=100.0, opened="2025-01-01", last_price=100.0, high_water=100.0)
    pf.runtime["strategy_mode"] = "v2_trend"
    pf.runtime["v2"] = {"last_full": T.DAYS[299].date().isoformat()}
    crashed = {s: 1.0 for s in SYMS}
    crashed["GOLDBEES"] = 74.0                                          # 1000 units * 74 = 74,000 < 75,000
    run_tick(pf, mk(UP, 300, 11, 0, prices=crashed), env)
    assert not pf.positions
    sells = [d for d in decs(env, 300) if d["action"] == "SELL"]
    assert sells and sells[0]["trigger"] == "Account stop"
    al = json.loads((env.state_dir / "alerts.json").read_text())
    assert sum(1 for e in al["alerts"].values() if e["category"] == "account_stop") == 1
    run_tick(pf, mk(UP, 300, 11, 1, prices=crashed), env)
    assert sum(1 for e in json.loads((env.state_dir / "alerts.json").read_text())["alerts"].values()
               if e["category"] == "account_stop")  == 1 and sum(1 for d in decs(env, 300) if d["trigger"] == "Account stop" and d["action"] == "BLOCK") == 1
    # the next day, value back over the stop and the trend ON: still no buys (latched)
    pf.liquidbees = 90000.0
    run_tick(pf, mk(UP, 301, 15, 10), env)
    assert not pf.positions
    assert any(d["action"] == "BLOCK" and "account stop" in d["reason"] for d in decs(env, 301))


def test_the_owner_can_clear_the_account_stop_with_a_config_date(tmp_path):
    p = v2_params()
    p["strategy"]["account_stop_clear_after"] = T.DAYS[301].date().isoformat()
    pf, env = fresh(tmp_path, 90000.0, p)
    pf.cooldowns["account_stop_latched"] = {"date": T.DAYS[300].date().isoformat(), "value": 74000.0}
    run_tick(pf, mk(UP, 301, 15, 10), env)
    assert pf.positions                                                 # cleared: the stop no longer blocks
    pf2, env2 = fresh(tmp_path / "x", 90000.0)
    pf2.cooldowns["account_stop_latched"] = {"date": T.DAYS[300].date().isoformat(), "value": 74000.0}
    run_tick(pf2, mk(UP, 301, 15, 10), env2)
    assert not pf2.positions


# ------------------------------------------------------------------ 8. history failure does not block exits
def test_without_daily_history_exits_still_run_and_nothing_is_bought(tmp_path):
    pf, env = held_on_day(tmp_path)
    fastlane_file(env, 301, {"GOLDBEES": {"sell_fraction": 1.0}})
    run_tick(pf, mk(UP, 301, 11, 0, hist=False), env)
    assert "GOLDBEES" not in pf.positions
    run_tick(pf, mk(UP, 301, 15, 10, hist=False), env)
    assert not [d for d in decs(env, 301) if d["action"] == "BUY"]
    assert "Not enough daily history" in json.loads((env.logs_dir / f"checks-{T.DAYS[301].date().isoformat()}.jsonl").read_text().splitlines()[-1])["checks"]["SILVERBEES"]


def test_live_daily_history_keeps_the_last_good_copy_and_alerts_after_repeated_failures(monkeypatch):
    import run_live
    calls = {"n": 0, "ok": True}

    def fake(tickers, period="1y", start=None):
        calls["n"] += 1
        if calls["ok"]:
            return {"close": "GOOD", "high": "GOOD", "low": "GOOD"}
        raise RuntimeError("yahoo down")
    monkeypatch.setattr(run_live, "daily_history", fake)
    reports = []

    class Mon:
        def report(self, *a, **k): reports.append(a)
    live = run_live.Live.__new__(run_live.Live)
    live.p, live.tickers, live.hist, live.hist_at, live.hist_fail, live.mon = v2_params(), ["X"], None, 0.0, 0, Mon()
    assert live._daily()["close"] == "GOOD"
    calls["ok"], live.hist_at = False, 0.0
    for _ in range(3):
        assert live._daily()["close"] == "GOOD"                         # the last good copy is kept (completed bars only)
        live.hist_at = 0.0
    assert reports and reports[0][1] == "data_source_down"
    live.hist = None
    live.hist_at = 0.0
    d = live._daily()                                                   # no copy at all: empty frames, so no buys and exits still run
    assert len(d["close"].columns) == 0


# ------------------------------------------------------------------ 11/12. mode switches and the first v2 day
def v1_params():
    return config.load_params()


def test_v1_to_v2_first_tick_keeps_v1_stops_and_the_floor_becomes_the_account_stop(tmp_path):
    pf, env = fresh(tmp_path, 50000.0)
    pf.positions["GOLDBEES"] = Position(qty=400, avg_cost=100.0, opened="2025-01-01", last_price=100.0, high_water=100.0, stop=98.8)
    pf.floor = 99500.0
    run_tick(pf, mk(UP, 300, 10, 0, prices={s: float(UP[s][300]) for s in SYMS}), env)
    assert pf.floor == 75000.0
    assert not [d for d in decs(env, 300) if d["action"] in ("BUY", "SELL")]               # no trade at 10:00
    assert [d for d in decs(env, 300) if "strategy v2 active" in d["reason"]].__len__() == 1
    assert pf.positions["GOLDBEES"].ai_stop == 98.8                                        # the v1 stop protects it from the first tick
    low = {s: float(UP[s][300]) for s in SYMS}
    low["GOLDBEES"] = 98.0
    run_tick(pf, mk(UP, 300, 10, 1, prices=low), env)
    assert "GOLDBEES" in pf.positions                                                      # D5-S: the second fresh price confirms
    run_tick(pf, mk(UP, 300, 10, 2, prices=low), env)
    assert "GOLDBEES" not in pf.positions


def test_v1_tripwire_no_buys_until_is_honoured_by_v2(tmp_path):
    pf, env = fresh(tmp_path)
    pf.cooldowns["no_buys_until"] = T.DAYS[301].date().isoformat()
    run_tick(pf, mk(UP, 300, 15, 10), env)
    assert not pf.positions
    assert any(d["trigger"] == "Tripwire" for d in decs(env, 300))
    run_tick(pf, mk(UP, 301, 15, 10), env)
    assert pf.positions


def test_v1_to_v2_forgets_an_old_v2_brake_peak(tmp_path):
    pf, env = fresh(tmp_path)
    pf.runtime["v2"] = {"brake": {"peak": 500000.0, "level": 2, "reconfirm": ["GOLDBEES"], "days": 40}}
    run_tick(pf, mk(UP, 300, 15, 10), env)
    assert pf.runtime["v2"]["brake"]["level"] == 0 and pf.runtime["v2"]["brake"]["peak"] < 200000
    assert pf.positions                                                                      # traded normally


def test_v2_to_v1_rederives_the_v1_stops_and_no_tripwire_fires(tmp_path):
    pf, env = fresh(tmp_path)
    run_tick(pf, mk(UP, 300, 15, 10), env)
    assert pf.positions and all(p.stop == 0.0 for p in pf.positions.values())
    pf.cash += 5000.0                                                                       # value comfortably above 99,500
    env1 = T.make_env(tmp_path / "v1", v1_params())
    run_tick(pf, mk(UP, 301, 10, 0), env1)
    p1 = v1_params()
    for sym, pos in pf.positions.items():
        assert pos.stop == pos.avg_cost * (1 - p1["instruments"][sym]["stop"]) and pos.ai_stop == 0.0
    assert not [d for d in T.all_decisions(env1) if d["trigger"] == "Tripwire"]
    assert pf.floor >= 99500.0 and pf.positions
    assert any("v1 score rules" in d["reason"] for d in T.all_decisions(env1))


# ------------------------------------------------------------------ 7. portfolio fast-lane trigger
def test_portfolio_trigger_under_v2_is_a_percent_drop_of_value():
    now = datetime(2026, 10, 14, 11, 0, tzinfo=IST)
    hist = [(now - timedelta(minutes=10), 100000.0)]
    p2, p1 = v2_params(), config.load_params()
    assert fastlane.detect_portfolio_trigger(hist, 98400.0, 75000.0, now, p2)["kind"] == "portfolio"      # -1.6%
    assert fastlane.detect_portfolio_trigger(hist, 99000.0, 75000.0, now, p2) is None                    # -1.0%
    assert fastlane.detect_portfolio_trigger(hist, 99900.0, 99500.0, now, p1) is None                    # v1 rule unchanged: 40% of the cushion
    assert fastlane.detect_portfolio_trigger(hist, 99750.0, 99500.0, now, p1)["kind"] == "portfolio"
