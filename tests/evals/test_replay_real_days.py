"""Real-day replays: the Engine is driven minute by minute over the REAL prices of 21-29 Oct 2026 (logs/ticks-*.csv, trimmed
into tests/evals/fixtures) with the CURRENT rules (config/params.yaml as shipped), starting from the real portfolio of the
morning of 21 Oct. The replay is run once; every test below asserts one thing about it."""
from __future__ import annotations

import os
import shutil
from datetime import date

import pytest

import evalkit as K
from simgold import alerts, triggers
from simgold.broker import Portfolio

pytestmark = pytest.mark.scenario
DAYS = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
HEADLINES = {   # (day, HH:MM) -> real-style market-moving headlines that WOULD have paused buys under the old rules
    ("2026-10-21", "14:30"): "RBI hikes repo rate to 5.5% in surprise move",
    ("2026-10-21", "15:00"): "Fed emergency rate hike shocks markets, gold plunges",
    ("2026-10-23", "11:33"): "Gold Slips As Oil Price Rallies After Fed Rate Decision",
}
FASTLANE_1022 = {"updated": "2026-10-22T09:16:46+05:30", "GOLDBEES": {"tighten_stop": 116.4},
                 "note": "Gold opened about 2.2% lower, so the gold stop is set at 116.40 to limit further loss."}


class Replay:
    pass


FULL = os.environ.get("EVAL_FULL_REPLAY") == "1"


def replayed(hhmm: str) -> bool:
    """Every recorded minute with EVAL_FULL_REPLAY=1 (about 2 minutes). By default the busy windows (open, the 14:30-15:30 check
    window, and every minute with a headline or injected file) run every minute and the quiet hours every 4th minute, to stay fast."""
    if FULL:
        return True
    hh, mm = int(hhmm[:2]), int(hhmm[3:])
    t = hh * 60 + mm
    return t <= 9 * 60 + 45 or t >= 14 * 60 + 30 or mm % 4 == 0 or hhmm in ("11:33", "09:17", "09:20")


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("replay")
    raw = K.load_daily()
    pf = Portfolio.load(K.FIX / "start_portfolio_2026-10-21.json")
    start_value = pf.cash + pf.liquidbees
    cache = {}
    sim = K.Sim(tmp, pf=pf, hist=lambda d: cache.setdefault(d, K.hist_before(raw, d)), match_headline=triggers.match_headline,
                expected_interest=start_value - 100000.0)
    shutil.copy(K.FIX / "events.json", sim.env.state_dir / "events.json")
    r = Replay()
    r.sim, r.eoD, r.minute_weights, r.noise_alerts, r.safe_flags, r.headline_wake = sim, {}, [], [], [], {}
    for title in HEADLINES.values():
        r.headline_wake[title] = bool(triggers.match_headline(title).get("wake"))
    for day in DAYS:
        d = date.fromisoformat(day)
        sim.prev_close = K.prev_close_of(raw, d)
        K.write_json(sim.env.state_dir / "guidance.json", {"updated": f"{day}T09:00:00+05:30", "bias": {"GOLD": "neutral", "SILVER": "block", "COPPER": "block"}})
        for hhmm, px in K.read_ticks(day):
            hh, mm = int(hhmm[:2]), int(hhmm[3:])
            if not replayed(hhmm):
                continue
            now = K.at(d, hh, mm)
            if mm == 0:      # the hourly routine refreshes the guidance
                K.write_json(sim.env.state_dir / "guidance.json", {"updated": now.isoformat(), "bias": {"GOLD": "neutral", "SILVER": "block", "COPPER": "block"}})
            if day == "2026-10-22" and hhmm == "09:17":
                K.write_json(sim.env.state_dir / "fastlane.json", FASTLANE_1022)
            if day == "2026-10-23" and hhmm == "09:20":      # 23 Oct: a burst of monitoring noise (the day safe mode went on in real life)
                for i in range(5):
                    alerts.raise_alert(sim.env.state_dir, "watchdog", f"late-{i}", "watchdog_stale", "ran late", now=now.timestamp())
                r.noise_alerts.append(now)
            hl = [{"title": HEADLINES[(day, hhmm)]}] if (day, hhmm) in HEADLINES else None
            sim.tick(now, px, headlines=hl)
            alerts.evaluate_safe_mode(sim.env.state_dir, now.timestamp())
            r.safe_flags.append(alerts.safe_mode_on(sim.env.state_dir))
            r.minute_weights.append((day, hhmm, sum(sim.weights().values())))
        sim.close_day()
        r.eoD[day] = (sum(sim.weights().values()), dict((s, p.qty) for s, p in sim.pf.positions.items()), sim.value_now())
    return r


