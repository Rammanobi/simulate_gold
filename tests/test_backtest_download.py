"""Backtest price download: survives Yahoo lock errors, fails cleanly when a series stays empty. No network."""
import pandas as pd
import pytest

from simgold import data


def _frame(t):
    idx = pd.date_range("2024-01-01", periods=3)
    cols = pd.MultiIndex.from_product([["Close", "High", "Low"], [t]])
    return pd.DataFrame(1.0, index=idx, columns=cols)


def test_lock_error_then_success(monkeypatch):
    monkeypatch.setattr(data.yf, "set_tz_cache_location", lambda p: None)
    calls, sleeps = {}, []

    def dl(t):
        calls[t] = calls.get(t, 0) + 1
        if t == "B.NS" and calls[t] < 3:
            raise RuntimeError("OperationalError('database is locked')")
        return _frame(t)

    out = data.daily_history_robust(["A.NS", "B.NS"], sleep=sleeps.append, downloader=dl)
    assert list(out["close"].columns) == ["A.NS", "B.NS"]
    assert calls == {"A.NS": 1, "B.NS": 3}
    assert sleeps == [2, 4]


def test_empty_frame_is_retried_then_clean_error(monkeypatch):
    monkeypatch.setattr(data.yf, "set_tz_cache_location", lambda p: None)
    sleeps = []
    with pytest.raises(data.HistoryUnavailable, match="HINDCOPPER.NS"):
        data.daily_history_robust(["GOLDBEES.NS", "HINDCOPPER.NS"], sleep=sleeps.append,
                                  downloader=lambda t: _frame(t) if t == "GOLDBEES.NS" else pd.DataFrame())
    assert sleeps == [2, 4, 8]


def test_run_backtest_exits_cleanly(monkeypatch, tmp_path):
    import subprocess, sys, textwrap
    code = textwrap.dedent("""
        import sys
        from simgold import data
        def boom(*a, **k): raise data.HistoryUnavailable("no price data for HINDCOPPER.NS")
        data.daily_history_robust = boom
        import simgold.backtest as b; b.daily_history_robust = boom
        sys.argv = ["run_backtest.py", "--strategy", "v2"]
        exec(open("run_backtest.py").read())
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert r.returncode != 0
    assert "HINDCOPPER.NS" in r.stderr and "Traceback" not in r.stderr
