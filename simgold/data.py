"""Price data from Yahoo Finance (free, ~1 min delayed for NSE intraday)."""
from __future__ import annotations

import logging
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


def _download(tickers: list[str], **kw) -> pd.DataFrame:
    df = yf.download(tickers, auto_adjust=True, progress=False, threads=True, **kw)
    if df is None or df.empty:
        raise RuntimeError(f"no data returned for {tickers}")
    if not isinstance(df.columns, pd.MultiIndex):  # single ticker
        df.columns = pd.MultiIndex.from_product([df.columns, tickers])
    return df


def daily_history(tickers: list[str], period: str = "2y", start: str | None = None) -> dict[str, pd.DataFrame]:
    """Return {"close","high","low"} frames (index=date, columns=ticker), forward-filled
    so that NSE and US holidays don't create gaps."""
    kw = {"start": start} if start else {"period": period}
    df = _download(tickers, interval="1d", **kw)
    out = {}
    for field in ("Close", "High", "Low"):
        f = df[field].copy()
        f.index = pd.to_datetime(f.index).tz_localize(None).normalize()
        out[field.lower()] = f.sort_index().ffill()
    return out


def live_prices(tickers: list[str]) -> tuple[dict[str, float], dict[str, pd.Timestamp]]:
    """Latest 1-minute close for each ticker plus the bar timestamp (for staleness checks)."""
    df = _download(tickers, period="1d", interval="1m")
    close = df["Close"]
    prices, stamps = {}, {}
    for t in tickers:
        s = close[t].dropna() if t in close else pd.Series(dtype=float)
        if not s.empty:
            prices[t], stamps[t] = float(s.iloc[-1]), s.index[-1]
    missing = set(tickers) - prices.keys()
    if missing:
        log.warning("no intraday bars for %s", sorted(missing))
    return prices, stamps