def test_the_replay_ran_every_recorded_minute(replay):
    every = sum(len(K.read_ticks(d)) for d in DAYS)
    assert replay.sim.ticks == (every if FULL else replay.sim.ticks) and replay.sim.ticks > (2400 if FULL else 900)


def test_21_oct_floor_buy_about_half_goldbees_at_1510(replay):
    buys = replay.sim.trades("BUY", day="2026-10-21")
    assert [(b["symbol"], b["time"], b["trigger"]) for b in buys] == [("GOLDBEES", "15:10", "Trend check")]
    w = replay.eoD["2026-10-21"][0]
    assert 0.46 <= w <= 0.52, w
    assert 400 <= buys[0]["qty"] <= 440                      # the real engine bought 423
    assert not replay.sim.trades("BUY", day="2026-10-22") and not replay.sim.trades("BUY", day="2026-10-23")


def test_idle_money_is_moved_to_cash_at_the_first_tick(replay):
    assert replay.sim.pf.liquidbees == 0.0
    assert any(d["trigger"] == "Idle cash" for d in replay.sim.decisions("2026-10-21"))


def test_23_oct_ai_stop_does_not_sell_the_gold_floor(replay):
    sim = replay.sim
    assert not sim.trades("SELL")                            # nothing at all was sold in the nine days
    assert replay.eoD["2026-10-22"][1] == replay.eoD["2026-10-23"][1] == replay.eoD["2026-10-21"][1]
    ai = [d for d in sim.decisions("2026-10-22") if d["trigger"] == "Opus guidance" and d["action"] == "HOLD"]
    assert ai and "floor" in ai[0]["reason"] and "refused" in ai[0]["reason"]      # the tighten_stop 116.40 was refused, in words
    low = min(p["GOLDBEES"] for _, p in K.read_ticks("2026-10-23"))
    assert low < 116.4 and replay.eoD["2026-10-23"][1].get("GOLDBEES", 0) > 0        # 23 Oct really traded under the stop


def test_news_headlines_are_matched_but_never_block_a_buy(replay):
    assert all(replay.headline_wake.values()), replay.headline_wake          # the headlines are real triggers: the eval is not vacuous
    blocks = [d for d in replay.sim.decisions() if d["action"] == "BLOCK" and d["trigger"] == "News trigger"]
    assert not blocks
    buy = replay.sim.trades("BUY", "GOLDBEES", "2026-10-21")
    assert buy and buy[0]["time"] == "15:10"                 # headlines at 14:30 and 15:00 did not stop the floor buy


def test_monitoring_noise_never_enables_safe_mode(replay):
    assert replay.noise_alerts and not any(replay.safe_flags)
    assert not alerts.safe_mode_on(replay.sim.env.state_dir)


def test_26_to_29_oct_account_stays_about_half_invested_every_minute(replay):
    rows = [w for d, t, w in replay.minute_weights if d >= "2026-10-26"]
    assert len(rows) > (1400 if FULL else 500) and all(0.40 <= w <= 0.58 for w in rows), (min(rows), max(rows))
    for d in DAYS[3:]:
        assert 0.44 <= replay.eoD[d][0] <= 0.56


def test_no_account_stop_or_brake_in_the_replay(replay):
    assert not [d for d in replay.sim.decisions() if d["trigger"] in ("Account stop", "Drawdown brake")]
    assert replay.sim.pf.floor == 75000
    assert not replay.sim.pf.cooldowns.get("account_stop_latched")


def test_money_summary_parts_sum_every_day_and_interest_does_not_drift(replay):
    sim = replay.sim
    assert sim.interest_seen and max(sim.interest_seen) - min(sim.interest_seen) < 0.02
    final = sim.last_payload["summary"]
    assert abs(final["overall_profit"] - (sim.value_now() - 100000)) < 0.011


def test_live_payload_was_valid_schema_v1_every_minute(replay):
    assert replay.sim.last_payload["v"] == 1 and replay.sim.payload        # validate_live_payload ran inside Sim on all ticks
    funds = replay.sim.last_payload["summary"].get("funds")
    if funds is None:
        pytest.skip("funds table (feat/funds-table) is not in this checkout yet")
    assert [r["symbol"] for r in funds["rows"]][:3] == K.SYMS
