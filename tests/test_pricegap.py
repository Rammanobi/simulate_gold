"""India price gap (simgold/pricegap.py, the LOW `price_gap` alert, tools/pricegap.py): constants, maths, z-score, alert band,
safe-mode exemption, the once-a-day job and failure injection. Information only: nothing here may trade, block or raise into a tick."""
import json
import statistics
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from simgold import alerts as A
from simgold import pricegap as P
from simgold.market import IST

ROOT = Path(__file__).resolve().parent.parent
D = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 15, 10, tzinfo=IST)          # a Friday
CFG = {**P.DEFAULTS, "enabled": True}


def frame(n=80, etf_premium=lambda i: 6.0, start="2026-08-03"):
    """Synthetic close frame on weekdays: the ETF price is built from the implied price times (1 + premium)."""
    idx = pd.bdate_range(start, periods=n)
    rows = {c: [] for c in ("GOLDBEES.NS", "GC=F", "SILVERBEES.NS", "SI=F", "INR=X")}
    for i, d in enumerate(idx):
        gc, si, fx = 3600.0 + i, 42.0 + 0.01 * i, 92.0 + 0.01 * i
        pm = 1 + etf_premium(i) / 100
        rows["GC=F"].append(gc); rows["SI=F"].append(si); rows["INR=X"].append(fx)
        rows["GOLDBEES.NS"].append(P.implied_unit_price("GOLDBEES", gc, fx, d.date(), CFG) * pm)
        rows["SILVERBEES.NS"].append(P.implied_unit_price("SILVERBEES", si, fx, d.date(), CFG) * pm)
    return pd.DataFrame(rows, index=idx)


# ------------------------------------------------------------------ constants and maths
def test_constants_are_documented_estimates():
    assert P.OZ_G == 31.1034768 and P.FX_TICKER == "INR=X"
    assert P.PRODUCTS["GOLDBEES"]["launch_g"] == 0.01 and P.PRODUCTS["SILVERBEES"]["launch_g"] == 1.0
    assert "ESTIMATE" in P.__doc__ and "0.01 gram" in P.__doc__ and "not verified" in P.__doc__.lower()
    assert P.content_g("GOLDBEES", date(2007, 3, 8)) == pytest.approx(0.01)
    assert P.content_g("SILVERBEES", date(2022, 2, 4)) == pytest.approx(1.0)
    assert 0.0080 < P.content_g("GOLDBEES", D) < 0.0084          # about 0.0082 g per unit in Nov 2026
    assert 0.96 < P.content_g("SILVERBEES", D) < 0.985
    assert P.content_g("GOLDBEES", date(2030, 1, 1)) < P.content_g("GOLDBEES", D)       # the entitlement only shrinks
    assert P.content_g("GOLDBEES", D, {"gold_g_per_unit": 0.0095}) == 0.0095             # a configured figure wins, no decay


def test_world_price_and_implied_unit_price():
    assert P.world_inr_per_g(3515.4, 93.295) == pytest.approx(3515.4 * 93.295 / 31.1034768)
    w = P.world_inr_per_g(3515.4, 93.295)
    assert P.implied_unit_price("GOLDBEES", 3515.4, 93.295, D, {"duty_pct": 6.0}) == pytest.approx(P.content_g("GOLDBEES", D) * w * 1.06)
    assert P.implied_unit_price("GOLDBEES", 3515.4, 93.295, D, {"duty_pct": 6.0}, duty=False) == pytest.approx(P.content_g("GOLDBEES", D) * w)


