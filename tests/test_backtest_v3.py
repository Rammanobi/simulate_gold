"""run_backtest.py --strategy v3 (simgold/backtest.py run_v2(v3=...), v3_report, load_wide_frames). No network."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import d5_params
from simgold import allocation as A
from simgold import backtest

SYMS = ["GOLDBEES", "SILVERBEES", "HINDCOPPER"]
REPO = Path(__file__).resolve().parent.parent


def frames(kind: str = "up", n: int = 900, seed: int = 5) -> dict:
    idx = pd.bdate_range("2013-01-01", periods=n)
    rng = np.random.default_rng(seed)
    drift = {"up": 0.0012, "down": -0.0012, "mixed": 0.0002}[kind]
    c = pd.DataFrame({s: 100 * np.cumprod(1 + drift + rng.normal(0, 0.006 + 0.004 * k, n)) for k, s in enumerate(SYMS)}, index=idx)
    return {"close": c, "high": c * 1.004, "low": c * 0.996}


def cfg(p, **kw):
    return dict(A.settings(p), **kw)


def test_v3_default_is_fully_invested_and_never_levered():
    p = d5_params()
    r = backtest.run_v2(frames("up"), p, "2013-06-01", v3={"cfg": cfg(p, ai_trust=0.0), "noise_seed": None})
    g = r["gross"]
    assert g.iloc[-1] > 0.95 and g.max() <= 1.0 + 1e-9 and g.iloc[20:].median() > 0.95     # ramps in, then stays invested
    w = r["weights"]
    assert (w["SILVERBEES"] <= 0.50 + 0.02).all() and (w["HINDCOPPER"] <= 0.25 + 0.02).all()   # the owner caps bind the default too
    assert r["equity"].iloc[-1] > 100000


def test_v3_ramp_in_respects_the_turnover_limits_and_the_owner_can_raise_them():
    p = d5_params()
    slow = backtest.run_v2(frames("up"), p, "2013-06-01", v3={"cfg": cfg(p, ai_trust=0.0), "noise_seed": None})
    fast = backtest.run_v2(frames("up"), p, "2013-06-01", v3={"cfg": cfg(p, ai_trust=0.0, ramp_turnover_day=0.6, ramp_turnover_week=1.0),
                                                              "noise_seed": None})
    d_slow = int((slow["gross"] < 0.9).sum())
    d_fast = int((fast["gross"] < 0.9).sum())
    assert d_slow >= 5 and d_fast < d_slow
    gross_moves = slow["gross"].diff().abs().fillna(0)
    assert gross_moves.iloc[:15].max() <= 0.2 + 0.05                                    # during the ramp one day moves at most ~the day limit


def test_noise_ai_is_seeded_and_clamped():
    p = d5_params()
    a = backtest.run_v2(frames("mixed"), p, "2013-06-01", v3={"cfg": cfg(p, ai_trust=1.0), "noise_seed": 11})
    b = backtest.run_v2(frames("mixed"), p, "2013-06-01", v3={"cfg": cfg(p, ai_trust=1.0), "noise_seed": 11})
    c = backtest.run_v2(frames("mixed"), p, "2013-06-01", v3={"cfg": cfg(p, ai_trust=1.0), "noise_seed": 12})
    assert a["equity"].equals(b["equity"]) and not a["equity"].equals(c["equity"])
    assert a["gross"].max() <= 1.0 + 1e-9
    assert (a["weights"]["SILVERBEES"] <= 0.50 + 0.03).all() and (a["weights"]["HINDCOPPER"] <= 0.25 + 0.03).all()


def test_v2_replay_is_untouched_by_the_v3_hook():
    p = d5_params()
    one = backtest.run_v2(frames("up"), p, "2013-06-01")
    two = backtest.run_v2(frames("up"), p, "2013-06-01", v3=None)
    assert one["equity"].equals(two["equity"]) and one["trades"].equals(two["trades"])


def test_v3_report_has_the_three_runs_and_the_benchmark():
    p = d5_params()
    rep = backtest.v3_report(frames("up"), p, "2013-06-01")
    assert set(rep["stats"]) == {"v2", "v3_default", "v3_noise_ai", "buy_hold_gold"}
    for k, s in rep["stats"].items():
        assert set(s["full"]) >= {"cagr", "max_drawdown", "avg_gross"}
    assert rep["stats"]["v3_default"]["full"]["avg_gross"] > rep["stats"]["v2"]["full"]["avg_gross"]      # fully invested vs the 50% floor


def test_wide_csv_loader_and_the_cli(tmp_path):
    f = frames("up", 700)
    for name, key in (("close", "close"), ("high", "high"), ("low", "low")):
        df = f[key].copy()
        df.index.name = "Date"
        df.to_csv(tmp_path / f"{name}.csv")
    (f["close"] * 0.999).rename_axis("Date").to_csv(tmp_path / "open.csv")
    fr = backtest.load_wide_frames(tmp_path, SYMS)
    assert set(fr) == {"close", "high", "low", "open"} and list(fr["close"].columns) == SYMS
    out = tmp_path / "bt3.md"
    r = subprocess.run([sys.executable, str(REPO / "run_backtest.py"), "--strategy", "v3", "--data-dir", str(tmp_path),
                        "--start", "2013-06-03", "--out", str(out)], capture_output=True, text=True, cwd=REPO, timeout=300)
    assert r.returncode == 0, r.stderr[-800:]
    text = out.read_text(encoding="utf-8")
    assert "v3 default (no AI)" in text and "v3 + noise AI" in text and "buy and hold GOLDBEES" in text
