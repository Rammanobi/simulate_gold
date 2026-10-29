"""Adversarial and edge scenarios (synthetic prices, shipped rules). Every tick also runs the Sim invariants (cash >= 0, whole
units, value reconciliation, live payload v:1, money summary parts) so a failure of those shows up in any scenario."""
from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta

import pytest

import evalkit as K
from simgold import alerts, enginekick, market, runguard, trend
from simgold.broker import Portfolio, Position
from simgold.engine import Engine, Env, Market
from simgold.incidents import IncidentMonitor
from simgold.livepush import LivePublisher

pytestmark = pytest.mark.scenario
D0, D1, D2 = "2026-11-03", "2026-11-04", "2026-11-05"        # Tue, Wed, Thu (normal trading days)
G, S, C = K.SYMS


def px_of(hist) -> dict:
    return {s: K.last_close(hist, s) for s in K.SYMS}


def floor_sim(tmp, kinds=None, **kw):
    """Gold floor already bought at the D0 15:10 check (gold/silver/copper trends OFF unless `kinds` says otherwise)."""
    hist = K.synth_hist(kinds or {})
    sim = K.Sim(tmp, hist=hist, **kw)
    px = px_of(hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    sim.tick(K.at(D0, 15, 10), px)
    return sim, hist, px


def silver_held(tmp, qty: int = 100):
    """SILVERBEES held with the trend ON (rising bars); gold/copper OFF. Returns the sim, hist, prices and the frozen exit level."""
    hist = K.synth_hist({S: "up"})
    px = px_of(hist)
    pf = K.floor_pf()
    pf.positions[S] = Position(qty=qty, avg_cost=px[S], opened="2026-11-02", last_price=px[S], high_water=px[S])
    pf.cash -= qty * px[S]
    K.seed_v2(pf, D0)                                       # the D0 check was done, so D1 starts normally
    sim = K.Sim(tmp, hist=hist, pf=pf)
    sim.prev_close = dict(px)
    st = sim.p["strategy"]
    b = hist[S]
    lvl = trend.next_exit_level(b.close, b.high, b.low, st)
    assert lvl and 0 < lvl < px[S]
    return sim, hist, px, lvl


# ================================================================= 1. stops
def test_gap_down_open_below_silver_stop_sells_once_at_the_open(tmp_path):
    sim, hist, px, lvl = silver_held(tmp_path)
    gap = dict(px, **{S: lvl * 0.93})
    sim.run_flat(D1, (9, 15), (15, 30), gap)
    sells = sim.trades("SELL", S)
    assert len(sells) == 1 and sells[0]["trigger"] == "Trend exit" and sells[0]["time"] <= "09:16"       # 2 fresh prices, no waiting for the close
    assert sells[0]["qty"] == 100 and S not in sim.pf.positions
    assert not sim.trades("BUY", S)                          # and not bought back at the 15:10 check the same day


def test_price_flicker_around_the_stop_needs_two_consecutive_prices(tmp_path):
    sim, hist, px, lvl = silver_held(tmp_path)
    t = K.at(D1, 9, 15)
    sim.tick(t, px)
    for k in range(1, 21):                                   # under, over, under, over ...: never two in a row
        p = lvl - 0.3 if k % 2 else lvl + 0.8
        sim.tick(t + timedelta(minutes=k), dict(px, **{S: p}))
    assert S in sim.pf.positions and not sim.trades("SELL", S)
    sim.tick(t + timedelta(minutes=30), dict(px, **{S: lvl - 0.3}))
    assert S in sim.pf.positions
    sim.tick(t + timedelta(minutes=31), dict(px, **{S: lvl - 0.4}))
    assert S not in sim.pf.positions and len(sim.trades("SELL", S)) == 1


def test_a_stale_price_under_the_stop_neither_counts_nor_sells(tmp_path):
    sim, hist, px, lvl = silver_held(tmp_path)
    t = K.at(D1, 10, 0)
    for k in range(30):
        sim.tick(t + timedelta(minutes=k), dict(px, **{S: lvl - 1}), ages={S: 8})
    assert S in sim.pf.positions and not sim.trades("SELL")


# ================================================================= 2. bad data
@pytest.mark.parametrize("factor", [0.80, 1.20, 0.70, 0.40])
def test_one_bad_bar_never_sells_the_gold_floor_or_trips_the_account_stop(tmp_path, factor):
    sim, hist, px = floor_sim(tmp_path)
    qty = sim.pf.positions[G].qty
    assert qty > 0
    t = K.at(D1, 11, 0)
    for k in range(5):
        sim.tick(t + timedelta(minutes=k), px)
    sim.tick(t + timedelta(minutes=5), {s: v * factor for s, v in px.items()})           # one minute of garbage on every symbol
    for k in range(6, 14):
        sim.tick(t + timedelta(minutes=k), px)
    assert sim.pf.positions[G].qty == qty and not sim.trades("SELL")
    assert not [d for d in sim.decisions() if d["trigger"] == "Account stop"]
    assert not sim.pf.cooldowns.get("account_stop_latched")


def test_impossible_prices_are_ignored(tmp_path):
    sim, hist, px = floor_sim(tmp_path)
    t = K.at(D1, 11, 0)
    for k, bad in enumerate([0.0, -5.0, float("nan"), float("inf")]):
        sim.tick(t + timedelta(minutes=k), dict(px, **{G: bad}))
    assert sim.pf.positions[G].qty > 0 and not sim.trades("SELL")


@pytest.mark.xfail(strict=True, reason="REAL BUG (not fixed, needs owner approval): the account stop sells on ONE tick at/under 75,000, so a single "
                   "-20% bad print (inside the 25% data filter) on an ~80k, gold-heavy account latches the permanent stop. See PR body.")
def test_a_bad_bar_on_a_stressed_account_must_not_latch_the_account_stop(tmp_path):
    """An account already down to ~80k and mostly in gold meets ONE -20% bad print (inside the 25% data filter)."""
    hist = K.synth_hist({G: "up"})
    px = px_of(hist)
    pf = K.floor_pf(cash=0.0)
    q = int(79000 / px[G])
    pf.positions[G] = Position(qty=q, avg_cost=px[G], opened="2026-11-02", last_price=px[G], high_water=px[G])
    pf.cash = 80000 - q * px[G]
    K.seed_v2(pf, "2026-11-02")
    sim = K.Sim(tmp_path, hist=hist, pf=pf, expected_interest=-20000.0)
    sim.prev_close = dict(px)
    t = K.at(D1, 11, 0)
    sim.tick(t, px)
    sim.tick(t + timedelta(minutes=1), {s: v * 0.8 for s, v in px.items()})
    sim.tick(t + timedelta(minutes=2), px)
    assert not [d for d in sim.decisions() if d["trigger"] == "Account stop"], "one bad print latched the permanent account stop"
    assert G in sim.pf.positions


# ================================================================= 3. feed outage / missing price
def test_feed_outage_30_minutes_makes_no_trade_on_stale_prices(tmp_path):
    sim, hist, px, lvl = silver_held(tmp_path)
    t = K.at(D1, 10, 0)
    sim.tick(t, px)
    for k in range(1, 31):                                   # the feed is down: no price at all, then old prices
        sim.tick(t + timedelta(minutes=k), {} if k < 15 else dict(px, **{S: lvl - 5}), ages={s: 9 for s in K.SYMS})
    assert not sim.trades("SELL") and not sim.trades("BUY")
    assert S in sim.pf.positions


def test_outage_through_the_whole_check_window_buys_nothing_and_does_not_crash(tmp_path):
    hist = K.synth_hist({})
    sim = K.Sim(tmp_path, hist=hist)
    K.seed_v2(sim.pf, "2026-11-02")
    for m in range(5, 31):
        sim.tick(K.at(D0, 15, m), {})
    assert not sim.pf.positions and not sim.trades("BUY")
    assert sim.pf.cash == 100000.0


def test_one_symbol_without_a_price_does_not_stop_the_others(tmp_path):
    hist = K.synth_hist({S: "up"})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    nos = {k: v for k, v in px.items() if k != S}
    for m in (10, 11):
        sim.tick(K.at(D0, 15, m), nos)
    assert G in sim.pf.positions and S not in sim.pf.positions
    sim.tick(K.at(D0, 15, 12), px)
    assert S in sim.pf.positions
    assert {d["symbol"]: d["time"] for d in sim.trades("BUY")} == {G: "15:10", S: "15:12"}


# ================================================================= 4. calendar
def test_weekend_holiday_and_half_day_calendar():
    assert not market.is_trading_day(date(2026, 11, 7)) and not market.is_trading_day(date(2026, 11, 8))      # Sat, Sun
    assert not market.is_trading_day(date(2026, 11, 10))                                                     # Balipratipada
    assert market.next_trading_day(date(2026, 11, 6)) == date(2026, 11, 9)
    assert market.prev_trading_day(date(2026, 11, 11)) == date(2026, 11, 9)
    go, why = enginekick.decide(K.at("2026-11-07", 10, 0), "")
    assert not go and "not an NSE trading day" in why
    assert not enginekick.decide(K.at("2026-11-10", 10, 0), "")[0]
    assert enginekick.decide(K.at(D0, 10, 0), "")[0]
    assert not enginekick.decide(K.at(D0, 10, 0), "in_progress")[0]        # a run is already active
    assert not enginekick.decide(K.at(D0, 15, 25), "")[0]                  # after the self-dispatch cut-off


def test_friday_to_monday_and_a_holiday_gap_cause_no_catch_up_trades(tmp_path):
    hist = K.synth_hist({})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-05")
    sim.tick(K.at("2026-11-06", 15, 10), px)                                # Friday: floor bought
    held = sim.pf.positions[G].qty
    sim.close_day()
    for day in ("2026-11-09", "2026-11-11"):                                # Monday, then Wednesday after the Tuesday holiday
        for hhmm in ((9, 15), (9, 30), (12, 0), (15, 10), (15, 20)):
            sim.tick(K.at(day, *hhmm), px)
        sim.close_day()
    assert sim.pf.positions[G].qty == held and not sim.trades("SELL")
    assert len(sim.trades("BUY")) == 1


def test_early_close_day_feed_ends_at_1230_and_protective_ticks_buy_nothing(tmp_path):
    hist = K.synth_hist({})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    for hhmm in ((9, 15), (10, 0), (12, 30)):
        sim.tick(K.at(D0, *hhmm), px)
    for m in range(15, 31):                                                 # run_live's protective tick: no prices at all
        sim.tick(K.at(D0, 15, m), {})
    assert not sim.trades("BUY") and not sim.pf.positions


# ================================================================= 5. restart / duplicates
def test_restart_mid_day_keeps_the_stop_count_and_the_stopped_out_flag(tmp_path):
    sim, hist, px, lvl = silver_held(tmp_path)
    t = K.at(D1, 10, 0)
    sim.tick(t, px)
    sim.tick(t + timedelta(minutes=1), dict(px, **{S: lvl - 0.5}))        # first price under the level: count 1
    assert sim.pf.runtime["v2"]["below"][S]["n"] == 1
    sim.restart()
    assert sim.pf.runtime["v2"]["below"][S]["n"] == 1
    sim.tick(t + timedelta(minutes=2), dict(px, **{S: lvl - 0.6}))        # the second one after the restart sells
    assert S not in sim.pf.positions and len(sim.trades("SELL", S)) == 1
    sim.restart()
    assert sim.pf.runtime["v2"]["xstop"][S] == D1
    sim.tick(K.at(D1, 15, 10), px)
    assert not sim.trades("BUY", S)                                       # still not bought back after the restart


def test_restart_keeps_ai_stop_and_pending_retry(tmp_path):
    hist = K.synth_hist({})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    for m in range(10, 26):
        sim.tick(K.at(D0, 15, m), px, ages={s: 10 for s in K.SYMS})        # stale all window: the floor buy is pending
    assert G in sim.pf.runtime["v2"]["pending_buys"]
    sim.restart()
    assert G in sim.pf.runtime["v2"]["pending_buys"]
    sim.close_day()
    sim.tick(K.at(D1, 9, 30), px)
    assert len(sim.trades("BUY", G, D1)) == 1


def test_a_rerun_of_the_same_minute_or_two_sessions_never_double_buy(tmp_path):
    hist = K.synth_hist({S: "up"})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    for m in (10, 10, 11, 11, 12):
        sim.tick(K.at(D0, 15, m), px)
    assert len(sim.trades("BUY", S)) == 1 and len(sim.trades("BUY", G)) <= 1     # silver's target already covers the 50% floor
    # a second engine process started from the saved file after the buys must not buy again either
    sim.restart()
    n = len(sim.trades("BUY"))
    sim.tick(K.at(D0, 15, 13), px)
    assert len(sim.trades("BUY")) == n


def test_the_engine_lock_and_run_guard_stop_a_second_session(tmp_path):
    import run_live
    first = run_live.acquire_lock(tmp_path / ".engine.lock")
    assert first is not None
    assert run_live.acquire_lock(tmp_path / ".engine.lock") is None      # the second engine run on one checkout is refused
    first.close()
    env = {"GITHUB_RUN_ID": "20", "GITHUB_REPOSITORY": "o/r"}

    def fetch(path):
        if path.endswith("/runs/20"):
            return {"workflow_id": 7, "created_at": "2026-11-03T04:00:00Z", "id": 20}
        return {"workflow_runs": [{"id": 10, "created_at": "2026-11-03T03:00:00Z"}, {"id": 30, "created_at": "2026-11-03T05:00:00Z"}]}
    assert runguard.older_engine_run(env, fetch)["id"] == 10                # an older run is in progress: this one exits
    assert runguard.older_engine_run({"GITHUB_RUN_ID": "10", "GITHUB_REPOSITORY": "o/r"},
                                     lambda p: {"workflow_id": 7, "created_at": "2026-11-03T03:00:00Z", "id": 10, "workflow_runs": [
                                         {"id": 20, "created_at": "2026-11-03T04:00:00Z"}]}) is None   # the oldest run goes on
    assert runguard.older_engine_run(env, lambda p: (_ for _ in ()).throw(OSError("api down"))) is None  # fails open


# ================================================================= 6. brakes and account stop override the floor
def pf_with(cash: float, **pos) -> Portfolio:
    pf = Portfolio()
    pf.cash, pf.liquidbees = cash, 0.0
    for s, (qty, px) in pos.items():
        pf.positions[s] = Position(qty=qty, avg_cost=px, opened="2026-11-02", last_price=px, high_water=px)
    return pf


def brake_sim(tmp, value: float, peak: float):
    hist = K.synth_hist({})
    px = px_of(hist)
    q = int(value / px[G] / (1 - 0.0005))
    pf = pf_with(0.0, **{G: (q, px[G])})
    K.seed_v2(pf, "2026-11-02")
    pf.runtime["v2"]["brake"] = {"peak": peak, "level": 0, "reconfirm": [], "days": 0, "rebase_value": None}
    sim = K.Sim(tmp, hist=hist, pf=pf, expected_interest=-100000.0 + value, payload=False)
    sim.prev_close = dict(px)
    return sim, px


def test_minus_15_percent_brake_halves_exposure_down_to_the_floor(tmp_path):
    sim, px = brake_sim(tmp_path, 84000, 100000)
    sim.tick(K.at(D0, 15, 10), px)
    assert sim.pf.runtime["v2"]["brake"]["level"] == 1
    sells = sim.trades("SELL", G)
    assert sells and sells[0]["trigger"] == "Drawdown brake"
    assert 0.40 <= sim.weights()[G] <= 0.56                  # trimmed to the floor weight, not sold out


def test_minus_25_percent_brake_sells_the_floor_too(tmp_path):
    sim, px = brake_sim(tmp_path, 82000, 110000)
    sim.tick(K.at(D0, 15, 10), px)
    assert sim.pf.runtime["v2"]["brake"]["level"] == 2
    s = sim.trades("SELL", G)
    assert len(s) == 1 and s[0]["trigger"] == "Drawdown brake" and G not in sim.pf.positions
    assert not [d for d in sim.decisions() if d["trigger"] == "Account stop"]       # value was still above 75,000


def test_account_stop_sells_everything_at_once_even_the_protected_floor_and_latches(tmp_path):
    sim, px = brake_sim(tmp_path, 74000, 100000)
    sim.tick(K.at(D0, 9, 30), px)                            # not even the daily check: the stop works every minute
    assert not sim.pf.positions
    s = sim.trades("SELL", G)
    assert len(s) == 1 and s[0]["trigger"] == "Account stop"
    assert sim.pf.cooldowns["account_stop_latched"]["date"] == D0
    open_alerts = [e for e in alerts.load_state(sim.env.state_dir)["alerts"].values() if e["category"] == "account_stop"]
    assert open_alerts and open_alerts[0]["severity"].upper() == "HIGH"
    sim.close_day()
    for m in (10, 11, 12):
        sim.tick(K.at(D1, 15, m), px)
    assert not sim.trades("BUY") and not sim.pf.positions   # latched: no floor buy tomorrow either


def test_ai_advice_cannot_sell_the_floor_but_the_brake_still_can(tmp_path):
    sim, hist, px = floor_sim(tmp_path)
    q = sim.pf.positions[G].qty
    K.write_json(sim.env.state_dir / "fastlane.json", {"updated": K.at(D1, 11, 0).isoformat(), G: {"sell_fraction": 1.0, "tighten_stop": px[G] * 0.999}})
    for k in range(1, 6):
        sim.tick(K.at(D1, 11, k), px)
    assert sim.pf.positions[G].qty == q and not sim.trades("SELL")
    refused = [d for d in sim.decisions() if d["trigger"] == "Opus guidance" and "floor" in d["reason"]]
    assert refused


# ================================================================= 7. money can never go wrong
def test_a_buy_never_exceeds_cash_including_costs(tmp_path):
    hist = K.synth_hist({})
    px = px_of(hist)
    for cash in (0.0, 50.0, 97.0, 98.5, 5000.0, 123456.78):
        pf = pf_with(cash)
        env = Env(state_dir=tmp_path / f"s{cash}", logs_dir=tmp_path / f"l{cash}", params=K.shipped_params(), environ={}, match_headline=None)
        now = K.at(D1, 15, 10)
        eng = Engine(pf, Market(now=now, prices=dict(px), price_ts={s: now for s in px}, scores={}, prev_close=dict(px), hist=hist), env)
        eng.housekeeping()
        r = eng._fill(G, "BUY", 10 ** 6, px[G], "eval", "Trend check")
        assert pf.cash >= 0
        if r:
            assert isinstance(r["qty"], int) and r["qty"] * r["fill"] + r["cost"] <= cash + 1e-9
            nxt = r["qty"] + 1                                  # one more unit would not have fitted
            from simgold.broker import trade_costs
            assert nxt * r["fill"] + trade_costs(nxt * r["fill"], "BUY", "etf") > cash
        else:
            assert pf.cash == cash and cash < px[G] * 1.002


def test_full_sell_then_rebuy_cycle_keeps_cash_and_units_sane(tmp_path):
    sim, hist, px = floor_sim(tmp_path)
    for day in (D1, D2):
        sim.close_day()
        for hhmm in ((9, 15), (12, 0), (15, 10), (15, 20)):
            sim.tick(K.at(day, *hhmm), {s: v * (1.01 if day == D1 else 0.99) for s, v in px.items()})
    assert all(isinstance(p.qty, int) for p in sim.pf.positions.values()) and sim.pf.cash >= 0


# ================================================================= 8. retry missed buy, stuck alert
def test_missed_buy_is_retried_once_next_morning(tmp_path):
    hist = K.synth_hist({})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    for m in range(10, 31):
        sim.tick(K.at(D0, 15, m), px, ages={s: 10 for s in K.SYMS} if m <= 26 else None)
    assert not sim.trades("BUY", day=D0) and list(sim.pf.runtime["v2"]["pending_buys"]) == [G]
    sim.close_day()
    for m in range(15, 30):
        sim.tick(K.at(D1, 9, m), px)
    assert not sim.trades("BUY", day=D1)                    # not before 09:30
    sim.tick(K.at(D1, 9, 30), px)
    b = sim.trades("BUY", G, D1)
    assert len(b) == 1 and b[0]["time"] == "09:30" and "Retry of the buy" in b[0]["reason"]
    for m in range(31, 60):
        sim.tick(K.at(D1, 9, m), px)
    sim.tick(K.at(D1, 15, 10), px)
    assert len(sim.trades("BUY", day=D1)) == 1 and not sim.pf.runtime["v2"]["pending_buys"]


def test_missed_buy_retry_expires_after_0950(tmp_path):
    hist = K.synth_hist({})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    for m in range(10, 26):
        sim.tick(K.at(D0, 15, m), px, ages={s: 10 for s in K.SYMS})
    sim.close_day()
    for m in range(15, 52):
        sim.tick(K.at(D1, 9, m), px, ages={s: 10 for s in K.SYMS} if m < 52 else None)
    sim.tick(K.at(D1, 9, 52), px)
    for hhmm in ((10, 30), (12, 0), (14, 0)):
        sim.tick(K.at(D1, *hhmm), px)
    assert not sim.trades("BUY", day=D1)
    assert [d for d in sim.decisions(D1) if "could not be retried" in d["reason"]]
    assert not sim.pf.runtime["v2"].get("pending_buys")
    sim.tick(K.at(D1, 15, 10), px)                          # the next daily check decides afresh
    assert len(sim.trades("BUY", G, D1)) == 1


def test_stuck_alert_after_two_checks_resolves_and_never_turns_on_safe_mode(tmp_path):
    hist = K.synth_hist({})
    px = px_of(hist)
    sim = K.Sim(tmp_path, hist=hist)
    sim.prev_close = dict(px)
    K.seed_v2(sim.pf, "2026-11-02")
    K.write_json(sim.env.state_dir / "events.json", [{"date": D0, "type": "FOMC", "time": "23:30"}, {"date": D1, "type": "FOMC", "time": "23:30"}])

    def stuck():
        return [e for e in alerts.load_state(sim.env.state_dir)["alerts"].values() if e["category"] == "allocation_stuck"]
    for hhmm in ((9, 30), (15, 10), (15, 26)):
        sim.tick(K.at(D0, *hhmm), px)
    assert not sim.pf.positions and not stuck()             # one check is not stuck yet
    sim.close_day()
    for hhmm in ((9, 30), (15, 10), (15, 26)):
        sim.tick(K.at(D1, *hhmm), px)
    assert len(stuck()) == 1 and stuck()[0]["status"] == "open" and stuck()[0]["severity"].upper() == "MEDIUM"
    assert not alerts.safe_mode_on(sim.env.state_dir)
    alerts.evaluate_safe_mode(sim.env.state_dir)
    assert not alerts.safe_mode_on(sim.env.state_dir)
    sim.close_day()
    sim.tick(K.at(D2, 15, 10), px)                          # no FOMC: the floor is bought, the gap closes
    assert sim.pf.positions[G].qty > 0
    assert stuck()[0]["status"] == "resolved"


# ================================================================= 9. news, safe mode, live push
def test_news_never_pauses_buys_unless_the_owner_switch_is_turned_back_on(tmp_path):
    from simgold import triggers
    title = "Fed emergency rate hike shocks markets, gold plunges"
    assert triggers.match_headline(title)["wake"]
    for blocks, expect in ((False, 0), (True, 1)):
        p = K.shipped_params()
        p["strategy"]["news_blocks_buys"] = blocks
        hist = K.synth_hist({})
        px = px_of(hist)
        sim = K.Sim(tmp_path / str(blocks), params=p, hist=hist, match_headline=triggers.match_headline)
        sim.prev_close = dict(px)
        K.seed_v2(sim.pf, "2026-11-02")
        sim.tick(K.at(D0, 14, 0), px, headlines=[{"title": title}])
        sim.tick(K.at(D0, 15, 10), px)
        assert bool([d for d in sim.decisions() if d["trigger"] == "News trigger"]) == bool(expect)
        assert (G in sim.pf.positions) == (not blocks) or blocks      # with news on, the floor top-up is delayed, never lost


@pytest.mark.parametrize("cat", ["watchdog_stale", "stale_guidance", "transient_fetch"])
def test_monitoring_noise_never_enables_safe_mode(tmp_path, cat):
    sd = tmp_path / "state"
    t0 = 1_790_000_000.0
    for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"):    # digits are normalised away: use words
        alerts.raise_alert(sd, "monitor", f"{cat}-{word}", cat, "noise", now=t0)
    assert not alerts.evaluate_safe_mode(sd, t0 + 3600) and not alerts.safe_mode_on(sd)


def test_a_genuine_high_open_for_30_minutes_turns_safe_mode_on_even_among_noise(tmp_path):
    sd = tmp_path / "state"
    t0 = 1_790_000_000.0
    for word in ("alpha", "bravo", "charlie"):
        alerts.raise_alert(sd, "monitor", f"late-{word}", "watchdog_stale", "noise", now=t0)
    alerts.raise_alert(sd, "engine", "tick-exceptions", "tick_exceptions", "3 consecutive tick exceptions", now=t0)
    assert not alerts.evaluate_safe_mode(sd, t0 + 10 * 60)
    assert alerts.evaluate_safe_mode(sd, t0 + 31 * 60) and alerts.safe_mode_on(sd)


def test_many_genuine_incidents_turn_safe_mode_on_and_exits_still_work(tmp_path):
    sim, hist, px, lvl = silver_held(tmp_path)
    t0 = K.at(D1, 9, 0).timestamp()
    for word in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"):      # digits are normalised away, so use words
        alerts.raise_alert(sim.env.state_dir, "engine", f"real-{word}", "data_source_down", f"outage {word}", now=t0)
    assert alerts.evaluate_safe_mode(sim.env.state_dir, t0 + 60)
    sim.tick(K.at(D1, 9, 30), px)
    sim.tick(K.at(D1, 9, 31), dict(px, **{S: lvl - 1}))
    sim.tick(K.at(D1, 9, 32), dict(px, **{S: lvl - 1.1}))
    assert S not in sim.pf.positions                           # the stop sold in safe mode
    sim.tick(K.at(D1, 15, 10), px)
    assert not sim.trades("BUY") and any(d["trigger"] == "Safe mode" for d in sim.decisions())


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("how", ["timeout", "git_error", "no_git"])
def test_livepush_failures_never_raise_and_back_off(tmp_path, how):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if how == "timeout":
            raise subprocess.TimeoutExpired(cmd, 15)
        if how == "no_git":
            raise FileNotFoundError("git")
        return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: unable to access")
    clock = FakeClock()
    pub = LivePublisher(tmp_path, runner=runner, environ={"SIMGOLD_GIT_SYNC": "1", "SIMGOLD_LIVE_PUSH": "1"}, clock=clock)
    for _ in range(5):
        clock.t += 60
        assert pub.publish(b"{}\n", K.at(D1, 10, 0)) is False
    assert pub.fails >= 3 and pub.skip_until > clock.t
    n = len(calls)
    clock.t += 60
    assert pub.publish(b"{}\n", K.at(D1, 10, 0)) is False and len(calls) == n       # backing off: git is not even called


def test_a_failing_live_publish_never_blocks_the_tick_or_the_portfolio_save(tmp_path, monkeypatch):
    import run_live
    st, lg = tmp_path / "state", tmp_path / "logs"
    st.mkdir(), lg.mkdir()
    monkeypatch.setattr(run_live, "STATE", st)
    monkeypatch.setattr(run_live, "LOGS", lg)
    monkeypatch.setattr(run_live, "EQUITY_PATH", st / "equity.csv")
    monkeypatch.setattr(run_live, "TRADES_ARCHIVE", lg / "arch.jsonl")
    live = run_live.Live.__new__(run_live.Live)
    live.p, live.pf, live.series, live.live_prev, live.live_ts = K.shipped_params(), Portfolio(), [], {}, {}
    live.tick_prev, live.tick_prev_day = {}, None

    class Boom:
        def publish(self, *a, **k):
            raise RuntimeError("git exploded")
    live.livepub = Boom()
    now = K.at(D1, 10, 0)
    mkt = Market(now=now, prices={G: 100.0}, price_ts={G: now}, scores={})
    live._publish_live(now, mkt, True, None, market_open=True)               # must return normally
    payload = json.loads((st / "live_now.json").read_text())
    K.validate_live_payload(payload, 100000.0)
    live._publish_live(now, mkt, True, None, market_open=True)
    monkeypatch.setattr(run_live, "STATE", tmp_path / "missing" / "dir")      # even a failing local write is swallowed
    live._publish_live(now, mkt, True, None, market_open=True)


def test_the_market_hours_workflow_still_starts_its_next_session():
    yml = (K.ROOT / ".github" / "workflows" / "market-hours.yml").read_text(encoding="utf-8")
    assert "Start the next session" in yml and "1525" in yml and "actions: write" in yml
    assert "cancel-in-progress: false" in yml