@pytest.mark.parametrize("sym,fut", [("GOLDBEES", 3515.4), ("SILVERBEES", 41.07)])
def test_premium_maths_round_trip(sym, fut):
    cfg = {"duty_pct": 6.0}
    for pm in (-5.0, 0.0, 6.0, 12.5):
        etf = P.implied_unit_price(sym, fut, 93.3, D, cfg) * (1 + pm / 100)
        assert P.premium_pct(sym, etf, fut, 93.3, D, cfg) == pytest.approx(pm, abs=1e-9)
    # against the bare world price the same ETF reads the duty higher: (1.06 x 1.00) - 1
    etf = P.implied_unit_price(sym, fut, 93.3, D, cfg)
    assert P.premium_pct(sym, etf, fut, 93.3, D, cfg, duty=False) == pytest.approx(6.0, abs=1e-9)
    assert P.premium_pct(sym, etf, fut, 93.3, D, cfg) == pytest.approx(0.0, abs=1e-9)
    assert P.premium_pct(sym, etf, fut, 93.3, D, {"duty_pct": 0.0}) == pytest.approx(6.0, abs=1e-9)      # duty_pct 0 reads the bare world premium


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), None, "x", True])
def test_premium_refuses_bad_inputs(bad):
    with pytest.raises(ValueError):
        P.premium_pct("GOLDBEES", bad, 3500.0, 93.0, D)
    with pytest.raises(ValueError):
        P.premium_pct("GOLDBEES", 100.0, 3500.0, bad, D)


def test_window_stats_median_and_z():
    base = [6.0 + (0.4 if i % 2 else -0.4) for i in range(60)]
    st = P.window_stats(base + [10.0], {**P.DEFAULTS})
    assert st["premium_pct"] == 10.0 and st["n"] == 61
    assert st["median20_pct"] == pytest.approx(statistics.median(base[-19:] + [10.0]))
    assert st["z"] == pytest.approx((10.0 - 6.0) / 0.4, rel=1e-6)           # sd of the prior 60 values is exactly 0.4
    quiet = P.window_stats([6.0] * 60 + [6.4], {**P.DEFAULTS})
    assert quiet["z"] == pytest.approx(0.4 / P.DEFAULTS["sd_floor_pts"])       # a flat history is floored, z does not explode
    assert P.window_stats([5.0] * 5, {**P.DEFAULTS})["median20_pct"] is None and P.window_stats([5.0] * 5, {**P.DEFAULTS})["z"] is None
    assert P.window_stats([], {**P.DEFAULTS})["premium_pct"] is None
    assert P.window_stats([float("nan"), "x", None, 5.0], {**P.DEFAULTS})["n"] == 1


def test_series_skips_bad_rows():
    rows = [{"date": "2026-11-18", "etf": 100.0, "fut": 3600.0, "fx": 93.0}, {"date": "bad", "etf": 1, "fut": 1, "fx": 1},
            {"date": "2026-11-19", "etf": 0, "fut": 3600.0, "fx": 93.0}, {"etf": 1}, {"date": "2026-11-20", "etf": 100.0, "fut": 3600.0, "fx": 93.0}]
    assert [d for d, _ in P.series_from_rows("GOLDBEES", rows)] == ["2026-11-18", "2026-11-20"]


def test_rows_from_frame_drops_weekends_and_forward_filled_copies():
    idx = pd.to_datetime(["2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22", "2026-11-23"])
    f = pd.DataFrame({"GOLDBEES.NS": [100, 101, 101, 101, 101], "GC=F": [3600, 3610, 3610, 3610, 3610], "INR=X": [93, 93.1, 93.1, 93.2, 93.2]}, index=idx)
    assert [r["date"] for r in P.rows_from_frame(f, "GOLDBEES")] == ["2026-11-19", "2026-11-20"]
    assert P.rows_from_frame(f, "SILVERBEES") == []          # a missing column is "no data", not an error


