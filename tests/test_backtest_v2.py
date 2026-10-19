"""Strategy v2 backtest (simgold/backtest.py run_v2): same rules as the engine, replayed on daily bars. No network."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import v2_params
from simgold import backtest

SYMS = ["GOLDBEES", "SILVERBEES", "HINDCOPPER"]


def frames(kind: str, n: int = 900, seed: int = 5) -> dict:
    idx = pd.bdate_range("2013-01-01", periods=n)
    rng = np.random.default_rng(seed)
    drift = {"up": 0.0012, "down": -0.0012}[kind]
    c = pd.DataFrame({s: 100 * np.cumprod(1 + drift + rng.normal(0, 0.006, n)) for s in SYMS}, index=idx)
    return {"close": c, "high": c * 1.004, "low": c * 0.996}


def test_rising_market_is_invested_and_beats_cash():
    p = v2_params()
    r = backtest.run_v2(frames("up"), p, "2013-06-01")
    assert r["equity"].iloc[-1] > 150000
    assert r["gross"].iloc[-1] > 0.8 and r["gross"].max() <= 1.0 + 1e-9        # never levered
    assert len(r["trades"]) >= 3


def test_falling_market_stays_in_liquidbees():
    p = v2_params()
    r = backtest.run_v2(frames("down"), p, "2013-06-01")
    years = (r["equity"].index[-1] - r["equity"].index[0]).days / 365.25
    assert r["gross"].mean() < 0.1 and r["gross"].iloc[-1] == 0                  # at most brief false starts early on
    assert r["equity"].iloc[-1] > 100000 * (1 + p["capital"]["liquid_yield_annual"]) ** years * 0.9


def test_stats_have_the_requested_columns():
    p = v2_params()
    rep = backtest.v2_report(frames("up"), p, "2013-06-01")
    for k in ("in_sample", "oos"):
        assert set(rep[k]) >= {"cagr", "max_drawdown", "month_win", "worst_month", "trades_per_month", "avg_gross"}
    assert rep["oos"]["from"] >= rep["oos_start"].date().isoformat()
    assert rep["in_sample"]["to"] < rep["oos"]["from"]
    assert -1 < rep["full"]["max_drawdown"] <= 0


def test_lag_zero_fills_on_the_signal_day_and_lag_one_a_day_later():
    f = frames("up")
    p0, p1 = v2_params(), v2_params()
    p0["strategy"]["backtest"]["lag_days"] = 0
    first0 = backtest.run_v2(f, p0, "2013-06-01")["trades"]["date"].iloc[0]
    first1 = backtest.run_v2(f, p1, "2013-06-01")["trades"]["date"].iloc[0]
    assert first1 > first0


def test_overrides_change_the_run():
    p = v2_params()
    q = backtest.apply_overrides(p, ["vol_target=0.05", "symbols.SILVERBEES.cap=0.2"])
    assert q["strategy"]["vol_target"] == 0.05 and q["strategy"]["symbols"]["SILVERBEES"]["cap"] == 0.2
    assert p["strategy"]["vol_target"] == 0.14                                   # the original is untouched
    with pytest.raises(KeyError):
        backtest.apply_overrides(p, ["no_such_key=1"])
    f = frames("up")
    hi = backtest.run_v2(f, p, "2013-06-01")["gross"].mean()
    lo = backtest.run_v2(f, q, "2013-06-01")["gross"].mean()
    assert lo < hi


def test_drawdown_brake_cuts_exposure_in_the_replay():
    f = frames("up")
    n = len(f["close"])
    for s in SYMS:                                              # a sharp -35% crash in the last 60 days
        f["close"].iloc[n - 60:, f["close"].columns.get_loc(s)] *= np.linspace(1, 0.65, 60)
    f["high"], f["low"] = f["close"] * 1.004, f["close"] * 0.996
    r = backtest.run_v2(f, v2_params(), "2013-06-01")
    assert r["brake"].max() >= 1
    assert r["gross"].iloc[-1] < r["gross"].max()


def test_csv_loader_reads_the_research_layout(tmp_path):
    idx = pd.bdate_range("2022-01-03", periods=30)
    rows = []
    for s, base in (("GOLDBEES", 50.0), ("SILVERBEES", 70.0), ("HINDCOPPER", 150.0)):
        for i, d in enumerate(idx):
            if s == "SILVERBEES" and i < 10:
                continue                                    # listed later: the proxy fills the early days
            c = base + i
            rows.append({"date": d, "symbol": s, "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1})
    pd.DataFrame(rows).to_csv(tmp_path / "nse_daily_ohlcv.csv", index=False)
    wide = pd.DataFrame({"SILVER_SPLICED": np.linspace(60, 70, 30)}, index=pd.Index(idx, name="date"))
    wide.to_csv(tmp_path / "nse_close_wide.csv")
    fr = backtest.load_csv_frames(tmp_path, SYMS)
    assert list(fr["close"].columns) == SYMS and len(fr["close"]) == 30
    assert fr["close"]["SILVERBEES"].notna().all()           # proxy used before the listing
    assert (fr["high"] >= fr["close"]).all().all() and (fr["low"] <= fr["close"]).all().all()
