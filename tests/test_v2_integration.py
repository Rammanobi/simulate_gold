"""Strategy v2 wiring outside the engine: market snapshot history, dashboard targets, healthcheck, alert severity, docs."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import test_healthcheck as TH
from conftest import v2_params
from simgold import alerts, config, dashboard
from simgold.feed import build_market
from simgold.market import IST

ROOT = Path(__file__).resolve().parent.parent
P1 = config.load_params()                 # v1_scores (see tests/conftest.py)
TICKERS = P1["engine"]["tickers"]


def daily(n=260, today=date(2026, 10, 14)):
    idx = pd.bdate_range(end=pd.Timestamp(today), periods=n)           # includes TODAY's partial bar, as Yahoo does
    c = {tk: 100 * np.cumprod(1 + 0.001 + np.random.default_rng(1).normal(0, 0.004, n)) for tk in TICKERS.values()}
    mk = lambda f: pd.DataFrame({k: v * f for k, v in c.items()}, index=idx)
    return {"close": mk(1.0), "high": mk(1.004), "low": mk(0.996)}


def bars(now):
    idx = pd.DatetimeIndex([now - timedelta(minutes=m) for m in range(5, -1, -1)])
    return {tk: pd.DataFrame({"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 10}, index=idx)
            for tk in TICKERS.values()}


def test_build_market_carries_completed_daily_bars_only_in_v2():
    now = datetime(2026, 10, 14, 15, 10, tzinfo=IST)
    d = daily()
    m2 = build_market(now, bars(now), d, params=v2_params())
    assert set(m2.hist) == set(TICKERS)
    h = m2.hist["GOLDBEES"]
    assert h.dates[-1] < now.date().isoformat() and len(h.close) == 259      # today's partial bar is dropped
    assert all(b >= c for b, c in zip(h.high, h.close)) and all(b <= c for b, c in zip(h.low, h.close))
    m1 = build_market(now, bars(now), d, params=P1)
    assert m1.hist == {}                                                      # v1 builds exactly what it did before
    assert m1.scores and m2.scores


def test_dashboard_has_the_targets(tmp_path):
    import test_dashboard as TD
    root = TD.make_root(tmp_path)
    rows = [TD.dec("15:10", "BUY", "GOLDBEES", "x")]
    for s, st, sg, tg, ac in (("GOLDBEES", "ON", 0.161, 87.0, 86.0), ("SILVERBEES", "OFF", 0.4, 0.0, 0.0)):
        r = json.loads(TD.dec("15:10", "TARGET", s, f"{s} trend {st}", trigger="Trend check"))
        r.update({"state": st, "sigma": sg, "target_pct": tg, "actual_pct": ac})
        rows.append(json.dumps(r))
    f = root / "logs" / f"decisions-{TD.D}.jsonl"
    f.write_text(f.read_text() + "\n".join(rows) + "\n")
    doc = dashboard.build(TD.D, root, TD.NOW)
    assert [t["symbol"] for t in doc["targets"]] == ["GOLDBEES", "SILVERBEES"]
    g = doc["targets"][0]
    assert (g["state"], g["sigma"], g["target_pct"], g["actual_pct"]) == ("ON", 0.161, 87.0, 86.0)
    assert doc["counts"]["BUY"] == 2 and "TARGET" not in doc["counts"]       # TARGET lines are not trades
    assert all(d["action"] != "TARGET" for d in doc["decisions"])
    (tmp_path / "x").mkdir()
    assert dashboard.build(TD.D, TD.make_root(tmp_path / "x"), TD.NOW)["targets"] == []


def test_account_stop_is_a_high_alert_category():
    assert alerts.severity_of("account_stop") == alerts.HIGH


def test_healthcheck_skips_green_guard_invariants_in_v2(tmp_path, monkeypatch):
    # v1: a floor that dropped from yesterday's, and LIQUIDBEES under 30 percent with a position, are problems
    def git(r, a):
        return "bbb 2026-10-12T15:40:00+05:30\n" if a[0] == "log" else json.dumps({"floor": 99500})
    root = TH.setup(tmp_path / "a", value=95000, floor=75000)
    pf = json.loads((root / "state" / "portfolio.json").read_text())
    pf.update({"positions": {"GOLDBEES": {"qty": 10, "avg_cost": 100, "last_price": 100}}, "liquidbees": 10000})
    (root / "state" / "portfolio.json").write_text(json.dumps(pf))
    assert {"floor_lowered", "liquid_low"} <= TH.checks(TH.run(root, git=git))
    monkeypatch.setattr(TH.H, "_is_v2", lambda: True)
    assert not ({"floor_lowered", "liquid_low"} & TH.checks(TH.run(root, git=git)))
    # v2: value near the fixed floor (the Rs 75,000 account stop) is still a HIGH near_floor problem
    root2 = TH.setup(tmp_path / "b", value=75050, floor=75000)
    p = [x for x in TH.run(root2) if x["check"] == "near_floor"]
    assert p and p[0]["severity"] == "high"


def test_docs_describe_strategy_v2_and_the_goal_line():
    rb = (ROOT / "docs" / "RULEBOOK.md").read_text(encoding="utf-8")
    assert "Strategy v2 (growth, aggressive)" in rb and "v1_scores" in rb and "v2_trend" in rb
    cl = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Goal: grow profit over months; evaluated monthly vs buy-and-hold; month-end green no longer required" in cl
    assert "Goal: green" not in cl
