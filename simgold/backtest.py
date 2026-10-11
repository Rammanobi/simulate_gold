"""Daily-bar backtest of the same brain (news layer off — free historical headlines don't exist).
Signals use data up to day t's close; orders fill at day t+1's close (no look-ahead)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .brain import decide
from .broker import Portfolio
from .data import daily_history
from .features import all_tickers, build_frames, snapshot_row


def run(start: str = "2019-01-01", warmup: int = 120) -> dict:
    hist = daily_history(all_tickers(), start=start)
    frames = build_frames(hist)
    idx = sorted(set.intersection(*(set(f.dropna(subset=["close"]).index) for f in frames.values())))
    pf = Portfolio()
    curve, reports = [], []
    for i in range(warmup, len(idx) - 1):
        d, nxt = idx[i], idx[i + 1]
        snap = {a: snapshot_row(f.loc[d]) for a, f in frames.items()}
        orders, rep = decide(pf, snap, d)
        for o in orders:
            pf.execute(o["asset"], o["side"], o["qty"], float(frames[o["asset"]].at[nxt, "close"]), nxt, o["reason"])
        prices_next = {a: float(f.at[nxt, "close"]) for a, f in frames.items()}
        curve.append((nxt, pf.equity(prices_next), sum(pf.weights(prices_next).values())))
        reports.append(rep)
    eq = pd.DataFrame(curve, columns=["date", "equity", "gross"]).set_index("date")
    gold = frames["GOLD"]["close"].reindex(eq.index)
    bench = config.STARTING_CAPITAL * gold / gold.iloc[0]
    return {"equity": eq, "benchmark_gold": bench, "portfolio": pf, "stats": stats(eq["equity"]),
            "bench_stats": stats(bench), "last_report": reports[-1] if reports else None}


def stats(equity: pd.Series) -> dict:
    rets = equity.pct_change().dropna()
    monthly = equity.resample("ME").last().pct_change().dropna()
    first_m = equity.resample("ME").last().iloc[0] / equity.iloc[0] - 1
    monthly = pd.concat([pd.Series([first_m], index=[equity.resample("ME").last().index[0]]), monthly])
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    dd = equity / equity.cummax() - 1
    return {
        "final_equity": round(float(equity.iloc[-1]), 0),
        "cagr": round(float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1), 4),
        "max_drawdown": round(float(dd.min()), 4),
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(252)), 2) if rets.std() > 0 else 0.0,
        "months": int(len(monthly)),
        "avg_month": round(float(monthly.mean()), 4),
        "median_month": round(float(monthly.median()), 4),
        "pct_months_positive": round(float((monthly > 0).mean()), 3),
        "pct_months_ge_3pct": round(float((monthly >= 0.03).mean()), 3),
        "worst_month": round(float(monthly.min()), 4),
        "best_month": round(float(monthly.max()), 4),
        "monthly": monthly,
    }
