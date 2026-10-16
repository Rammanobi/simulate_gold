"""Strategy v2 pure rules (simgold/trend.py): trend state, weights, rebalance threshold, RSI(2) dip adds, drawdown brake,
bias effects. The engine and the backtest use the same functions."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from conftest import v2_params
from simgold import trend

ST = v2_params()["strategy"]


def lin(a, b, n):
    return list(np.linspace(a, b, n))


# ------------------------------------------------------------------ mode switch
def test_is_v2_reads_the_mode():
    assert trend.is_v2(v2_params())
    p = v2_params()
    p["strategy"]["mode"] = "v1_scores"
    assert not trend.is_v2(p)
    assert not trend.is_v2({}) and not trend.is_v2(None) and not trend.is_v2({"strategy": {}})


# ------------------------------------------------------------------ GOLDBEES: SMA200 with hysteresis
def test_sma_state_on_above_off_below():
    up = lin(100, 140, 260)
    df = trend.frame(up, up, up, "sma_trend", ST)
    assert df["on"].iloc[-1] == 1.0
    assert df["on"].iloc[:199].eq(0).all()                                  # no SMA200 yet: OFF
    down = up + lin(140, 100, 120)
    df = trend.frame(down, down, down, "sma_trend", ST)
    assert df["on"].iloc[-1] == 0.0 and df["close"].iloc[-1] < df["sma"].iloc[-1]


def test_sma_hysteresis_band_keeps_the_previous_state():
    base = [100.0] * 260                                                    # SMA200 = 100
    for last, expect in ((100.4, 1.0), (99.6, 1.0), (99.4, 0.0)):           # ON series: inside the 0.5% band stays ON
        c = base[:-1] + [101.0] + [last]
        df = trend.frame(c, c, c, "sma_trend", ST)
        assert df["on"].iloc[-2] == 1.0
        assert df["on"].iloc[-1] == expect, last
    c = base + [100.4]                                                      # OFF series: +0.4% is not enough to turn ON
    assert trend.frame(c, c, c, "sma_trend", ST)["on"].iloc[-1] == 0.0
    c = base + [100.6]
    assert trend.frame(c, c, c, "sma_trend", ST)["on"].iloc[-1] == 1.0


# ------------------------------------------------------------------ SILVERBEES / HINDCOPPER: Donchian 55 with ATR trail
def donchian_series():
    flat = [100.0 + (i % 3) * 0.5 for i in range(70)]                       # range 100..101
    rise = lin(101.5, 130, 25)                                              # breakout above the 55-day high
    return flat + rise


def test_donchian_enters_on_breakout_above_prior_55_day_high():
    c = donchian_series()
    h = [x * 1.003 for x in c]
    l = [x * 0.997 for x in c]
    df = trend.frame(c, h, l, "donchian", ST)
    assert df["on"].iloc[69] == 0.0
    first_on = int(np.argmax(df["on"].values))
    assert first_on == 70 and df["close"].iloc[first_on] > df["hh"].iloc[first_on]
    assert df["on"].iloc[-1] == 1.0
    assert df["peak"].iloc[-1] == pytest.approx(max(c[70:]))


def test_donchian_exits_below_peak_minus_3_atr_or_20_day_low():
    c = donchian_series() + [128.0, 125.0, 118.0]
    h = [x * 1.003 for x in c]
    l = [x * 0.997 for x in c]
    df = trend.frame(c, h, l, "donchian", ST)
    on = df["on"].values
    assert on[94] == 1.0
    exit_i = next(i for i in range(95, len(c)) if on[i] == 0.0)
    row = df.iloc[exit_i]
    assert row["close"] < max(row["peak"] - 3 * row["atr"], row["ll"])      # exactly the research rule
    prev = df.iloc[exit_i - 1]
    assert prev["close"] >= max(prev["peak"] - 3 * prev["atr"], prev["ll"])


def test_donchian_does_not_re_enter_without_a_new_high():
    c = donchian_series() + [128.0, 118.0, 120.0, 121.0, 122.0]
    df = trend.frame(c, c, c, "donchian", ST)
    assert df["on"].iloc[-1] == 0.0


# ------------------------------------------------------------------ compute / describe
def bars_from(c):
    return trend.clean_bars(list(c), [x * 1.003 for x in c], [x * 0.997 for x in c])


def test_compute_uses_the_live_price_as_the_provisional_close():
    c = lin(100, 120, 250)
    on = trend.compute("GOLDBEES", "sma_trend", bars_from(c), 122.0, ST)
    off = trend.compute("GOLDBEES", "sma_trend", bars_from(c), 80.0, ST)
    assert on.on and not off.on and off.was_on
    state, detail = trend.describe(on, ST)
    assert state == "ON" and detail.startswith("close 122.00 > SMA200 ")
    state, detail = trend.describe(off, ST)
    assert state == "OFF" and "close 80.00 < SMA200 " in detail


def test_compute_needs_enough_history_and_a_good_price():
    assert trend.compute("GOLDBEES", "sma_trend", bars_from(lin(100, 110, 150)), 111.0, ST) is None
    assert trend.compute("GOLDBEES", "sma_trend", bars_from(lin(100, 110, 250)), float("nan"), ST) is None
    assert trend.compute("GOLDBEES", "sma_trend", bars_from(lin(100, 110, 250)), -1.0, ST) is None


def test_describe_donchian_texts():
    c = donchian_series()
    i = trend.compute("SILVERBEES", "donchian", bars_from(c[:-1]), c[-1], ST)
    state, detail = trend.describe(i, ST)
    assert state == "ON" and "55-day high" in detail or "exit level" in detail
    c2 = [100.0 + (k % 3) * 0.5 for k in range(80)]
    i = trend.compute("SILVERBEES", "donchian", bars_from(c2[:-1]), c2[-1], ST)
    assert trend.describe(i, ST)[0] == "OFF" and "not above the 55-day high" in trend.describe(i, ST)[1]


def test_clean_bars_drops_bad_closes_and_clips_highs_lows():
    b = trend.clean_bars([10, float("nan"), 12, -1, 0], [9, 5, 13, 1, 1], [11, 5, 11, 1, 1])
    assert b.close == [10.0, 12.0]
    assert b.high == [10.0, 13.0]                                           # a high under the close is lifted to the close
    assert b.low == [10.0, 11.0]                                            # a low over the close is pulled down to it


# ------------------------------------------------------------------ volatility, RSI
def test_sigma_is_annualised_close_to_close_volatility():
    rng = np.random.default_rng(0)
    c = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 400)))
    s = trend.annual_vol(c, 60, "ewma").iloc[-1]
    assert 0.12 < s < 0.21                                                  # 1% a day is about 16% a year
    simple = trend.annual_vol(c, 60, "simple").iloc[-1]
    assert simple == pytest.approx(c.pct_change().tail(60).std() * math.sqrt(252))


def test_rsi2_dips_after_two_down_days_and_is_high_after_two_up_days():
    c = pd.Series([100, 101, 102, 103, 104.0])
    assert trend.rsi(c, 2).iloc[-1] > 90
    c = pd.Series([100, 101, 102, 103, 101, 98, 95.0])             # three down days in an uptrend: a real dip
    assert trend.rsi(c, 2).iloc[-1] < 5
    assert trend.rsi(pd.Series([100.0, 100, 100]), 2).iloc[-1] == 50
    c = pd.Series([100.0, 90.0, 80.0, 70.0, 60.0])
    assert trend.rsi(c, 2).iloc[-1] < 5


# ------------------------------------------------------------------ weights
def test_raw_weight_is_vol_target_capped():
    assert trend.raw_weight(True, 0.161, 1.0, 0.14) == pytest.approx(0.14 / 0.161)
    assert trend.raw_weight(True, 0.08, 1.0, 0.14) == 1.0                   # cap binds
    assert trend.raw_weight(True, 0.20, 0.35, 0.14) == 0.35                 # 0.14/0.20 = 0.7 > cap 0.35
    assert trend.raw_weight(False, 0.10, 1.0, 0.14) == 0.0
    for bad in (None, 0.0, -1.0, float("nan"), float("inf")):
        assert trend.raw_weight(True, bad, 1.0, 0.14) == 0.0


def test_target_weights_caps_and_vol_target():
    w, scale = trend.target_weights({"GOLDBEES": (True, 0.161), "SILVERBEES": (False, 0.4), "HINDCOPPER": (False, 0.4)}, ST)
    assert scale == 1.0
    assert w["GOLDBEES"] == pytest.approx(0.14 / 0.161) and w["SILVERBEES"] == 0.0 and w["HINDCOPPER"] == 0.0
    w, _ = trend.target_weights({"GOLDBEES": (True, 0.05), "SILVERBEES": (True, 0.05), "HINDCOPPER": (True, 0.05)}, ST)
    assert (w["GOLDBEES"], w["SILVERBEES"], w["HINDCOPPER"]) == pytest.approx((1.0 / 2.05, 0.7 / 2.05, 0.35 / 2.05))


def test_target_weights_scale_down_proportionally_never_lever():
    infos = {"GOLDBEES": (True, 0.14), "SILVERBEES": (True, 0.30), "HINDCOPPER": (True, 0.40)}   # 1.0 + 0.467 + 0.35
    raw = [1.0, 0.14 / 0.30, 0.35]
    w, scale = trend.target_weights(infos, ST)
    assert sum(w.values()) == pytest.approx(1.0) and scale == pytest.approx(1 / sum(raw))
    assert w["SILVERBEES"] / w["GOLDBEES"] == pytest.approx(raw[1] / raw[0])   # proportional
    w2, _ = trend.target_weights(infos, ST, brake_mult=0.5)
    assert sum(w2.values()) == pytest.approx(0.5)                              # the brake multiplies after the scaling
    w3, _ = trend.target_weights(infos, ST, ineligible=("GOLDBEES",))
    assert w3["GOLDBEES"] == 0.0 and sum(w3.values()) == pytest.approx(min(1.0, raw[1] + raw[2]))


# ------------------------------------------------------------------ decide: rebalance threshold, RSI(2), bias
def D(**kw):
    base = dict(on=True, target=0.5, cur=0.0, flat=True, rsi_val=50.0, bias="neutral", st=ST)
    base.update(kw)
    return trend.decide(**base)


def test_entry_when_flat_and_not_overbought():
    d = D()
    assert (d.action, d.kind, d.to_w) == ("BUY", "entry", 0.5)
    d = D(rsi_val=95.0)
    assert (d.action, d.kind) == ("SKIP", "wait") and "overbought" in d.why
    assert D(rsi_val=90.0).action == "BUY"                                  # at the limit is still allowed
    assert D(target=0.005).action == "HOLD"                                  # below the minimum entry size
    assert D(target=0.02).action == "BUY"


def test_rebalance_only_beyond_five_points():
    assert D(flat=False, cur=0.46, rsi_val=2.0).action == "HOLD"            # 4 points under: inside the band
    assert D(flat=False, cur=0.451, rsi_val=2.0).action == "HOLD"
    assert D(flat=False, cur=0.44, rsi_val=2.0).action == "BUY"              # 6 points under and a dip
    assert D(flat=False, cur=0.54).action == "HOLD"                          # 4 points over
    d = D(flat=False, cur=0.56)
    assert (d.action, d.kind, d.to_w) == ("SELL", "trim", 0.5)               # 6 points over: trim to the target


def test_adds_need_an_rsi2_dip_below_5_unless_bias_favour():
    held = dict(flat=False, cur=0.30)
    d = D(rsi_val=40.0, **held)
    assert (d.action, d.kind) == ("HOLD", "wait") and "dip" in d.why
    assert D(rsi_val=5.0, **held).action == "HOLD"                           # RSI(2) must be BELOW 5
    d = D(rsi_val=4.9, **held)
    assert (d.action, d.kind, d.to_w) == ("BUY", "add", 0.5)
    d = D(rsi_val=40.0, bias="favour", **held)
    assert (d.action, d.kind) == ("BUY", "add")                              # favour: add to target without the dip
    assert D(rsi_val=None, **held).action == "HOLD"


def test_bias_block_stops_new_buys_and_adds_but_not_exits_or_the_core():
    assert D(bias="block").action == "SKIP"
    assert D(bias="block", flat=False, cur=0.30, rsi_val=1.0).action == "SKIP"
    assert D(bias="block", flat=False, cur=0.50).action == "HOLD"            # the existing core is kept while the trend is ON
    assert D(bias="block", on=False, target=0.0, flat=False, cur=0.5).action == "SELL"
    assert D(bias="favour", on=False, target=0.0, flat=False, cur=0.5).action == "SELL"   # favour never overrides an OFF trend
    assert D(bias="favour", rsi_val=99.0).action == "SKIP"                   # favour does not buy an overbought entry


def test_trend_off_sells_everything_at_once_even_a_tiny_position():
    d = D(on=False, target=0.0, flat=False, cur=0.01)
    assert (d.action, d.kind, d.to_w) == ("SELL", "exit", 0.0) and "trend is OFF" in d.why
    assert D(on=False, target=0.0, flat=True).action == "HOLD"
    d = D(on=True, target=0.0, flat=False, cur=0.2, brake_level=2)
    assert d.action == "SELL" and "deep drawdown brake" in d.why
    d = D(on=True, target=0.0, flat=False, cur=0.2, eligible=False)
    assert d.action == "SELL" and "re-confirm" in d.why


# ------------------------------------------------------------------ drawdown brake
def B(state, value, on=None):
    return trend.brake_update(state, value, on or {"GOLDBEES": True, "SILVERBEES": False}, ST)


def test_brake_steps_at_15_and_25_percent():
    s = B(None, 100000.0)
    assert s["level"] == 0 and s["peak"] == 100000.0 and trend.brake_mult(0, ST) == 1.0
    s = B(s, 120000.0)
    assert s["peak"] == 120000.0
    s = B(s, 120000.0 * 0.86)                                                # -14%
    assert s["level"] == 0
    s = B(s, 120000.0 * 0.85)                                                # -15% exactly
    assert s["level"] == 1 and s["changed"] and trend.brake_mult(1, ST) == 0.5
    s = B(s, 120000.0 * 0.86)                                                # -14%: still inside the release hysteresis
    assert s["level"] == 1 and not s["changed"]
    s = B(s, 120000.0 * 0.88)                                                # -12%: released
    assert s["level"] == 0 and s["changed"]
    s = B(s, 120000.0 * 0.75)                                                # -25%
    assert s["level"] == 2 and trend.brake_mult(2, ST) == 0.0


def test_brake_level_two_holds_until_recovery_and_reconfirmation():
    s = B(None, 100000.0)
    s = B(s, 74000.0, {"GOLDBEES": True, "SILVERBEES": False})
    assert s["level"] == 2 and s["reconfirm"] == ["GOLDBEES"]               # only the symbols that were ON must re-confirm
    s = B(s, 80000.0)                                                        # -20%: not recovered
    assert s["level"] == 2
    s = B(s, 86000.0, {"GOLDBEES": True, "SILVERBEES": False})               # -14%: recovered, GOLDBEES never turned OFF
    assert s["level"] == 0 and s["reconfirm"] == ["GOLDBEES"]
    w, _ = trend.target_weights({"GOLDBEES": (True, 0.1), "SILVERBEES": (False, 0.3)}, ST, 1.0, s["reconfirm"])
    assert w["GOLDBEES"] == 0.0                                              # still waiting for OFF -> ON
    s = B(s, 86000.0, {"GOLDBEES": False, "SILVERBEES": False})              # the trend turned OFF: confirmation reset
    assert s["reconfirm"] == []
    w, _ = trend.target_weights({"GOLDBEES": (True, 0.1), "SILVERBEES": (False, 0.3)}, ST, 1.0, s["reconfirm"])
    assert w["GOLDBEES"] > 0


def test_brake_peak_never_below_start_capital():
    s = trend.brake_update(None, 90000.0, {}, ST, start_capital=100000.0)
    assert s["peak"] == 100000.0 and s["level"] == 0 and s["dd"] == pytest.approx(-0.10)
