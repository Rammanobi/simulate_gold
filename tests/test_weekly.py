"""Weekly 'rules vs live' line (simgold/weekly.py): synthetic bars, no network."""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd

from conftest import cash_params, v2_params
from test_report import GREEN_DAY, green_root  # noqa: F401  (fixture)
from simgold import backtest, evaluate, report, weekly

SYMS = ["GOLDBEES", "SILVERBEES", "HINDCOPPER"]
MON, FRI = date(2026, 10, 26), date(2026, 10, 30)
BASE = date(2026, 10, 23)


def frames(n: int = 900, seed: int = 5, drift: float = 0.0012) -> dict:
    idx = pd.bdate_range(end="2026-10-30", periods=n)
    rng = np.random.default_rng(seed)
    c = pd.DataFrame({s: 100 * np.cumprod(1 + drift + rng.normal(0, 0.006, n)) for s in SYMS}, index=idx)
    return {"close": c, "high": c * 1.004, "low": c * 0.996}


def make_root(tmp_path, equity, trades=(), positions=None):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "equity.csv").write_text("\n".join(f"{d},{v}" for d, v in equity) + "\n")
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps({"positions": positions or {}, "trades": list(trades)}))
    return tmp_path


def params():
    p = cash_params(v2_params())
    p["capital"]["liquid_yield_annual"] = 0.0           # what weekly.py uses when idle money is cash
    return p


def test_week_helpers():
    assert weekly.week_bounds(date(2026, 10, 29)) == (MON, date(2026, 11, 1))
    assert weekly.is_last_trading_day_of_week(FRI)
    assert not weekly.is_last_trading_day_of_week(date(2026, 10, 29))
    assert not weekly.is_last_trading_day_of_week(date(2026, 10, 31))        # Saturday


def test_flat_live_account_vs_rules_and_buy_and_hold(tmp_path):
    f = frames()
    root = make_root(tmp_path, [("2026-10-23", 100000.0), ("2026-10-26", 100000.0), ("2026-10-30", 100000.0)])
    r = weekly.rules_vs_live(root, FRI, f, params())
    assert r["live_pct"] == 0.0 and r["from"] == "2026-10-23" and r["to"] == "2026-10-30" and r["week"] == "2026-W44"
    g = f["close"]["GOLDBEES"]
    assert abs(r["goldbees_pct"] - (g[pd.Timestamp(FRI)] / g[pd.Timestamp(BASE)] - 1) * 100) < 1e-9
    # rules alone, started flat on the same day, must equal a direct run from the same opening book
    res = backtest.run_v2(f, params(), pd.Timestamp(BASE), pd.Timestamp(FRI), init={"cash": 100000.0, "hold": {}})
    assert abs(r["rules_pct"] - (res["equity"].iloc[-1] / 100000 - 1) * 100) < 1e-9
    text = weekly.line(r)
    assert text.startswith("This week: live account +0.00% · rules alone (backtest of the same rules on the same days, no AI blocks/pauses) ")
    assert "GOLDBEES buy-and-hold" in text and text.count("%") == 3


def test_opening_book_is_rebuilt_from_trades_and_held(tmp_path):
    f = frames()
    close = f["close"]
    q = 100
    px = float(close.at[pd.Timestamp(BASE), "GOLDBEES"])
    trades = [{"ts": "2026-10-20T15:10:00+05:30", "asset": "GOLDBEES", "side": "BUY", "qty": q, "price": px}]
    root = make_root(tmp_path, [("2026-10-23", 100000.0), ("2026-10-30", 100500.0)],
                     trades, {"GOLDBEES": {"qty": q}})
    r = weekly.rules_vs_live(root, FRI, f, params())
    assert abs(r["live_pct"] - 0.5) < 1e-9
    res = backtest.run_v2(f, params(), pd.Timestamp(BASE), pd.Timestamp(FRI),
                          init={"cash": 100000.0 - q * px, "hold": {"GOLDBEES": q * px}})
    assert abs(r["rules_pct"] - (res["equity"].iloc[-1] / res["equity"].iloc[0] - 1) * 100) < 1e-9


