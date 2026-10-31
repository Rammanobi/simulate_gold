"""Holdings and trade ledger (simgold/holdings.py), the email sections and the dashboard fields."""
import json
from datetime import datetime
from pathlib import Path

from simgold import dashboard, holdings, report
from simgold.market import IST

D = "2026-10-14"
NOW = datetime(2026, 10, 14, 15, 40, tzinfo=IST)


def tr(ts, asset, side, qty, price, costs, pnl=0.0, reason="r"):
    return {"ts": ts, "asset": asset, "side": side, "qty": qty, "price": price, "value": round(qty * price, 2),
            "costs": costs, "pnl": pnl, "reason": reason}


TRADES = [
    tr("2026-10-13T10:00:00+05:30", "GOLDBEES", "BUY", 10, 100.0, 5.0, reason="first buy"),
    tr("2026-10-13T11:00:00+05:30", "GOLDBEES", "BUY", 10, 110.0, 6.0, reason="added"),
    tr("2026-10-14T10:30:00+05:30", "GOLDBEES", "SELL", 5, 120.0, 2.0, pnl=70.0, reason="took half"),
    tr("2026-10-13T12:00:00+05:30", "SILVERBEES", "BUY", 20, 50.0, 3.0),
    tr("2026-10-14T13:00:00+05:30", "SILVERBEES", "SELL", 20, 55.0, 3.0, pnl=94.0),
]


def norm(trades):
    return holdings.merge_trades([holdings.normalize_trade(t) for t in trades])


def by(rows, sym):
    return next(r for r in rows if r["symbol"] == sym)


def test_average_after_two_buys_and_a_partial_sell():
    rows = holdings.compute(norm(TRADES), {"GOLDBEES": 120.0}, 50000.0, upto=D)
    g = by(rows, "GOLDBEES")
    assert g["units"] == 15 and g["avg_price"] == 105.0           # (1000 + 1100) / 20, a sell does not change the average
    assert g["invested"] == 1583.25                              # 15 x 105 + (5 + 6) x 15/20 of the buying costs
    assert g["value"] == 1800.0 and g["gain"] == 216.75 and g["gain_pct"] == 13.69
    assert g["first_buy"] == "2026-10-13"
    s = by(rows, "SILVERBEES")                                    # fully sold: nothing held, realised kept
    assert s["units"] == 0 and s["value"] == 0 and s["invested"] == 0 and s["realised"] == 94.0


def test_as_of_an_earlier_day_ignores_later_trades():
    rows = holdings.compute(norm(TRADES), {"GOLDBEES": 110.0, "SILVERBEES": 50.0}, 1000.0, upto="2026-10-13")
    assert by(rows, "GOLDBEES")["units"] == 20 and by(rows, "GOLDBEES")["invested"] == 2111.0
    assert by(rows, "SILVERBEES")["units"] == 20


def test_shares_add_up_and_liquidbees_units_are_approximate():
    rows = holdings.compute(norm(TRADES), {"GOLDBEES": 120.0}, 5000.0, upto=D)
    assert abs(sum(r["share_pct"] for r in rows) - 100.0) < 0.2
    lb = by(rows, "LIQUIDBEES")
    assert lb["approx"] is True and lb["units"] == 5.0 and lb["value"] == 5000.0 and "approximate" in lb["note"]
    rows = holdings.compute([], {}, 100000.0, liquid_price=1002.5)
    lb = by(rows, "LIQUIDBEES")
    assert lb["approx"] is False and lb["units"] == 99.75 and lb["share_pct"] == 100.0


def test_portfolio_positions_win_on_the_current_day():
    pos = {"GOLDBEES": {"qty": 15, "avg_cost": 105.0, "buy_costs": 8.25, "opened": "2026-10-13"}}
    g = by(holdings.compute(norm(TRADES), {"GOLDBEES": 120.0}, 0.0, upto=D, positions=pos), "GOLDBEES")
    assert g["units"] == 15 and g["invested"] == 1583.25 and g["first_buy"] == "2026-10-13"
    none = by(holdings.compute(norm(TRADES), {"GOLDBEES": 120.0}, 0.0, upto=D, positions={}), "GOLDBEES")
    assert none["units"] == 0


def test_ledger_is_newest_first_deduplicated_and_capped():
    trades = norm(TRADES)
    dec = [holdings.normalize_decision({"ts": "2026-10-14T10:30:41+05:30", "time": "10:30", "action": "SELL", "symbol": "GOLDBEES",
                                        "qty": 5, "price": 120.0, "amount": 600.0, "pnl": 70.0, "reason": "dup"}),
           holdings.normalize_decision({"ts": "2026-10-14T14:00:00+05:30", "time": "14:00", "action": "BUY", "symbol": "HINDCOPPER",
                                        "qty": 3, "price": 450.0, "amount": 1350.0, "pnl": None, "reason": "from decision log only"}),
           holdings.normalize_decision({"action": "BLOCK", "symbol": "GOLDBEES"})]
    merged = holdings.merge_trades(trades, [e for e in dec if e])
    assert len(merged) == 6                                     # the SELL in the decision log is the same trade
    rows, total = holdings.ledger_rows(merged, upto=D, cap=2)
    assert total == 6 and len(rows) == 2
    assert rows[0]["symbol"] == "HINDCOPPER" and rows[0]["costs"] is None and rows[0]["pnl"] is None
    assert [r["time"] for r in holdings.ledger_rows(merged, upto=D)[0]][:2] == ["14:00", "13:00"]
    assert holdings.ledger_rows(merged, upto="2026-10-13")[1] == 3


