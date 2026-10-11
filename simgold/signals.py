"""Technical and macro scores, each in [-1, +1]. Vectorised so backtest and live share code."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(close: pd.Series, high: pd.Series, low: pd.Series, n: int = 14) -> pd.Series:
    prev = close.shift()
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def realised_vol(close: pd.Series, n: int = 20) -> pd.Series:
    return close.pct_change().rolling(n).std() * np.sqrt(252)


def risk_adj_momentum(close: pd.Series, n: int = 20) -> pd.Series:
    """n-day return divided by its own expected noise → tanh-squashed to [-1, 1]."""
    ret = close.pct_change(n)
    noise = close.pct_change().rolling(60).std() * np.sqrt(n)
    return np.tanh(ret / noise.replace(0, np.nan))


def technical_frame(close: pd.Series, high: pd.Series, low: pd.Series) -> pd.DataFrame:
    e20, e50, e100 = ema(close, 20), ema(close, 50), ema(close, 100)
    trend = ((close > e50).astype(int) + (e20 > e50).astype(int) + (e50 > e100).astype(int) - 1.5) / 1.5
    mom = risk_adj_momentum(close, 20)
    r = rsi(close)
    # Fade stretched moves; buy dips only inside an uptrend.
    rsi_adj = -((r - 70) / 30).clip(0, 1) + ((30 - r) / 30).clip(0, 1) * (trend > 0)
    score = (0.50 * trend + 0.35 * mom + 0.15 * rsi_adj).clip(-1, 1)
    return pd.DataFrame({
        "close": close, "ema20": e20, "ema50": e50, "rsi": r, "trend": trend, "mom": mom,
        "tech": score, "atr": atr(close, high, low), "vol": realised_vol(close),
    })


def macro_score(drivers: dict[str, float], closes: pd.DataFrame) -> pd.Series:
    num = pd.Series(0.0, index=closes.index)
    den = 0.0
    for tkr, w in drivers.items():
        if tkr in closes:
            num = num + w * risk_adj_momentum(closes[tkr].ffill(), 20).fillna(0)
            den += abs(w)
    return (num / den).clip(-1, 1) if den else num
