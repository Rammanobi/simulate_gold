"""Idle money as plain cash (capital.idle: cash, owner decision 2026-10-22), exact fill prices, the daily-profit basis,
the floor-buy reason suffix and the healthcheck engine window."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from simgold import config, holdings, livepnl, report, trend
from simgold.broker import Portfolio, Position
from simgold.engine import Env, Market, run_tick
from simgold.market import IST

from conftest import cash_params

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import healthcheck as hc  # noqa: E402

D = date(2026, 10, 14)       # Wednesday, a trading day
P_LB = config.load_params()  # conftest keeps the cached params on the LIQUIDBEES path (the rollback mode)


def at(h, m, d=D):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def env(tmp_path, params):
    return Env(tmp_path / "s", tmp_path / "l", params, environ={}, match_headline=None)


def lb_pf(amount=50000.0, cash=0.0):
    pf = Portfolio()
    pf.cash, pf.liquidbees, pf.last_accrual = cash, amount, (D - timedelta(days=1)).isoformat()
    return pf


# ---------------------------------------------------------------- switch
def test_shipped_params_use_cash():
    shipped = yaml.safe_load((ROOT / "config" / "params.yaml").read_text(encoding="utf-8"))
    assert shipped["capital"]["idle"] == "cash"
    assert config.idle_is_cash(shipped) is True
    assert config.idle_is_cash(cash_params()) is True
    assert config.idle_is_cash(P_LB) is False
    assert config.idle_is_cash({"capital": {}}) is False          # no key: the old path


def test_conversion_keeps_value_and_logs_one_hold(tmp_path):
    pf = lb_pf(50000.0, cash=1000.0)
    pf.positions["GOLDBEES"] = Position(qty=100, avg_cost=100.0, last_price=100.0, opened="2026-10-13")
    params = cash_params()
    e0 = env(tmp_path, params)
    from simgold.engine import Engine
    eng = Engine(pf, Market(at(10, 0), {"GOLDBEES": 100.0}, {}, {}), e0)
    before, costs_before = eng.value(), pf.total_costs
    eng.housekeeping()
    assert pf.liquidbees == 0.0 and pf.cash == pytest.approx(51000.0)
    assert pf.total_costs == costs_before
    assert eng.value() == pytest.approx(before)
    recs = eng.log
    moved = [r for r in recs if r["trigger"] == "Idle cash"]
    assert len(moved) == 1 and moved[0]["action"] == "HOLD"
    assert "Idle money moved from LIQUIDBEES to cash (owner decision)" in moved[0]["reason"]
    eng2 = Engine(pf, Market(at(10, 1), {"GOLDBEES": 100.0}, {}, {}), e0)
    eng2.housekeeping()
    assert not [r for r in eng2.log if r["trigger"] == "Idle cash"]    # only once


def test_no_accrual_in_cash_mode_and_liquidbees_mode_unchanged(tmp_path):
    pf = lb_pf(0.0, cash=100000.0)
    pf.last_accrual = "2026-10-09"                                      # Friday
    run_tick(pf, Market(at(10, 0, date(2026, 10, 12)), {}, {}, {}), env(tmp_path / "c", cash_params()))
    assert pf.cash == 100000.0 and pf.liquidbees == 0.0 and pf.liquidbees_income_today == 0.0
    pf2 = lb_pf(100000.0)
    pf2.last_accrual = "2026-10-09"
    run_tick(pf2, Market(at(10, 0, date(2026, 10, 12)), {}, {}, {}), env(tmp_path / "l", P_LB))
    assert pf2.liquidbees - 100000 == pytest.approx(100000 * ((1 + 0.063 / 365) ** 3 - 1), abs=0.01)
    assert pf2.cash == 0.0


def _engine(tmp_path, params, pf):
    from simgold.engine import Engine
    return Engine(pf, Market(at(10, 0), {"GOLDBEES": 100.0}, {}, {}), env(tmp_path, params))


def test_buy_pays_from_cash_and_sale_returns_to_cash(tmp_path):
    pf = Portfolio()
    pf.cash = 100000.0
    e = _engine(tmp_path, cash_params(), pf)
    r = e._fill("GOLDBEES", "BUY", 100, 100.0, "t", "Minute check")
    assert r["amount"] == pytest.approx(100 * r["fill"]) and r["cost"] > 0
    assert pf.cash == pytest.approx(100000 - r["amount"] - r["cost"]) and pf.liquidbees == 0.0
    cash_after_buy = pf.cash
    s = e._fill("GOLDBEES", "SELL", 100, 101.0, "t", "Minute check")
    assert pf.liquidbees == 0.0 and pf.cash == pytest.approx(cash_after_buy + s["amount"] - s["cost"])


def test_sale_goes_to_liquidbees_in_the_old_mode(tmp_path):
    pf = Portfolio()
    pf.cash = 0.0
    pf.liquidbees = 20000.0
    pf.positions["GOLDBEES"] = Position(qty=100, avg_cost=100.0, last_price=100.0, opened="2026-10-13")
    e = _engine(tmp_path, P_LB, pf)
    s = e._fill("GOLDBEES", "SELL", 100, 101.0, "t", "Minute check")
    assert pf.cash == 0.0 and pf.liquidbees == pytest.approx(20000 + s["amount"] - s["cost"])


def test_trade_record_shows_exact_price_amount_and_costs(tmp_path):
    pf = Portfolio()
    pf.cash = 100000.0
    e = _engine(tmp_path, cash_params(), pf)
    r = e._fill("GOLDBEES", "BUY", 10, 121.45, "why", "Minute check")
    rec = e.record("BUY", "GOLDBEES", r["qty"], r["fill"], "why", "Minute check", costs=round(r["cost"], 2))
    assert rec["price"] == round(r["fill"], 4)
    assert f"10 × ₹{r['fill']:,.4f} = ₹{10 * r['fill']:,.2f}" in rec["reason"]
    assert f"costs ₹{r['cost']:,.2f}" in rec["reason"]


# ---------------------------------------------------------------- displays
def test_fill_text_is_exact():
    assert report.fill_text(15, 121.4575, 1821.8625) == "15 units × ₹121.4575 = ₹1,821.86"
    assert report.fill_text(1, 100.0, 100.0) == "1 unit × ₹100.00 = ₹100.00"


def test_holdings_row_is_cash_in_cash_mode():
    rows = holdings.compute([], {}, 50000.0, idle_cash=True)
    r = rows[-1]
    assert r["cash"] is True and r["name"] == "Cash (idle money)" and r["value"] == 50000.0 and r["units"] is None
    old = holdings.compute([], {}, 50000.0, idle_cash=False)[-1]
    assert old["name"] == "Cash (LIQUIDBEES)" and old["units"] == 50.0


def test_livepnl_cash_mode_reports_cash_and_no_accrual():
    pf = {"cash": 20000.0, "liquidbees": 30000.0, "liquidbees_income_today": 5.0, "day_costs_date": "2026-10-14", "positions": {}}
    params = cash_params()
    d = livepnl.build(pf, {}, {}, [("2026-10-13", 50000.0)], params, at(10, 0), [], True, True)
    assert d["v"] == 1 and d["cash"] == 50000.0 and d["liquidbees"]["value"] == 0.0 and d["liquidbees"]["accrued_today"] == 0.0
    assert d["total_value"] == 50000.0
    d2 = livepnl.build(pf, {}, {}, [("2026-10-13", 50000.0)], P_LB, at(10, 0), [], True, True)
    assert d2["cash"] == 20000.0 and d2["liquidbees"]["value"] == 30000.0


def test_template_and_wording(tmp_path):
    t = (ROOT / "docs" / "dashboard" / "template.html").read_text(encoding="utf-8")
    assert "Cash (idle money)" in t and "isCash" in t and "px4" in t


def _root(tmp_path, pf_extra=None):
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()
    dec = {"ts": "2026-10-22T10:00:00+05:30", "time": "10:00", "action": "BUY", "symbol": "GOLDBEES", "qty": 15, "price": 121.4575,
           "amount": 1821.8625, "pnl": None, "reason": "r", "trigger": "Trend check", "stop": None, "risk": 0, "value_after": 99700, "floor": 75000}
    (tmp_path / "logs" / "decisions-2026-10-22.jsonl").write_text(json.dumps(dec) + "\n", encoding="utf-8")
    (tmp_path / "logs" / "ticks-2026-10-22.csv").write_text(
        "time,value,floor,cash,liquidbees\n09:15,99647.00,75000,50000,0\n15:30,99740.00,75000,50000,0\n", encoding="utf-8")
    pf = {"cash": 50000.0, "liquidbees": 0.0, "positions": {}, "floor": 75000, "day_open_value": 99647.0,
          "day_costs_date": "2026-10-22", "month": "2026-10", "month_start_equity": 100000}
    pf.update(pf_extra or {})
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps(pf))
    (tmp_path / "state" / "equity.csv").write_text("2026-10-20,100100.00\n2026-10-21,100048.00\n")
    return tmp_path


def test_daily_profit_is_close_minus_previous_close_not_the_0915_open(tmp_path):
    day = report.build_day("2026-10-22", _root(tmp_path))
    p = day["portfolio"]
    assert p["open"] == 100048.0 and p["prev_close_date"] == "2026-10-21"
    assert p["close"] == 99740.0 and p["day_pnl"] == pytest.approx(-308.0)
    txt = report.render_text(day)
    assert "vs yesterday's close ₹1,00,048.00, 2026-10-21" in txt and "opened the day" not in txt
    assert "yesterday's close" in report.render_email(day)


def test_cash_mode_email_has_no_liquidbees_and_shows_exact_fill(tmp_path, monkeypatch):
    monkeypatch.setitem(config.load_params()["capital"], "idle", "cash")
    day = report.build_day("2026-10-22", _root(tmp_path))
    html = report.render_email(day)
    txt = report.render_text(day)
    assert "LIQUIDBEES" not in html and "LIQUIDBEES" not in txt
    for out in (html, txt):
        assert "15 units × ₹121.4575 = ₹1,821.86" in out
    assert "Cash (idle money): ₹50,000.00" in txt


# ---------------------------------------------------------------- reason suffix
ST = config.load_params()["strategy"]


def test_floor_buy_reason_does_not_claim_trend_on():
    d = trend.decide(on=False, target=0.5, cur=0.0, flat=True, rsi_val=50.0, bias="neutral", st=ST)
    assert d.action == "BUY" and "trend ON" not in d.why and "trend is OFF" in d.why
    d2 = trend.decide(on=True, target=0.5, cur=0.0, flat=True, rsi_val=50.0, bias="neutral", st=ST)
    assert d2.action == "BUY" and d2.why == "trend ON and no position"
    st2 = {**ST, "entry": {**ST["entry"], "rsi_timing": False}}
    d3 = trend.decide(on=False, target=0.5, cur=0.0, flat=True, rsi_val=50.0, bias="neutral", st=st2)
    assert "trend ON" not in d3.why


# ---------------------------------------------------------------- healthcheck
def _gh(total):
    return lambda args: json.dumps({"total_count": total})


def test_engine_run_missing_only_while_the_market_is_open(tmp_path):
    (tmp_path / "logs").mkdir()
    checks = lambda now: {p["check"] for p in hc.check_engine(tmp_path, now, _gh(0), "o/r")}
    assert "engine_run_missing" in checks(at(12, 0))
    assert "engine_run_missing" in checks(at(15, 30))
    assert "engine_run_missing" not in checks(at(15, 31))
    assert "engine_run_missing" not in checks(at(15, 39))
    assert "engine_run_missing" not in checks(at(9, 12))
    assert hc.engine_should_run(at(10, 0)) and not hc.engine_should_run(at(15, 35))
    assert not hc.engine_should_run(at(10, 0, date(2026, 10, 18)))      # a Sunday
