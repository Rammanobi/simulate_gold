"""simgold.summary: the 'money at a glance' numbers reconcile to the paisa."""
from __future__ import annotations

import json
from pathlib import Path

from simgold import config, report, summary

P = config.load_params()
START = 100000.0

BUY = {"ts": "2026-10-21T15:10:20+05:30", "asset": "GOLDBEES", "side": "BUY", "qty": 423, "price": 118.1891,
       "value": 49993.97, "costs": 27.23, "pnl": 0.0}
SELL = {"ts": "2026-10-23T09:16:00+05:30", "asset": "GOLDBEES", "side": "SELL", "qty": 423, "price": 115.5422,
        "value": 48874.35, "costs": 19.29, "pnl": -1166.15}
EQ = [("2026-10-21", 100048.28), ("2026-10-22", 99739.84), ("2026-10-23", 98980.67), ("2026-10-28", 98980.67)]


def real_case(day="2026-10-29"):
    pf = {"cash": 98980.66917235326, "liquidbees": 0.0, "positions": {}, "trades": [BUY, SELL]}
    return summary.money_summary(pf, {}, EQ, day, P)


def test_real_case_reconciles_to_the_paisa():
    s = real_case()
    assert s["total_now"] == 98980.67 and s["cash"] == 98980.67 and s["metals"] == []
    assert s["overall_profit"] == -1019.33 and s["overall_pct"] == -1.019
    assert s["today_profit"] == 0.0 and s["live_profit"] == 0.0 and s["held"] is False
    assert s["bought_total"] == 49993.97 and s["sold_total"] == 48874.35
    assert s["today_bought"] == 0.0 and s["today_sold"] == 0.0
    b = s["breakdown"]
    assert b["booked_after_costs"] == -1166.15
    assert b["costs"] == -46.52 and b["open"] == 0.0 and b["interest"] == 146.82
    assert round(b["booked"] + b["open"] + b["costs"] + b["interest"], 2) == s["overall_profit"] == b["sum"]


def test_trade_day_shows_todays_fills():
    s = real_case("2026-10-23")
    assert s["today_sells"] == [{"symbol": "GOLDBEES", "time": "09:16", "qty": 423, "price": 115.5422, "amount": 48874.35}]
    assert s["today_sold"] == 48874.35 and s["today_bought"] == 0.0
    assert s["bought_total"] == 49993.97                       # the buy of 21 Oct counts in 'so far'
    assert s["prev_close"] == 99739.84 and s["prev_close_date"] == "2026-10-22"


def test_holding_today_every_row_nonzero():
    buy = {"ts": "2026-10-29T10:00:00+05:30", "asset": "GOLDBEES", "side": "BUY", "qty": 423, "price": 116.5,
           "value": 49279.5, "costs": 26.87, "pnl": 0.0}
    cash = 98980.67 - 49279.5 - 26.87
    pf = {"cash": cash, "liquidbees": 0.0, "trades": [BUY, SELL, buy],
          "positions": {"GOLDBEES": {"qty": 423, "avg_cost": 116.5, "buy_costs": 26.87, "last_price": 117.2}}}
    s = summary.money_summary(pf, {"GOLDBEES": 117.2}, EQ, "2026-10-29", P)
    assert s["metals"][0]["value"] == round(423 * 117.2, 2) and s["held"]
    net = round(423 * 117.2 * (1 - 0.0010 / 2), 2)
    assert s["exit_cost"] == round(round(423 * 117.2, 2) - net, 2) > 0
    assert s["total_now"] == round(cash + net, 2)
    assert s["today_bought"] == 49279.5 and len(s["today_buys"]) == 1
    assert s["live_profit"] == round(net - (423 * 116.5 + 26.87), 2)
    b = s["breakdown"]
    assert b["open"] == s["live_profit"] and b["costs"] == -46.52          # open buy costs are inside the open gain
    assert round(b["booked"] + b["open"] + b["costs"] + b["interest"], 2) == s["overall_profit"]
    assert s["today_profit"] == round(s["total_now"] - 98980.67, 2)


def test_archive_trades_count_and_duplicates_collapse():
    pf = {"cash": 98980.67, "positions": {}, "trades": [SELL]}
    s = summary.money_summary(pf, {}, EQ, "2026-10-29", P, archive=[BUY, SELL])
    assert s["bought_total"] == 49993.97 and s["sold_total"] == 48874.35


def test_total_override_and_start_default():
    s = summary.money_summary({"cash": 5.0, "positions": {}, "trades": []}, {}, [], "2026-10-12", P, total=100000.0)
    assert s["total_now"] == 100000.0 and s["overall_profit"] == 0.0 and s["today_profit"] == 0.0 and s["prev_close_date"] is None


def test_read_archive_skips_bad_lines(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text(json.dumps(BUY) + "\nnot json\n[1]\n")
    assert summary.read_archive(f) == [BUY] and summary.read_archive(tmp_path / "none") == []


def test_email_block_comes_first_and_reads_plainly():
    s = real_case()
    h = report._summary_html(s)
    assert h.index("Your money at a glance") < h.index("Details")
    assert "₹98,980.67" in h and "−₹1,019.33 (−1.02%)" in h and "+₹146.82" in h
    t = "\n".join(report._summary_text(s))
    assert t.startswith("YOUR MONEY AT A GLANCE") and "DETAILS" in t and "Nothing held, so zero" in t
