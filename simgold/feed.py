"""Turn fetched bars into the engine's Market snapshot (no network here, so tests can use synthetic bars)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from . import config
from .engine import Market
from .score import daily_stats, intraday_stats, score_from


def build_market(now: datetime, bars: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame],
                 headlines: list[dict] | None = None, params: dict | None = None) -> Market:
    """bars: {TICKER.NS: today's 1-minute OHLCV}; daily: {"close","high","low"} frames with ticker columns."""
    p = params or config.load_params()
    prices, price_ts, scores, series, prev_close = {}, {}, {}, {}, {}
    for sym, tk in p["engine"]["tickers"].items():
        b = bars.get(tk)
        if b is None or b.empty:
            continue
        b = b[b.index <= now]
        if b.empty:
            continue
        d = None
        if tk in daily["close"]:
            d = daily_stats(daily["close"][tk].dropna(), daily["high"][tk], daily["low"][tk], now.date())
        i = intraday_stats(b, d["last_close"] if d else None)
        prices[sym], price_ts[sym] = float(b["close"].iloc[-1]), b.index[-1].to_pydatetime()
        series[sym] = [(ts.to_pydatetime(), float(c)) for ts, c in b["close"].items()]
        if d:
            prev_close[sym] = d["last_close"]
            scores[sym] = score_from(d, i, p["entry"]["score_weights"])[0]
    return Market(now=now, prices=prices, price_ts=price_ts, scores=scores, series=series,
                  prev_close=prev_close, headlines=headlines or [])