def root_with(tmp_path: Path, trades, positions=None, liquid=95000.0, archive=()) -> Path:
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "logs" / f"decisions-{D}.jsonl").write_text(json.dumps(
        {"ts": f"{D}T09:20:00+05:30", "time": "09:20", "action": "HOLD", "symbol": "GOLDBEES", "qty": 0, "price": 120.0,
         "amount": 0.0, "pnl": None, "reason": "nothing", "trigger": "Minute check", "value_after": 100000.0}) + "\n")
    (tmp_path / "logs" / f"ticks-{D}.csv").write_text(
        "time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER\n"
        f"09:15:00,100000,75000,0,{liquid},119.0,202.8,468.7\n15:29:00,100000,75000,0,{liquid},120.0,202.8,468.7\n")
    if archive:
        (tmp_path / "logs" / "trades-archive.jsonl").write_text("".join(json.dumps(t) + "\n" for t in archive))
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps(
        {"positions": positions or {}, "floor": 75000.0, "liquidbees": liquid, "cash": 0.0, "trades": trades,
         "day_open_value": 100000.0, "day_costs_date": D, "last_accrual": D}))
    return tmp_path


def test_empty_ledger_text_in_email_and_text(tmp_path):
    root = root_with(tmp_path, [])
    day = report.build_day(D, root)
    html, text = report.render_email(day), report.render_text(day)
    assert "Where your money is" in html and "Every trade so far" in html
    assert "No buys or sells yet." in html and "No buys or sells yet." in text
    assert "Every purchase so far" not in html and "Your holdings" not in html       # the old duplicate sections are gone


def test_email_has_fund_table_and_trades(tmp_path):
    pos = {"GOLDBEES": {"qty": 15, "avg_cost": 105.0, "buy_costs": 8.25, "opened": "2026-10-13", "last_price": 120.0}}
    gold = [t for t in TRADES if t["asset"] == "GOLDBEES"]
    root = root_with(tmp_path, gold[1:], positions=pos, archive=gold[:1])      # the first buy comes from the archive
    day = report.build_day(D, root)
    assert day["holdings_view"]["ledger_total"] == 3
    html, text = report.render_email(day), report.render_text(day)
    assert html.index("Where your money is") < html.index("What I did")
    for needle in ("GOLDBEES", "SILVERBEES", "HINDCOPPER", "₹105.00", "₹1,583", "+₹216", "BUY", "SELL"):
        assert needle in html, needle
    assert "No buys or sells yet" not in html
    assert "WHERE YOUR MONEY IS" in text and "EVERY TRADE SO FAR" in text and "15 shares, avg buy price ₹105.00" in text
    assert "<script" not in html and "Every purchase so far" not in html


def test_email_trades_are_capped_and_say_so(tmp_path):
    many = [tr(f"2026-10-14T{10 + i // 60:02d}:{i % 60:02d}:00+05:30", "GOLDBEES", "BUY", 1, 100.0, 0.1) for i in range(90)]
    day = report.build_day(D, root_with(tmp_path, many))
    assert len(day["holdings_view"]["ledger"]) == 60 and day["holdings_view"]["ledger_total"] == 90
    assert f"and {90 - report.TRADES_SHOWN} older" in report.render_email(day)


def test_dashboard_has_holdings_and_ledger(tmp_path):
    pos = {"GOLDBEES": {"qty": 15, "avg_cost": 105.0, "buy_costs": 8.25, "opened": "2026-10-13", "last_price": 120.0}}
    root = root_with(tmp_path, [t for t in TRADES if t["asset"] == "GOLDBEES"], positions=pos)
    doc = dashboard.build(D, root, NOW)
    assert [h["symbol"] for h in doc["holdings"]] == ["GOLDBEES", "SILVERBEES", "HINDCOPPER", "LIQUIDBEES"]
    g = doc["holdings"][0]
    for k in ("units", "avg_price", "invested", "price", "value", "gain", "gain_pct", "share_pct", "first_buy", "qty", "avg", "close", "unrealized"):
        assert k in g
    assert g["units"] == 15 and g["avg_price"] == 105.0 and g["invested"] == 1583.25
    assert doc["holdings"][3]["approx"] is True
    assert doc["ledger_total"] == 3 and [e["side"] for e in doc["ledger"]] == ["SELL", "BUY", "BUY"]
    assert set(doc["ledger"][0]) >= {"date", "time", "symbol", "side", "units", "price", "amount", "costs", "pnl", "reason"}
    earlier = dashboard.build("2026-10-13", root, NOW)                  # a past day only holds what was bought up to then
    assert earlier["ledger_total"] == 2


def test_dashboard_document_stays_small_with_a_long_ledger(tmp_path):
    many = [tr(f"2026-10-{13 + i // 600:02d}T{9 + (i % 600) // 60:02d}:{i % 60:02d}:00+05:30", "GOLDBEES", "BUY", 1, 100.0, 0.1, reason="x" * 300)
            for i in range(900)]
    doc = dashboard.build(D, root_with(tmp_path, many), NOW)
    assert doc["ledger_total"] == 900 and len(doc["ledger"]) == holdings.DOC_LEDGER_CAP
    assert len(json.dumps(doc)) < 200_000
