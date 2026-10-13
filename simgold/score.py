"""The rulebook 3.1 entry score (0.00 to 1.00): eight yes/no tests, each worth a fixed number of points.

daily_stats()    -> numbers from completed daily bars (EMA20/EMA50, 10-day return, RSI14, ATR14, daily volatility)
intraday_stats() -> numbers from today's 1-minute bars (VWAP, first-15-minute high, range so far, opening gap)
score_from()     -> (score, parts)   parts maps each rule name to the points it earned
"""
from __future__ import annotations

from datetime import date, time

import pandas as pd

from . import config
from .signals import atr, ema, rsi

OR_END = time(9, 30)       # the first 15 minutes of the session end here


def daily_stats(close: pd.Series, high: pd.Series, low: pd.Series, today: date) -> dict | None:
    """Indicators from COMPLETED daily bars only (any bar dated today or later is dropped)."""
    keep = [i for i in close.index if pd.Timestamp(i).date() < today]
    c = close.loc[keep].dropna()
    if len(c) < 25:
        return None
    h = high.reindex(c.index).ffill()
    l = low.reindex(c.index).ffill()
    ret = c.pct_change().dropna()
    return {
        "last_close": float(c.iloc[-1]),
        "ema20": float(ema(c, 20).iloc[-1]),
        "ema50": float(ema(c, 50).iloc[-1]),
        "ret10": float(c.iloc[-1] / c.iloc[-11] - 1) if len(c) > 10 else 0.0,
        "rsi14": float(rsi(c).iloc[-1]),
        "atr14": float(atr(c, h, l).iloc[-1]),
        "daily_vol": float(ret.tail(20).std()),
    }


def intraday_stats(bars: pd.DataFrame, prev_close: float | None) -> dict | None:
    """bars: today's 1-minute bars, tz-aware IST index, columns open/high/low/close/volume."""
    if bars is None or bars.empty:
        return None
    price = float(bars["close"].iloc[-1])
    vol = bars["volume"].fillna(0) if "volume" in bars else pd.Series(0.0, index=bars.index)
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    vwap = float((typical * vol).sum() / vol.sum()) if vol.sum() > 0 else float(typical.mean())
    first = bars[bars.index.map(lambda t: t.time() < OR_END)]
    or_done = bars.index[-1].time() >= OR_END
    open_px = float(bars["open"].iloc[0])
    return {
        "price": price, "vwap": vwap,
        "or_high": float(first["high"].max()) if len(first) else None, "or_done": or_done,
        "day_range": float(bars["high"].max() - bars["low"].min()),
        "gap": abs(open_px / prev_close - 1) if prev_close else None,
    }


def score_from(d: dict | None, i: dict | None, weights: dict | None = None) -> tuple[float, dict]:
    w = weights or config.load_params()["entry"]["score_weights"]
    parts = {k: 0.0 for k in w}
    if d:
        price = i["price"] if i else d["last_close"]
        parts["close_gt_ema20"] = w["close_gt_ema20"] * (d["last_close"] > d["ema20"])
        parts["ema20_gt_ema50"] = w["ema20_gt_ema50"] * (d["ema20"] > d["ema50"])
        parts["ret10d_pos"] = w["ret10d_pos"] * (d["ret10"] > 0)
        parts["rsi14_50_70"] = w["rsi14_50_70"] * (50 <= d["rsi14"] <= 70)
        if i:
            parts["price_gt_vwap"] = w["price_gt_vwap"] * (price > i["vwap"])
            parts["price_gt_or15_high"] = w["price_gt_or15_high"] * bool(i["or_done"] and i["or_high"] is not None
                                                                          and price > i["or_high"])
            parts["range_lt_1p5_atr14"] = w["range_lt_1p5_atr14"] * (i["day_range"] < 1.5 * d["atr14"])
            parts["open_gap_lt_1_dailyvol"] = w["open_gap_lt_1_dailyvol"] * bool(
                i["gap"] is not None and i["gap"] < d["daily_vol"])
    parts = {k: round(float(v), 4) for k, v in parts.items()}
    return round(sum(parts.values()), 4), parts
