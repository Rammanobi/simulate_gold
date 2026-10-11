"""Turn raw price history into the per-asset snapshot the brain consumes."""
from __future__ import annotations

import pandas as pd

from . import config
from .signals import technical_frame, macro_score


def all_tickers() -> list[str]:
    return sorted({a["ticker"] for a in config.ASSETS.values()} | set(config.MACRO_TICKERS))


def build_frames(hist: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Per asset: technical columns + macro score, indexed by date."""
    out = {}
    for name, spec in config.ASSETS.items():
        t = spec["ticker"]
        if t not in hist["close"]:
            continue
        c = hist["close"][t].dropna()
        f = technical_frame(c, hist["high"][t].reindex(c.index).ffill(), hist["low"][t].reindex(c.index).ffill())
        f["macro"] = macro_score(spec["drivers"], hist["close"]).reindex(f.index).fillna(0)
        out[name] = f
    return out


def snapshot_row(row: pd.Series, price: float | None = None) -> dict:
    return {"price": float(price if price is not None else row["close"]),
            **{k: float(row[k]) if pd.notna(row[k]) else 0.0
               for k in ("tech", "macro", "atr", "vol", "rsi", "trend", "mom")}}


def with_live_prices(hist: dict[str, pd.DataFrame], live: dict[str, float], today: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Overwrite/append today's bar with the live price so indicators react intraday."""
    h = {k: v.copy() for k, v in hist.items()}
    for t, p in live.items():
        for k in h:
            if today not in h[k].index:
                h[k].loc[today] = h[k].iloc[-1]
            if t in h[k]:
                cur = h[k].at[today, t]
                if k == "close":
                    h[k].at[today, t] = p
                elif k == "high":
                    h[k].at[today, t] = max(cur, p) if pd.notna(cur) else p
                else:
                    h[k].at[today, t] = min(cur, p) if pd.notna(cur) else p
    return {k: v.sort_index() for k, v in h.items()}
