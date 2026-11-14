"""Price data from Yahoo Finance (free, ~1 min delayed for NSE intraday)."""
from __future__ import annotations

import logging
import tempfile
import time
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


def intraday_bars(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Today's 1-minute OHLCV bars per ticker (tz-aware IST index, lower-case columns)."""
    df = _download(tickers, period="1d", interval="1m")
    out = {}
    for t in tickers:
        try:
            sub = pd.DataFrame({c.lower(): df[c][t] for c in ("Open", "High", "Low", "Close", "Volume")})
        except KeyError:
            continue
        sub = sub.dropna(subset=["close"])
        if sub.empty:
            continue
        idx = pd.DatetimeIndex(sub.index)
        sub.index = idx.tz_localize("UTC") if idx.tz is None else idx
        sub.index = sub.index.tz_convert("Asia/Kolkata")
        out[t] = sub.sort_index()
    return out


class HistoryUnavailable(RuntimeError):
    """A needed price series stayed empty after all retries (backtests exit cleanly on this)."""


def _sequential_download(tickers: list[str], start: str | None) -> pd.DataFrame:
    """One ticker at a time, no threads: avoids yfinance's 'database is locked' (shared sqlite tz cache) race."""
    frames = []
    for t in tickers:
        df = yf.download(t, start=start, interval="1d", auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            raise RuntimeError(f"empty download for {t}")
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        df.columns = pd.MultiIndex.from_product([df.columns, [t]])
        frames.append(df)
    return pd.concat(frames, axis=1)


def daily_history_robust(tickers: list[str], start: str | None = None, retries: int = 3, sleep=time.sleep,
                         downloader=None) -> dict[str, pd.DataFrame]:
    """Backtest-grade daily history: fresh temp yfinance cache, sequential downloads, up to `retries` retries
    with 2/4/8 s backoff per ticker. Raises HistoryUnavailable naming the ticker if one stays empty."""
    try:
        yf.set_tz_cache_location(tempfile.mkdtemp(prefix="yf-cache-"))
    except Exception as e:  # cache setup is best effort
        log.warning("could not set yfinance cache location: %s", e)
    dl = downloader or (lambda t: _sequential_download([t], start))
    parts = []
    for t in tickers:
        last = None
        for attempt in range(retries + 1):
            try:
                df = dl(t)
                if df is not None and not df.empty:
                    break
                last = RuntimeError("empty result")
            except Exception as e:
                last = e
            df = None
            if attempt < retries:
                sleep(2 ** (attempt + 1))
        if df is None:
            raise HistoryUnavailable(f"no price data for {t} after {retries + 1} attempts ({last})")
        parts.append(df)
    df = pd.concat(parts, axis=1)
    out = {}
    for field in ("Close", "High", "Low"):
        f = df[field].copy()
        f.index = pd.to_datetime(f.index).tz_localize(None).normalize()
        out[field.lower()] = f.sort_index().ffill()
    return out