def test_a_trade_inside_the_week_is_not_in_the_opening_book(tmp_path):
    f = frames()
    trades = [{"ts": "2026-10-27T15:10:00+05:30", "asset": "GOLDBEES", "side": "BUY", "qty": 50, "price": 100.0}]
    root = make_root(tmp_path, [("2026-10-23", 100000.0), ("2026-10-30", 100000.0)], trades, {"GOLDBEES": {"qty": 50}})
    assert weekly._qty_at(weekly._trades(root)[0], weekly._trades(root)[1], SYMS, BASE)["GOLDBEES"] == 0
    assert weekly.rules_vs_live(root, FRI, f, params()) is not None


def test_init_book_takes_no_orders_on_its_first_day():
    f = frames()
    p = params()
    r = backtest.run_v2(f, p, pd.Timestamp(BASE), pd.Timestamp(FRI), init={"cash": 1000.0, "hold": {"GOLDBEES": 99000.0}})
    assert r["equity"].iloc[0] == 100000.0
    assert not (r["trades"]["date"] == pd.Timestamp(BASE)).any() if len(r["trades"]) else True


def test_missing_data_omits_the_line(tmp_path):
    f = frames()
    ok = [("2026-10-23", 100000.0), ("2026-10-30", 100000.0)]
    assert weekly.week_line(tmp_path, FRI, f, params()) is None                                   # no state at all
    root = make_root(tmp_path, [("2026-10-26", 100000.0), ("2026-10-30", 100000.0)])              # no prior-week close
    assert weekly.rules_vs_live(root, FRI, f, params()) is None
    root = make_root(tmp_path, ok)
    short = {k: v[v.index < pd.Timestamp(FRI)] for k, v in f.items()}                              # bars stop before the day
    assert weekly.rules_vs_live(root, FRI, short, params()) is None
    assert weekly.rules_vs_live(root, FRI, None, params()) is None                                # fetch fails (offline fixture)
    root = make_root(tmp_path, ok, [], {"GOLDBEES": {"qty": 5}})                                  # trade list does not add up
    assert weekly.rules_vs_live(root, FRI, f, params()) is None
    p = params()
    p["strategy"]["mode"] = "v1_scores"
    assert weekly.rules_vs_live(make_root(tmp_path, ok), FRI, f, p) is None
    assert weekly.line(None) is None


def test_scorecard_row_only_when_asked(tmp_path, monkeypatch):
    root = make_root(tmp_path, [("2026-09-30", 100000.0), ("2026-10-23", 99000.0), ("2026-10-30", 99500.0)])
    (tmp_path / "logs").mkdir()
    r = evaluate.evaluate(root, "2026-10")
    assert "weekly_line" not in r and all(k != "Latest week" for k, _ in evaluate.rows(r))
    monkeypatch.setattr(weekly, "week_line", lambda root, d, *a, **k: "This week: live account +0.50% · rules alone x")
    r = evaluate.evaluate(root, "2026-10", weekly=True)
    assert ("Latest week", "live account +0.50% · rules alone x") in evaluate.rows(r)
    assert "Latest week" in evaluate.to_md(r)
    monkeypatch.setattr(weekly, "week_line", lambda *a, **k: None)
    assert "weekly_line" not in evaluate.evaluate(root, "2026-10", weekly=True)


def test_email_shows_the_line_on_the_last_trading_day_only(green_root, monkeypatch):  # noqa: F811
    LINE = "This week: live account +1.00% · rules alone (backtest) +2.00% · GOLDBEES buy-and-hold +3.00%"
    calls = []
    monkeypatch.setattr(weekly, "week_line", lambda root, d, *a, **k: calls.append(d) or LINE)
    monkeypatch.setattr(weekly, "is_last_trading_day_of_week", lambda d: True)
    day = report.build_day(GREEN_DAY, green_root)
    assert day["weekly_line"] == LINE and calls
    assert LINE in report.render_text(day) and report._html.escape(LINE) in report.render_email(day)
    monkeypatch.setattr(weekly, "is_last_trading_day_of_week", lambda d: False)
    day = report.build_day(GREEN_DAY, green_root)
    assert "weekly_line" not in day and "This week:" not in report.render_text(day)
    monkeypatch.setattr(weekly, "is_last_trading_day_of_week", lambda d: True)
    monkeypatch.setattr(weekly, "week_line", lambda *a, **k: None)                  # data missing -> no line, no failure
    day = report.build_day(GREEN_DAY, green_root)
    assert not day.get("weekly_line") and "This week:" not in report.render_email(day)
    monkeypatch.setattr(weekly, "week_line", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "weekly_line" not in report.build_day(GREEN_DAY, green_root)              # even a crash never breaks the report
