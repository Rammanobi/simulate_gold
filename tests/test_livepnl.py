"""simgold.livepnl: the live P&L payload (pure functions, no network)."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from simgold import config, livepnl
from simgold.broker import Portfolio, Position
from simgold.engine import Engine, Env, Market
from simgold.market import IST

P = config.load_params()
KEYS = {"v", "ts", "session_date", "market_open", "feed_ok", "start_capital", "prev_close_date", "prev_close_value",
        "total_value", "today", "open", "total", "prices", "positions", "liquidbees", "cash", "series", "summary"}


def at(day, hh=11, mm=30):
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, 20, tzinfo=IST)


def pfd(pf: Portfolio) -> dict:
    from dataclasses import asdict
    return asdict(pf)


def two_pos(day="2026-10-22"):
    pf = Portfolio(cash=1000.0, liquidbees=40000.0)
    pf.positions["GOLDBEES"] = Position(qty=300, avg_cost=100.0, opened="2026-10-20", last_price=102.0, buy_costs=20.0)
    pf.positions["SILVERBEES"] = Position(qty=100, avg_cost=200.0, opened="2026-10-20", last_price=196.0, buy_costs=10.0)
    pf.day_costs_date = day
    return pf


def test_total_value_equals_engine_value(tmp_path):
    pf = two_pos()
    eng = Engine(pf, Market(at("2026-10-22"), {}, {}, {}), Env(tmp_path / "s", tmp_path / "l", P))
    prices = {s: p.last_price for s, p in pf.positions.items()}
    d = livepnl.build(pfd(pf), prices, {"GOLDBEES": 101.0, "SILVERBEES": 198.0}, [], P, at("2026-10-22"), [])
    assert d["total_value"] == round(eng.value(), 2)
    assert set(d) == KEYS


def test_held_overnight_dip_then_rise_uses_previous_equity_row():
    pf = two_pos()
    pf.positions.pop("SILVERBEES")
    prev = 1000 + 40000 + 300 * 102.0 * (1 - 0.0005)
    rows = [("2026-10-20", 41000.0), ("2026-10-21", prev)]
    prices = {"GOLDBEES": 101.0}
    d = livepnl.build(pfd(pf), prices, {"GOLDBEES": 102.0}, rows, P, at("2026-10-22"), [])
    assert d["prev_close_date"] == "2026-10-21" and d["prev_close_value"] == round(prev, 2)
    assert d["positions"][0]["day_pnl"] == -300.0 and d["positions"][0]["bought_today"] is False
    assert d["today"]["pnl"] < 0
    # next day: it rises, yesterday's close row is now the baseline
    rows.append(("2026-10-22", 41000.0))
    d2 = livepnl.build(pfd(pf), {"GOLDBEES": 104.0}, {"GOLDBEES": 101.0}, rows, P, at("2026-10-23"), [])
    assert d2["prev_close_date"] == "2026-10-22" and d2["prev_close_value"] == 41000.0
    assert d2["today"]["pnl"] > 0 and d2["positions"][0]["day_pnl"] == 900.0


def test_bought_today_uses_buy_price_and_adds_use_weighted_basis():
    pf = Portfolio(cash=0.0, liquidbees=1000.0)
    pf.positions["GOLDBEES"] = Position(qty=100, avg_cost=110.0, opened="2026-10-22", last_price=109.0, buy_costs=5.0)
    pf.trades = [{"ts": "2026-10-22T11:00:00+05:30", "asset": "GOLDBEES", "side": "BUY", "qty": 100, "price": 110.0, "pnl": 0.0}]
    d = livepnl.build(pfd(pf), {"GOLDBEES": 109.0}, {"GOLDBEES": 120.0}, [], P, at("2026-10-22"), [])
    p = d["positions"][0]
    assert p["bought_today"] is True and p["day_basis"] == 110.0 and p["day_pnl"] == -100.0
    # an add of 100 at 110 to a carried 100 units (prev close 100): basis = 105
    pf.positions["GOLDBEES"] = Position(qty=200, avg_cost=105.0, opened="2026-10-20", last_price=109.0)
    d = livepnl.build(pfd(pf), {"GOLDBEES": 109.0}, {"GOLDBEES": 100.0}, [], P, at("2026-10-22"), [])
    p = d["positions"][0]
    assert p["bought_today"] is False and p["day_basis"] == 105.0 and p["day_pnl"] == 800.0


def test_prev_close_skips_same_day_row_and_empty_equity_uses_start_capital():
    assert livepnl.prev_close_from_equity([("2026-10-21", 5.0), ("2026-10-22", 9.0)], "2026-10-22", 100000.0) == ("2026-10-21", 5.0)
    assert livepnl.prev_close_from_equity([], "2026-10-22", 100000.0) == (None, 100000.0)
    d = livepnl.build(pfd(two_pos()), {}, {}, [], P, at("2026-10-22"), [])
    assert d["prev_close_date"] is None and d["prev_close_value"] == 100000.0


def test_equity_reader_skips_header_and_junk(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text("date,value\n2026-10-20,1.5\nbad\n2026-10-21,nan\n2026-10-21,2.5\n")
    assert livepnl.read_equity(f) == [("2026-10-20", 1.5), ("2026-10-21", 2.5)]
    assert livepnl.read_equity(tmp_path / "missing.csv") == []


def test_no_prev_close_gives_null_day_pnl_and_never_raises():
    d = livepnl.build(pfd(two_pos()), {"GOLDBEES": 101.0}, {}, [], P, at("2026-10-22"), [])
    assert d["positions"][0]["day_pnl"] is None and d["positions"][0]["day_basis"] is None
    assert d["prices"]["GOLDBEES"]["chg_pct"] is None
    json.dumps(d, allow_nan=False)


def test_sell_today_realised_and_small_other():
    pf = Portfolio(cash=0.0, liquidbees=50000.0)
    pf.day_costs_date, pf.day_costs, pf.liquidbees_income_today = "2026-10-22", 10.0, 8.0
    pf.positions["GOLDBEES"] = Position(qty=100, avg_cost=100.0, opened="2026-10-20", last_price=101.0, buy_costs=10.0)
    pf.trades = [{"ts": "2026-10-22T10:00:00+05:30", "asset": "SILVERBEES", "side": "SELL", "qty": 50, "price": 200.0,
                  "costs": 10.0, "pnl": 15.0}]
    # yesterday: 100 GOLDBEES @100 (value 9995) + 50 SILVERBEES @199.5 + liquidbees 40000 + cash 0
    # today: sold silver at 200 (proceeds 10000-10 into liquidbees), gold 101
    pf.liquidbees = 40000 + 8 + 10000 - 10
    prev = 100 * 100 * (1 - 0.0005) + 50 * 199.5 * (1 - 0.0005) + 40000
    d = livepnl.build(pfd(pf), {"GOLDBEES": 101.0}, {"GOLDBEES": 100.0}, [("2026-10-21", prev)], P, at("2026-10-22"), [])
    assert d["today"]["parts"]["realised"] == 25.0      # before the sell cost, which is counted once in `costs`
    assert d["today"]["parts"]["costs"] == -10.0
    assert abs(d["today"]["parts"]["other"]) < 15


def test_weekend_closed_and_price_fallbacks():
    sat = at("2026-10-24")
    pf = two_pos()
    pf.positions["GOLDBEES"].last_price = 0.0
    d = livepnl.build(pfd(pf), {}, {}, [], P, sat, [["15:29", 1.0]])
    assert d["market_open"] is False
    assert d["positions"][0]["price"] == 100.0           # no price, no last_price: avg cost
    assert d["positions"][1]["price"] == 196.0           # last_price
    assert d["series"] == [["15:29", 1.0, 1.0 - 100000.0]]   # a closed market adds no point


def test_nan_and_inf_become_null():
    pf = two_pos()
    d = livepnl.build(pfd(pf), {"GOLDBEES": float("nan")}, {"GOLDBEES": float("inf"), "SILVERBEES": 1.0}, [], P, at("2026-10-22"), [])
    assert d["prices"]["GOLDBEES"]["prev_close"] is None
    assert all(not (isinstance(x, float) and not math.isfinite(x)) for x in json.loads(livepnl.dumps(d)).values() if isinstance(x, float))
    json.loads(livepnl.dumps(d))


def test_series_limits_and_old_shape_ticks(tmp_path):
    rows = [[f"{i:04d}", float(i)] for i in range(1000)]
    out = livepnl.decimate(rows)
    assert len(out) <= 400 and out[0] == rows[0] and out[-1] == rows[-1]
    f = tmp_path / "ticks.csv"
    f.write_text("time,value,floor,cash,liquidbees\n09:15,100.5,75000,0,5\n09:16,101.5,75000,0,5\nxx,1,1,1,1\n")
    assert livepnl.series_from_ticks(f) == [["09:15", 100.5], ["09:16", 101.5]]
    f.write_text("time,value,floor,cash,liquidbees,GOLDBEES\n09:15,100.5,75000,0,5,117.0\n09:15,100.7,75000,0,5,117.1\n")
    assert livepnl.series_from_ticks(f) == [["09:15", 100.7]]
    assert livepnl.series_from_ticks(tmp_path / "nope.csv") == []


def test_payload_is_small_with_400_series_rows():
    ser = [[f"{9 + i // 60:02d}:{i % 60:02d}", 99000.0 + i * 1.23] for i in range(375)] * 2
    d = livepnl.build(pfd(two_pos()), {"GOLDBEES": 101.0, "SILVERBEES": 198.0, "HINDCOPPER": 400.0},
                      {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 401.0}, [], P, at("2026-10-22"), ser)
    assert len(d["series"]) <= 400 and len(livepnl.dumps(d)) <= 16 * 1024


def test_write_local_is_atomic(tmp_path):
    livepnl.write_local(tmp_path / "live_now.json", b'{"v":1}\n')
    assert json.loads((tmp_path / "live_now.json").read_text()) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["live_now.json"]


def test_sell_cost_is_counted_once():
    pf = Portfolio(cash=0.0, liquidbees=1000.0)
    pf.day_costs_date, pf.day_costs = "2026-10-22", 10.0
    pf.trades = [{"ts": "2026-10-22T10:00:00+05:30", "asset": "SILVERBEES", "side": "SELL", "qty": 1, "costs": 10.0, "pnl": 15.0}]
    parts = livepnl.build(pfd(pf), {}, {}, [], P, at("2026-10-22"), [])["today"]["parts"]
    assert parts["realised"] + parts["costs"] == 15.0


def test_tick_close_basis_reconciles_when_yahoo_prev_close_differs(tmp_path):
    """21 Oct: the engine valued GOLDBEES at 118.10 (equity row), Yahoo's daily close says 119.83."""
    logs = tmp_path
    (logs / "ticks-2026-10-20.csv").write_text("time,value,floor,cash,liquidbees,GOLDBEES\n15:29,1,1,0,1,111.00\n")
    (logs / "ticks-2026-10-21.csv").write_text(
        "time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES\n15:28,1,1,0,1,118.00,190.00\n15:29,1,1,0,1,118.10,\n")
    (logs / "ticks-2026-10-22.csv").write_text("time,value,floor,cash,liquidbees,GOLDBEES\n09:15,1,1,0,1,117.00\n")
    tick = livepnl.prev_tick_closes(logs, "2026-10-22", ["GOLDBEES", "SILVERBEES", "HINDCOPPER"])
    assert tick == {"GOLDBEES": 118.10, "SILVERBEES": 190.00}           # last non-empty value; today's file ignored
    assert livepnl.prev_tick_closes(tmp_path / "nope", "2026-10-22", ["GOLDBEES"]) == {}
    pf = Portfolio(cash=0.0, liquidbees=50125.61)
    pf.day_costs_date, pf.liquidbees_income_today = "2026-10-22", 8.65
    pf.positions["GOLDBEES"] = Position(qty=423, avg_cost=118.1891, opened="2026-10-21", last_price=117.06, buy_costs=27.23)
    prev = 50116.96 + 423 * 118.10 * (1 - 0.0005)
    basis = {"GOLDBEES": 119.83, **tick}                                  # Yahoo first, the tick close wins
    d = livepnl.build(pfd(pf), {"GOLDBEES": 117.06}, basis, [("2026-10-21", prev)], P, at("2026-10-22"), [])
    assert d["prices"]["GOLDBEES"]["prev_close"] == 118.10
    assert d["prices"]["GOLDBEES"]["chg_pct"] == round((117.06 / 118.10 - 1) * 100, 3)
    assert abs(d["today"]["parts"]["other"]) < 20
    wrong = livepnl.build(pfd(pf), {"GOLDBEES": 117.06}, {"GOLDBEES": 119.83}, [("2026-10-21", prev)], P, at("2026-10-22"), [])
    assert abs(wrong["today"]["parts"]["other"]) > 500


def test_live_payload_carries_the_money_summary():
    pf = two_pos()
    prices = {"GOLDBEES": 102.0, "SILVERBEES": 196.0}
    d = livepnl.build(pfd(pf), prices, {}, [("2026-10-21", 41000.0)], P, at("2026-10-22"), [])
    s = d["summary"]
    assert d["v"] == 1 and s["total_now"] == d["total_value"]
    assert s["live_profit"] == d["open"]["pnl"] and s["held"] and len(s["metals"]) == 2
    assert s["today_profit"] == d["today"]["pnl"] and s["overall_profit"] == d["total"]["pnl"]
    b = s["breakdown"]
    assert round(b["booked"] + b["open"] + b["costs"] + b["interest"], 2) == s["overall_profit"]
