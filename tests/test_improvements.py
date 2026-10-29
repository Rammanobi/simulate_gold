"""Owner-approved improvements (29 Oct 2026): no news pauses under v2, the fast lane only for a price shock on a held symbol,
a missed daily-check buy retried the next morning, the `allocation_stuck` alert, the two plain lines at the top of the email,
and the leaner routine playbooks."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import test_engine_v2 as T
import test_engine_v2_fixes as F
from conftest import d5_params
from simgold import alerts, report
from simgold.broker import Portfolio, Position
from simgold.engine import Market, run_tick
from simgold.market import IST
from test_report import GREEN_DAY, green_root  # noqa: F401  (fixture)
from test_stops_d5s import I, bars, d5_env, last, reasons

REPO = Path(__file__).resolve().parent.parent
SYMS = T.SYMS


def at(idx, hh, mm):
    d = T.DAYS[idx].to_pydatetime()
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


def tick_at(pf, env, prices, idx, hh, mm, hist, headlines=None, series=None, prev_close=None):
    now = at(idx, hh, mm)
    mkt = Market(now=now, prices=prices, price_ts={s: now for s in prices}, scores={}, series=series or {},
                 prev_close=prev_close or {s: p for s, p in prices.items()}, headlines=headlines or [], hist=hist)
    return run_tick(pf, mkt, env)


def decs(env, idx):
    return T.read_decisions(env, idx)


# ------------------------------------------------------------------------------------------------ 1. news pauses
FED = {"urgency": 5, "categories": ["monetary_policy"], "symbols": ["GOLDBEES", "SILVERBEES"], "wake": True}


def news_env(tmp_path, held=False, **strategy):
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS},
                           positions=[("GOLDBEES", 300, 100.0)] if held else None)
    calls = []
    env.match_headline = lambda text: dict(FED)
    env.post = lambda url, headers, body, timeout=10: calls.append(body) or 200
    env.environ = {"ROUTINE_FIRE_URL": "https://example.invalid/fire", "ROUTINE_FIRE_TOKEN": "tok"}
    env.params["strategy"].update(strategy)
    return pf, env, hist, calls


def test_shipped_config_switches_news_off_and_keeps_headline_matching(tmp_path):
    st = d5_params()["strategy"]
    assert st["news_blocks_buys"] is False and st["fastlane_only_held"] is True
    assert st["retry_missed_buys"] is True and st["stuck_alert_points"] == 10


def test_news_headline_no_longer_blocks_or_fires_under_v2(tmp_path):
    pf, env, hist, calls = news_env(tmp_path, held=True)
    px = {s: last(hist, s) for s in SYMS}
    tick_at(pf, env, px, I, 11, 0, hist, headlines=[{"title": "Fed cuts rates in surprise move"}])
    assert not [d for d in decs(env, I) if d["action"] == "BLOCK"]
    assert not pf.cooldowns.get("block_until") and not calls
    assert not pf.runtime.get("news_cooldown")


def test_news_headline_still_blocks_when_the_switch_is_turned_back_on(tmp_path):
    pf, env, hist, calls = news_env(tmp_path, held=True, news_blocks_buys=True)
    px = {s: last(hist, s) for s in SYMS}
    tick_at(pf, env, px, I, 11, 0, hist, headlines=[{"title": "Fed cuts rates in surprise move"}])
    assert {d["trigger"] for d in decs(env, I) if d["action"] == "BLOCK"} == {"News trigger"}
    assert set(pf.cooldowns["block_until"]) == {"GOLDBEES", "SILVERBEES"}
    assert not calls                                   # news never wakes the fast lane under v2, even with the switch on


def test_an_old_news_pause_is_dropped_and_a_price_shock_pause_is_kept(tmp_path):
    pf, env, hist, _ = news_env(tmp_path)
    until = (at(I, 11, 0) + timedelta(hours=2)).isoformat()
    pf.cooldowns["block_until"] = {"GOLDBEES": until, "SILVERBEES": until}
    pf.cooldowns["block_src"] = {"GOLDBEES": "News trigger", "SILVERBEES": "Price shock"}
    px = {s: last(hist, s) for s in SYMS}
    tick_at(pf, env, px, I, 11, 0, hist)
    assert "GOLDBEES" not in pf.cooldowns["block_until"] and "GOLDBEES" not in pf.cooldowns["block_src"]
    assert pf.cooldowns["block_src"]["SILVERBEES"] == "Price shock"


def test_the_floor_buy_at_the_check_is_not_stopped_by_a_headline(tmp_path):
    """28 Oct 2026: a news pause until 16:00 plus FOMC kept the 50% gold floor from being bought."""
    pf, env, hist, _ = news_env(tmp_path)
    px = {s: last(hist, s) for s in SYMS}
    tick_at(pf, env, px, I, 14, 0, hist, headlines=[{"title": "Fed cuts rates in surprise move"}])
    tick_at(pf, env, px, I, 15, 10, hist)
    assert "GOLDBEES" in pf.positions


def shock_series(idx, sym, base, move=0.02):
    now = at(idx, 11, 0)
    s = [(now - timedelta(minutes=10 - k), base * (1 + move * (k / 10) ** 3)) for k in range(11)]
    return s, s[-1][1]


@pytest.mark.parametrize("held,fires", [(True, True), (False, False)])
def test_fast_lane_fires_only_for_a_price_shock_on_a_held_symbol(tmp_path, held, fires):
    pf, env, hist, calls = news_env(tmp_path, held=held)
    px = {s: last(hist, s) for s in SYMS}
    ser, p = shock_series(I, "GOLDBEES", px["GOLDBEES"])
    px["GOLDBEES"] = p
    tick_at(pf, env, px, I, 11, 0, hist, series={"GOLDBEES": ser}, prev_close={s: px[s] for s in SYMS})
    assert any(d["action"] == "BLOCK" and d["trigger"] == "Price shock" for d in decs(env, I))     # the pause stays either way
    assert bool(calls) is fires
    req = json.loads((env.state_dir / "fastlane_requests.jsonl").read_text().splitlines()[0])
    assert req["fired"] is fires
    if not fires:
        assert "held" in req["fire_note"]


def test_fast_lane_held_only_switch_off_restores_the_old_behaviour(tmp_path):
    pf, env, hist, calls = news_env(tmp_path, held=False, fastlane_only_held=False)
    px = {s: last(hist, s) for s in SYMS}
    ser, p = shock_series(I, "GOLDBEES", px["GOLDBEES"])
    px["GOLDBEES"] = p
    tick_at(pf, env, px, I, 11, 0, hist, series={"GOLDBEES": ser}, prev_close={s: px[s] for s in SYMS})
    assert calls


# ------------------------------------------------------------------------------------------------ 2. retry missed buys
def blocked_env(tmp_path, **kw):
    """Gold floor 50% wanted, nothing held, a v1-style tripwire (a temporary blocker that is not safe mode) in force."""
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS}, **kw)
    pf.cooldowns["no_buys_until"] = "2999-01-01"
    return pf, env, hist, {s: last(hist, s) for s in SYMS}


def test_a_blocked_check_buy_is_kept_and_made_the_next_morning(tmp_path):
    pf, env, hist, px = blocked_env(tmp_path)
    tick_at(pf, env, px, I, 15, 10, hist)
    tick_at(pf, env, px, I, 15, 25, hist)
    assert "GOLDBEES" not in pf.positions
    pend = pf.runtime["v2"]["pending_buys"]
    assert pend["GOLDBEES"]["date"] == T.DAYS[I].date().isoformat() and pend["GOLDBEES"]["blocker"] == "Tripwire"
    pf.cooldowns.pop("no_buys_until")                                           # the blocker has expired by the morning
    tick_at(pf, env, px, I + 1, 9, 29, hist)
    assert "GOLDBEES" not in pf.positions                                       # before the retry window
    tick_at(pf, env, px, I + 1, 9, 30, hist)
    assert "GOLDBEES" in pf.positions
    buy = [d for d in decs(env, I + 1) if d["action"] == "BUY"]
    assert buy and "Retry of the buy" in buy[0]["reason"] and buy[0]["time"] == "09:30"
    assert not pf.runtime["v2"].get("pending_buys")


def test_the_retry_respects_todays_blockers_and_stops_at_the_end_of_its_window(tmp_path):
    pf, env, hist, px = blocked_env(tmp_path)
    tick_at(pf, env, px, I, 15, 10, hist)
    tick_at(pf, env, px, I + 1, 9, 30, hist)                                    # still blocked this morning
    assert "GOLDBEES" not in pf.positions and pf.runtime["v2"]["pending_buys"]
    tick_at(pf, env, px, I + 1, 9, 50, hist)
    assert pf.runtime["v2"]["pending_buys"]
    tick_at(pf, env, px, I + 1, 9, 51, hist)                                    # window over: dropped, said so
    assert not pf.runtime["v2"]["pending_buys"]
    assert any(d["action"] == "SKIP" and "could not be retried" in d["reason"] for d in decs(env, I + 1))
    pf.cooldowns.pop("no_buys_until")
    tick_at(pf, env, px, I + 1, 9, 52, hist)
    assert "GOLDBEES" not in pf.positions                                       # no late catch-up outside the window


def test_the_retry_is_buys_only_safe_mode_still_blocks_it(tmp_path):
    pf, env, hist, px = blocked_env(tmp_path)
    tick_at(pf, env, px, I, 15, 10, hist)
    pf.cooldowns.pop("no_buys_until")
    env.state_dir.mkdir(parents=True, exist_ok=True)
    alerts.safe_path(env.state_dir).write_text('{"on": true}')
    tick_at(pf, env, px, I + 1, 9, 35, hist)
    assert "GOLDBEES" not in pf.positions and pf.runtime["v2"]["pending_buys"]


def test_a_retry_is_not_made_when_the_target_has_gone_or_the_pending_is_old(tmp_path):
    gpx = bars("down", T.START["GOLDBEES"]).close[-1]
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS}, cash=50000.0, positions=[("GOLDBEES", int(50000 / gpx), gpx)])
    px = {s: last(hist, s) for s in SYMS}
    day = T.DAYS[I].date().isoformat()
    pf.runtime["v2"]["pending_buys"] = {"GOLDBEES": {"date": day, "target": 0.5, "actual": 0.0, "blocker": "Tripwire"}}
    n = pf.positions["GOLDBEES"].qty
    tick_at(pf, env, px, I + 1, 9, 30, hist)                                    # already at the target: nothing to retry
    assert pf.positions["GOLDBEES"].qty == n and not pf.runtime["v2"]["pending_buys"]
    assert any("is not retried" in d["reason"] for d in decs(env, I + 1))
    pf.runtime["v2"]["pending_buys"] = {"SILVERBEES": {"date": T.DAYS[I - 3].date().isoformat(), "target": 0.2, "actual": 0.0, "blocker": "x"}}
    tick_at(pf, env, px, I + 1, 9, 31, hist)                                    # two trading days old: dropped silently
    assert not pf.runtime["v2"]["pending_buys"] and "SILVERBEES" not in pf.positions


def test_a_check_buy_that_fills_leaves_nothing_pending(tmp_path):
    pf, env, hist = d5_env(tmp_path, {s: "down" for s in SYMS})
    px = {s: last(hist, s) for s in SYMS}
    tick_at(pf, env, px, I, 15, 10, hist)
    assert "GOLDBEES" in pf.positions and not pf.runtime["v2"].get("pending_buys")


def test_retry_switch_off(tmp_path):
    pf, env, hist, px = blocked_env(tmp_path)
    env.params["strategy"]["retry_missed_buys"] = False
    tick_at(pf, env, px, I, 15, 10, hist)
    assert not pf.runtime["v2"].get("pending_buys")


# ------------------------------------------------------------------------------------------------ 2b. the stuck alert
def open_alerts(env):
    return [e for e in alerts.load_state(env.state_dir)["alerts"].values() if e["category"] == "allocation_stuck"]


def test_stuck_alert_after_two_checks_is_medium_never_safe_mode_and_resolves(tmp_path):
    pf, env, hist, px = blocked_env(tmp_path)
    tick_at(pf, env, px, I, 15, 10, hist)
    assert not open_alerts(env)                                                # one check is not "stuck"
    tick_at(pf, env, px, I + 1, 15, 10, hist)
    (e,) = open_alerts(env)
    assert e["severity"] == "medium" and e["status"] == "open"
    assert "Target 50% in metals" in e["detail"] and "holding 0%" in e["detail"] and "Tripwire" in e["detail"]
    assert not alerts.safe_mode_on(env.state_dir)
    inc = [json.loads(x) for f in env.logs_dir.glob("incidents-*.jsonl") for x in f.read_text().splitlines()]
    assert inc and inc[-1]["category"] == "allocation_stuck" and inc[-1]["severity"] == "medium"
    pf.cooldowns.pop("no_buys_until")                                          # unblocked: the next check buys and the alert closes
    tick_at(pf, env, px, I + 2, 15, 10, hist)
    assert "GOLDBEES" in pf.positions
    assert open_alerts(env)[0]["status"] == "resolved"


def test_no_stuck_alert_when_the_gap_is_small_or_not_on_consecutive_checks(tmp_path):
    pf, env, hist, px = blocked_env(tmp_path)
    tick_at(pf, env, px, I, 15, 10, hist)
    tick_at(pf, env, px, I + 2, 15, 10, hist)                                  # a check day was skipped in between
    assert not open_alerts(env)
    pf2, env2, hist2 = d5_env(tmp_path / "b", {s: "down" for s in SYMS})
    px2 = {s: last(hist2, s) for s in SYMS}
    tick_at(pf2, env2, px2, I, 15, 10, hist2)
    tick_at(pf2, env2, px2, I + 1, 15, 10, hist2)
    assert not open_alerts(env2)


def test_allocation_stuck_is_medium_and_does_not_count_toward_safe_mode(tmp_path):
    assert alerts.severity_of("allocation_stuck") == alerts.MEDIUM
    for k in range(7):
        alerts.raise_alert(tmp_path, "engine", f"allocation-stuck-{chr(97 + k)}", "allocation_stuck", "x", now=1_000_000.0 + k)
    assert alerts.evaluate_safe_mode(tmp_path, 1_000_100.0) is False and not alerts.safe_mode_on(tmp_path)
    for k in range(7):                                                         # control: other MEDIUM alerts still trip it
        alerts.raise_alert(tmp_path / "c", "engine", f"step-{chr(97 + k)}", "workflow_failed", "x", now=1_000_000.0 + k)
    assert alerts.evaluate_safe_mode(tmp_path / "c", 1_000_100.0) is True


# ------------------------------------------------------------------------------------------------ 3. the email top
def write_top_root(tmp_path, gold_start=100.0, gold_now=98.0):
    (tmp_path / "logs").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "logs" / "ticks-2026-10-13.csv").write_text("time,value,floor,cash,liquidbees\n15:08,100000.00,99500.00,0.00,100000.00\n")
    rec = {"ts": "2026-10-13 14:43:30", "assets": {"GOLD": {"price": gold_start}, "SILVER": {"price": 207.0}}}
    (tmp_path / "logs" / "decisions-2026-10-13.jsonl").write_text(json.dumps(rec) + "\n")
    (tmp_path / "logs" / "ticks-2026-10-29.csv").write_text(
        "time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER\n"
        f"09:15,98990.00,75000.00,98990.00,0.00,{gold_now + 1:.2f},180.00,410.00\n"
        f"14:17,98980.67,75000.00,98980.67,0.00,{gold_now:.2f},180.00,410.00\n")
    return tmp_path


DAY = {"portfolio": {"close": 98980.67, "floor": 75000.0, "v2": True}}


def test_top_line_one_compares_the_account_with_goldbees_buy_and_hold(tmp_path):
    root = write_top_root(tmp_path)
    t = report.owner_top("2026-10-29", root, DAY)
    assert t["vs_hold"] == "You vs simply holding gold: account −1.0% vs GOLDBEES buy-and-hold −2.0% since 13 Oct start (1.0 pts ahead)"
    assert t["action"] == "Action for you: none"
    t = report.owner_top("2026-10-29", write_top_root(tmp_path / "b", gold_now=100.5), DAY)
    assert "+0.5% since 13 Oct start (1.5 pts behind)" in t["vs_hold"]


def test_top_line_one_without_a_start_price_says_so(tmp_path):
    root = write_top_root(tmp_path)
    (root / "logs" / "decisions-2026-10-13.jsonl").unlink()
    assert "comparison is not available yet" in report.owner_top("2026-10-29", root, DAY)["vs_hold"]


def test_top_line_two_names_the_stuck_alert_and_the_account_stop(tmp_path):
    root = write_top_root(tmp_path)
    alerts.raise_alert(root / "state", "engine", "allocation-stuck", "allocation_stuck",
                       "Target 50% in metals (GOLDBEES 50%), holding 0% at 2 daily checks in a row. Blocked by: Safe mode.")
    act = report.owner_top("2026-10-29", root, DAY)["action"]
    assert act.startswith("Action for you: stuck alert: Target 50% in metals") and "Safe mode" in act
    alerts.resolve(root / "state", alerts.fingerprint("engine", "allocation-stuck"))
    assert report.owner_top("2026-10-29", root, DAY)["action"] == "Action for you: none"
    (root / "state" / "portfolio.json").write_text(json.dumps({"cooldowns": {"account_stop_latched": {"date": "2026-10-29", "value": 74000}}}))
    assert report.owner_top("2026-10-29", root, DAY)["action"].startswith("Action for you: account stop hit")


def test_both_lines_come_first_in_the_text_and_html_email(green_root):  # noqa: F811
    day = report.build_day(GREEN_DAY, green_root)
    assert day["owner_top"]["action"] == "Action for you: none"
    text = report.render_text(day).splitlines()
    assert text[3].startswith("You vs simply holding gold:") and text[4] == "Action for you: none"
    page = report.render_email(day)
    i = page.index("You vs simply holding gold:")
    assert i < page.index("Portfolio value") and "Action for you: none" in page[i:i + 600]
    del day["owner_top"]                                                        # without it (the sample) nothing changes
    assert "You vs simply holding gold" not in report.render_text(day) and "You vs simply holding gold" not in report.render_email(day)


# ------------------------------------------------------------------------------------------------ 4. the playbooks
def playbook(name):
    return (REPO / "routines" / f"{name}.md").read_text(encoding="utf-8")


def test_hourly_is_one_afternoon_check_and_premarket_runs_on_sonnet():
    h, pm, fl = playbook("hourly"), playbook("premarket"), playbook("fastlane")
    assert "14:40" in h and "once a day" in h.lower()
    assert "Sonnet" in pm and "Opus subagent" not in pm
    assert "held" in fl.lower() and "price shock" in fl.lower()
    for text in (h, pm, fl):
        assert "never" in text.lower() and "buy" in text.lower()
