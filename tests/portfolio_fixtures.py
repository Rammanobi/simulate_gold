"""Synthetic days for the 'Where your money is' tests and design previews (no network, no repo state)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from simgold import config, holdings, summary

REPO = Path(__file__).resolve().parent.parent
DATE = "2026-10-29"
START = 100000.0


def trade(day, hhmm, asset, side, qty, price, costs=0.0, pnl=0.0):
    return {"ts": f"{day}T{hhmm}:00+05:30", "asset": asset, "side": side, "qty": qty, "price": price,
            "value": round(qty * price, 2), "costs": costs, "pnl": pnl, "reason": "test"}


def make_day(positions: dict, trades: list, prices: dict, style: str = "A", date: str = DATE, equity=None) -> dict:
    """positions: {SYM: (qty, avg_cost, buy_costs)}; cash is whatever is left of START after the trades."""
    spent = sum(t["value"] + t["costs"] if t["side"] == "BUY" else -(t["value"] - t["costs"]) for t in trades)
    pos = {s: {"qty": q, "avg_cost": a, "buy_costs": c, "last_price": prices.get(s, a)} for s, (q, a, c) in positions.items()}
    pf = {"cash": START - spent, "liquidbees": 0.0, "positions": pos, "trades": trades}
    params = copy.deepcopy(config.load_params())
    params["report"] = {"portfolio_style": style}
    eq = equity or [("2026-10-28", START)]
    ms = summary.money_summary(pf, prices, eq, date, params)
    norm = holdings.merge_trades([holdings.normalize_trade(t) for t in trades])
    px = {**{s: 0 for s in holdings.SYMS}, **prices}
    px = {k: v for k, v in px.items() if v}
    ledger, total = holdings.ledger_rows(norm, upto=date)
    rows = holdings.compute(norm, px, 0.0, upto=date, positions=pos)
    day = json.loads((REPO / "templates" / "sample_day.json").read_text(encoding="utf-8"))
    day["date"] = date
    day["money_summary"] = ms
    day["holdings_view"] = {"date": date, "rows": rows, "ledger": ledger, "ledger_total": total, "ledger_shown": len(ledger),
                            "total_value": round(sum(r["value"] for r in rows), 2)}
    return day


PRICES3 = {"GOLDBEES": 117.20, "SILVERBEES": 183.87, "HINDCOPPER": 301.50}


def scenarios(style: str = "A") -> dict:
    d = DATE
    out = {}
    out["all_cash"] = make_day({}, [], {"GOLDBEES": 117.2, "SILVERBEES": 183.87}, style)
    t1 = [trade(d, "10:00", "GOLDBEES", "BUY", 423, 116.5, 26.87)]
    out["one_holding"] = make_day({"GOLDBEES": (423, 116.5, 26.87)}, t1, {"GOLDBEES": 117.2}, style)
    t3 = [trade("2026-10-21", "10:00", "GOLDBEES", "BUY", 200, 116.5, 12.0), trade("2026-10-22", "10:00", "SILVERBEES", "BUY", 120, 180.0, 11.0),
          trade("2026-10-23", "10:00", "HINDCOPPER", "BUY", 30, 300.0, 5.0)]
    out["three_holdings"] = make_day({"GOLDBEES": (200, 116.5, 12.0), "SILVERBEES": (120, 180.0, 11.0), "HINDCOPPER": (30, 300.0, 5.0)}, t3, PRICES3, style)
    out["loss"] = make_day({"GOLDBEES": (423, 118.0, 26.87)}, [trade("2026-10-21", "10:00", "GOLDBEES", "BUY", 423, 118.0, 26.87)],
                           {"GOLDBEES": 112.4}, style)
    t_today = [trade("2026-10-28", "10:00", "SILVERBEES", "BUY", 120, 180.0, 11.0), trade(d, "10:00", "GOLDBEES", "BUY", 423, 116.5, 26.87)]
    out["bought_today"] = make_day({"GOLDBEES": (423, 116.5, 26.87), "SILVERBEES": (120, 180.0, 11.0)}, t_today,
                                   {"GOLDBEES": 117.2, "SILVERBEES": 183.87}, style)
    many = []
    for i in range(18):
        day = f"2026-10-{(i % 20) + 1:02d}"
        many.append(trade(day, "09:30", "GOLDBEES", "BUY", 10, 110.0 + i, 1.0))
        many.append(trade(day, "14:30", "GOLDBEES", "SELL", 10, 111.0 + i, 1.0, pnl=9.0))
    out["many_trades"] = make_day({}, many, {"GOLDBEES": 117.2}, style)
    return out