def test_a_holiday_on_either_exchange_drops_the_row():
    idx = pd.to_datetime(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"])
    # NSE closed on the 17th (the ETF is a forward-fill copy) while COMEX moved; COMEX closed on the 19th (the future is a copy) while the ETF moved
    f = pd.DataFrame({"GOLDBEES.NS": [100.0, 100.0, 101.0, 102.0], "GC=F": [3600.0, 3610.0, 3620.0, 3620.0], "INR=X": [93.0, 93.1, 93.2, 93.3]}, index=idx)
    assert [r["date"] for r in P.rows_from_frame(f, "GOLDBEES")] == ["2026-11-16", "2026-11-18"]


# ------------------------------------------------------------------ compute, state file
def test_compute_flags_only_the_symbol_over_the_band():
    # gold has been about 12% over parity for the last 25 sessions (and 6% before), silver stays at 6%
    f = frame(80, lambda i: 6.0 + (0.1 if i % 2 else -0.1))
    gold = f.columns.get_loc("GOLDBEES.NS")
    f.iloc[-25:, gold] *= 1.057
    obj = P.compute(f, CFG, NOW)
    g, s = obj["gaps"]["GOLDBEES"], obj["gaps"]["SILVERBEES"]
    assert g["warn"] and g["premium_pct"] >= 8 and g["median20_pct"] >= 8 and g["z"] is not None and not g["stale"]
    assert not s["warn"] and abs(s["premium_pct"] - 6.0) < 0.3
    assert obj["warn"] == ["GOLDBEES"] and obj["estimate"] is True and obj["history"]["GOLDBEES"]
    assert g["world_premium_pct"] == pytest.approx(((1 + g["premium_pct"] / 100) * 1.06 - 1) * 100, abs=0.02)


def test_one_noisy_print_is_not_a_gap_the_median_must_be_over_the_band_too():
    f = frame(80, lambda i: 6.0 + (0.1 if i % 2 else -0.1))
    f.iloc[-1, f.columns.get_loc("GOLDBEES.NS")] *= 1.09              # a one-day spike: today 15.5%, the 20-day median stays about 6%
    g = P.compute(f, CFG, NOW)["gaps"]["GOLDBEES"]
    assert g["premium_pct"] > 8 and g["median20_pct"] < 8 and g["z"] > 5 and not g["warn"]
    f.iloc[-12:, f.columns.get_loc("GOLDBEES.NS")] *= 1.09              # sustained for 12 of the last 20 sessions: the median is over too
    assert P.compute(f, CFG, NOW)["gaps"]["GOLDBEES"]["warn"]


def test_a_reading_not_from_today_is_stale_and_never_alerts():
    f = frame(80, lambda i: 14.0 + (0.1 if i % 2 else -0.1))
    obj = P.compute(f, CFG, datetime(2026, 11, 23, 15, 10, tzinfo=IST))      # the last row is Friday 20 Nov, "today" is Monday
    assert all(g["stale"] and not g["warn"] for g in obj["gaps"].values()) and obj["warn"] == []
    fresh = P.compute(f, CFG, NOW)
    assert all(not g["stale"] and g["warn"] for g in fresh["gaps"].values())


def test_no_alert_flag_before_min_obs():
    obj = P.compute(frame(6, lambda i: 30.0), CFG, NOW)
    assert obj["gaps"]["GOLDBEES"]["median20_pct"] is None and obj["warn"] == []


def test_compute_none_without_data():
    assert P.compute(pd.DataFrame({"GC=F": [1.0]}, index=pd.to_datetime(["2026-11-20"])), CFG, NOW) is None


def test_write_is_atomic_and_size_capped(tmp_path):
    obj = P.compute(frame(80), CFG, NOW)
    assert P.write(tmp_path, obj)
    assert P.state_path(tmp_path).stat().st_size <= P.MAX_BYTES
    assert json.loads(P.state_path(tmp_path).read_text())["gaps"]["GOLDBEES"]["premium_pct"]
    assert not list(tmp_path.glob(".*.tmp"))
    big = dict(obj, history={"GOLDBEES": [1.23] * 5000})
    assert P.write(tmp_path, big)                                        # too big with the history: written without it
    assert "history" not in json.loads(P.state_path(tmp_path).read_text())
    assert not P.write(tmp_path, dict(obj, note="x" * 20000))            # too big even without it: not written
    assert "note" not in json.loads(P.state_path(tmp_path).read_text())


@pytest.mark.parametrize("junk", ["", "{", "[]", "null", '{"gaps": 3}', '{"date": "x", "gaps": {}}', '{"date": "2026-11-20", "gaps": {"GOLDBEES": {"premium_pct": "NaN"}}}',
                                  '{"date": "2026-11-20", "gaps": {"GOLDBEES": []}}', '{"date": "2026-11-20", "gaps": {"GOLDBEES": {"premium_pct": true}}}'])
def test_load_is_defensive(tmp_path, junk):
    (tmp_path / "pricegap.json").write_text(junk, encoding="utf-8")
    assert P.load(tmp_path) is None
    assert P.load(tmp_path / "missing") is None


def test_load_keeps_good_symbols_and_drops_bad_numbers(tmp_path):
    (tmp_path / "pricegap.json").write_text(json.dumps({"date": "2026-11-20", "warn_pct": 8, "gaps": {
        "GOLDBEES": {"premium_pct": 9.5, "median20_pct": "x", "z": 1.5, "n": 40, "warn": True}, "SILVERBEES": {"premium_pct": None}}}), encoding="utf-8")
    v = P.load(tmp_path)
    assert list(v["gaps"]) == ["GOLDBEES"] and v["gaps"]["GOLDBEES"]["median20_pct"] is None and v["gaps"]["GOLDBEES"]["z"] == 1.5


def test_settings_defaults_and_bad_values():
    assert P.settings({})["enabled"] is False and P.settings({})["warn_pct"] == 8.0
    shipped = P.settings()
    assert shipped["enabled"] is False                                 # the shipped config is dormant
    s = P.settings({"pricegap": {"enabled": "yes", "warn_pct": -3, "duty_pct": "x", "median_window": 0, "history_period": "99y",
                                 "check_time": "late", "gold_g_per_unit": -1, "unknown": 1}})
    assert s["enabled"] is False and s["warn_pct"] == 8.0 and s["duty_pct"] == 6.0 and s["median_window"] == 20
    assert s["history_period"] == "6mo" and s["check_time"] == "15:10" and s["gold_g_per_unit"] is None and "unknown" not in s
    assert P.settings({"pricegap": {"enabled": True, "warn_pct": 5}})["enabled"] is True and P.settings({"pricegap": {"warn_pct": 5}})["warn_pct"] == 5.0
    assert P.settings("not a dict")["enabled"] is False


# ------------------------------------------------------------------ the LOW alert
def test_price_gap_is_low_and_exempt_from_safe_mode():
    assert A.severity_of("price_gap") == A.LOW
    assert "price_gap" in A.SAFE_MODE_EXEMPT


def test_alert_raised_over_band_never_wakes_and_resolves(tmp_path):
    obj = P.compute(frame(80, lambda i: 14.0 + (0.1 if i % 2 else -0.1)), CFG, NOW)
    assert obj["warn"] == ["GOLDBEES", "SILVERBEES"]
    P.sync_alert(tmp_path, obj)
    st = A.load_state(tmp_path)
    e = next(iter(st["alerts"].values()))
    assert e["category"] == "price_gap" and e["severity"] == "low" and e["status"] == "open"
    assert "Information only" in e["detail"] and "GOLDBEES" in e["detail"]
    assert A.fires_today(st, NOW.timestamp()) == 0 and st["last_high_fire"] == 0.0         # never wakes the routine
    assert A.should_email(e, NOW.timestamp(), A.DEFAULTS)[0] == "HOLD"                   # LOW is never emailed
    P.sync_alert(tmp_path, obj)                                                          # a repeat is the same incident
    assert len(A.load_state(tmp_path)["alerts"]) == 1
    P.sync_alert(tmp_path, dict(obj, warn=[]))                                           # back inside the band: resolved
    assert next(iter(A.load_state(tmp_path)["alerts"].values()))["status"] == "resolved"


def test_alert_below_band_is_not_raised(tmp_path):
    obj = P.compute(frame(80, lambda i: 6.0 + (0.1 if i % 2 else -0.1)), CFG, NOW)
    assert obj["warn"] == []
    P.sync_alert(tmp_path, obj)
    assert A.load_state(tmp_path)["alerts"] == {}


def test_band_is_configurable():
    f = frame(80, lambda i: 10.0 + (0.1 if i % 2 else -0.1))
    assert P.compute(f, {**CFG, "warn_pct": 8.0}, NOW)["warn"] == ["GOLDBEES", "SILVERBEES"]
    assert P.compute(f, {**CFG, "warn_pct": 12.0}, NOW)["warn"] == []


def test_price_gap_never_switches_safe_mode_on(tmp_path):
    """Many open price_gap incidents (distinct fingerprints) never count towards safe mode, and never block anything."""
    t0 = datetime(2026, 11, 20, 10, 0, tzinfo=IST).timestamp()
    for i in range(9):
        A.raise_alert(tmp_path, "engine", f"price-gap-{chr(97 + i)}", "price_gap", "gap", now=t0 + i)
    assert len(A.load_state(tmp_path)["alerts"]) == 9
    assert not A.evaluate_safe_mode(tmp_path, t0 + 3600, dict(A.DEFAULTS))
    assert not A.safe_mode_on(tmp_path)
    # ... and a real incident alongside is still counted on its own
    for i in range(6):
        A.raise_alert(tmp_path, "engine", f"real-{chr(97 + i)}", "data_source_down", "down", now=t0 + 100 + i)
    assert A.evaluate_safe_mode(tmp_path, t0 + 3700, dict(A.DEFAULTS))


# ------------------------------------------------------------------ the daily job
def test_dormant_by_default_writes_nothing(tmp_path):
    called = []
    assert P.run_daily(tmp_path, {}, NOW, fetch=lambda cfg: called.append(1)) is None
    assert not called and not (tmp_path / "pricegap.json").exists()
    P.run_daily_bounded(tmp_path, {}, NOW, fetch=lambda cfg: called.append(1))
    assert not called


def _on(**kw):
    return {"pricegap": {"enabled": True, **kw}}


def test_once_per_day_at_15_10_and_never_on_weekends(tmp_path):
    P._attempts.clear()
    n = []
    fetch = lambda cfg: n.append(1) or frame(80)
    early = datetime(2026, 11, 20, 15, 9, tzinfo=IST)
    assert P.run_daily(tmp_path, _on(), early, fetch) is None and not n
    assert P.run_daily(tmp_path, _on(), datetime(2026, 11, 21, 15, 10, tzinfo=IST), fetch) is None and not n          # Saturday
    obj = P.run_daily(tmp_path, _on(), NOW, fetch)
    assert obj and n == [1] and obj["date"] == "2026-11-20" and (tmp_path / "pricegap.json").exists()
    assert P.run_daily(tmp_path, _on(), NOW.replace(minute=11), fetch) is None and n == [1]      # already written today
    nxt = datetime(2026, 11, 23, 15, 10, tzinfo=IST)
    assert P.run_daily(tmp_path, _on(), nxt, fetch) and n == [1, 1]                               # tomorrow runs again


def test_after_the_session_it_does_not_run(tmp_path):
    P._attempts.clear()
    assert P.run_daily(tmp_path, _on(), datetime(2026, 11, 20, 15, 50, tzinfo=IST), lambda c: frame(80)) is None


def test_job_writes_state_and_alert_end_to_end(tmp_path):
    P._attempts.clear()
    obj = P.run_daily(tmp_path, _on(), NOW, lambda c: frame(80, lambda i: 14.0 + (0.1 if i % 2 else -0.1)))
    assert obj["warn"] and P.load(tmp_path)["gaps"]["GOLDBEES"]["warn"]
    assert [e["category"] for e in A.load_state(tmp_path)["alerts"].values()] == ["price_gap"]


def test_failures_are_swallowed(tmp_path, caplog):
    P._attempts.clear()
    def boom(cfg):
        raise RuntimeError("yahoo down")
    assert P.run_daily(tmp_path, _on(), NOW, boom) is None
    assert not (tmp_path / "pricegap.json").exists()
    P._attempts.clear()
    assert P.run_daily(tmp_path, _on(), NOW, lambda c: pd.DataFrame()) is None          # empty download
    P._attempts.clear()
    assert P.run_daily(tmp_path, _on(), NOW, lambda c: "not a frame") is None
    P._attempts.clear()
    assert P.run_daily(tmp_path, {"pricegap": "garbage"}, NOW, boom) is None
    P._attempts.clear()
    (tmp_path / "pricegap.json").write_text("{corrupt", encoding="utf-8")                  # a corrupt old file is simply replaced
    assert P.run_daily(tmp_path, _on(), NOW, lambda c: frame(80))
    P._attempts.clear()
    assert P.run_daily(tmp_path / "x" / "y", _on(), datetime(2026, 11, 23, 15, 10, tzinfo=IST), lambda c: frame(80))     # the folder is created


def test_alert_failure_does_not_lose_the_state_file(tmp_path, monkeypatch):
    P._attempts.clear()
    monkeypatch.setattr(P, "sync_alert", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert P.run_daily(tmp_path, _on(), NOW, lambda c: frame(80, lambda i: 14.0))
    assert P.load(tmp_path) is not None


def test_three_attempts_a_day_then_stop(tmp_path):
    P._attempts.clear()
    n = []
    def boom(cfg):
        n.append(1)
        raise RuntimeError("down")
    for _ in range(6):
        P.run_daily(tmp_path, _on(), NOW, boom)
    assert len(n) == 3


def test_bounded_runner_never_stalls_the_tick(tmp_path):
    P._attempts.clear()
    t0 = time.time()
    P.run_daily_bounded(tmp_path, _on(), NOW, fetch=lambda c: time.sleep(5), timeout=0.3)
    assert time.time() - t0 < 2
    P.run_daily_bounded(tmp_path, {"pricegap": 7}, NOW)           # garbage config: no exception


def test_no_second_worker_while_a_timed_out_one_is_still_running(tmp_path):
    P._attempts.clear()
    started = []
    gate = threading.Event()
    def slow(cfg):
        started.append(1)
        gate.wait(5)
        return frame(80)
    P._worker = None
    P.run_daily_bounded(tmp_path, _on(), NOW, fetch=slow, timeout=0.2)           # times out, the worker keeps running
    assert P._worker is not None and P._worker.is_alive() and started == [1]
    P.run_daily_bounded(tmp_path, _on(), NOW, fetch=slow, timeout=0.2)           # returns at once: no second thread
    assert started == [1]
    gate.set()
    P._worker.join(5)
    assert not P._worker.is_alive()


def test_default_timeout_is_15_seconds():
    import inspect
    assert inspect.signature(P.run_daily_bounded).parameters["timeout"].default == 15.0


def test_a_failing_price_gap_hook_never_fails_the_tick(monkeypatch, tmp_path):
    """Behavioural: Live.tick with stub bars completes (decisions made, state saved, live published) even when the price-gap call raises."""
    import run_live
    idx = pd.DatetimeIndex([datetime(2026, 11, 20, 15, 10, tzinfo=IST)])
    bars = {"GOLDBEES.NS": pd.DataFrame({"close": [100.0]}, index=idx)}
    calls = []

    class Mon:
        def mark_fresh(self): calls.append("fresh")

    class News:
        last_new = []
        def refresh(self, **k): pass

    class PF:
        floor, positions = 75000.0, {}

    live = run_live.Live.__new__(run_live.Live)
    live.force, live.p, live.mon, live.news, live.pf, live.env, live.tickers = True, {"engine": {"stale_feed_skip_min": 15}}, Mon(), News(), PF(), object(), ["GOLDBEES.NS"]
    live.last_tick_day = None
    monkeypatch.setattr(run_live, "now_ist", lambda: datetime(2026, 11, 20, 15, 10, tzinfo=IST))
    monkeypatch.setattr(run_live, "intraday_bars", lambda tickers: bars)
    monkeypatch.setattr(run_live, "build_market", lambda *a, **k: "MKT")
    monkeypatch.setattr(run_live, "run_tick", lambda pf, mkt, env: calls.append("tick") or [])
    monkeypatch.setattr(live, "_daily", lambda: {}, raising=False)
    monkeypatch.setattr(live, "_save_pf", lambda: calls.append("saved"), raising=False)
    monkeypatch.setattr(live, "_backup_state", lambda: None, raising=False)
    monkeypatch.setattr(live, "_publish_live", lambda *a, **k: calls.append("published"), raising=False)
    monkeypatch.setattr(live, "_value", lambda: 100000.0, raising=False)
    monkeypatch.setattr(run_live.pricegap, "run_daily_bounded", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("price gap exploded")))
    live.tick()                                                                    # must not raise
    assert calls == ["fresh", "tick", "saved", "published"] and live.last_tick_day == date(2026, 11, 20)


def test_the_hook_runs_after_the_decisions_and_publish(monkeypatch):
    import run_live
    idx = pd.DatetimeIndex([datetime(2026, 11, 20, 15, 10, tzinfo=IST)])
    order = []

    class Mon:
        def mark_fresh(self): pass

    class News:
        last_new = []
        def refresh(self, **k): pass

    class PF:
        floor, positions = 75000.0, {}

    live = run_live.Live.__new__(run_live.Live)
    live.force, live.p, live.mon, live.news, live.pf, live.env, live.tickers = True, {"engine": {"stale_feed_skip_min": 15}}, Mon(), News(), PF(), object(), ["X"]
    monkeypatch.setattr(run_live, "now_ist", lambda: datetime(2026, 11, 20, 15, 10, tzinfo=IST))
    monkeypatch.setattr(run_live, "intraday_bars", lambda tickers: {"X": pd.DataFrame({"close": [1.0]}, index=idx)})
    monkeypatch.setattr(run_live, "build_market", lambda *a, **k: "MKT")
    monkeypatch.setattr(run_live, "run_tick", lambda pf, mkt, env: order.append("tick") or [])
    for n in ("_daily", "_save_pf", "_backup_state"):
        monkeypatch.setattr(live, n, lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(live, "_publish_live", lambda *a, **k: order.append("published"), raising=False)
    monkeypatch.setattr(live, "_value", lambda: 1.0, raising=False)
    monkeypatch.setattr(run_live.pricegap, "run_daily_bounded", lambda *a, **k: order.append("pricegap"))
    live.tick()
    assert order == ["tick", "published", "pricegap"]


# ------------------------------------------------------------------ the tool
def test_tool_show(tmp_path):
    (tmp_path / "state").mkdir()
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "tools" / "pricegap.py"), "show", "--root", str(tmp_path), *a], capture_output=True, text=True, timeout=60)
    r = run()
    assert r.returncode == 0 and "dormant" in r.stdout
    P.write(tmp_path / "state", P.compute(frame(80, lambda i: 14.0), CFG, NOW))
    before = (tmp_path / "state" / "pricegap.json").read_bytes()
    r = run()
    assert r.returncode == 0 and "GOLDBEES" in r.stdout and "ABOVE BAND" in r.stdout and "ESTIMATE" in r.stdout
    assert json.loads(run("--json").stdout)["gaps"]["SILVERBEES"]["premium_pct"]
    (tmp_path / "state" / "pricegap.json").write_text("[1]", encoding="utf-8")
    assert run().returncode == 1
    (tmp_path / "state" / "pricegap.json").write_bytes(before)
    assert (tmp_path / "state" / "pricegap.json").read_bytes() == before            # read-only: show never writes
