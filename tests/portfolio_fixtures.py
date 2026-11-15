"""Synthetic days for the 'Where your money is' tests and design previews (no network, no repo state)."""
from __future__ import annotations

import copy
import datetime as dt
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
    if equity is None:      # yesterday's close consistent with the positions: today's change is the price move (+ costs), not a jump from 100,000
        prev_total = _prev_total(ms, positions, trades, prices, date)
        eq = [("2026-10-28", prev_total)]
        ms = summary.money_summary(pf, prices, eq, date, params)
    norm = holdings.merge_trades([holdings.normalize_trade(t) for t in trades])
    px = {**{s: 0 for s in holdings.SYMS}, **prices}
    px = {k: v for k, v in px.items() if v}
    ledger, total = holdings.ledger_rows(norm, upto=date)
    rows = holdings.compute(norm, px, 0.0, upto=date, positions=pos)
    day = json.loads((REPO / "templates" / "sample_day.json").read_text(encoding="utf-8"))
    day["date"] = date
    day["weekday"] = dt.date.fromisoformat(date).strftime("%A")
    day["money_summary"] = ms
    day["holdings_view"] = {"date": date, "rows": rows, "ledger": ledger, "ledger_total": total, "ledger_shown": len(ledger),
                            "total_value": round(sum(r["value"] for r in rows), 2)}
    _make_consistent(day, ms, positions, trades, prices, date, eq)
    return day


PREV_PX = {"GOLDBEES": 116.9, "SILVERBEES": 186.85, "HINDCOPPER": 300.0}
QUOTES = {"GOLDBEES": 117.2, "SILVERBEES": 183.87, "HINDCOPPER": 301.5}


def _prev_total(ms: dict, positions: dict, trades: list, prices: dict, date: str) -> float:
    """Yesterday's close: today's total minus today's price moves on what was held and on what was bought today."""
    move = 0.0
    for s, (q, avg, _c) in positions.items():
        px = prices.get(s)
        if not px:
            continue
        bought = sum(t["qty"] for t in trades if t["asset"] == s and t["side"] == "BUY" and t["ts"][:10] == date)
        move += (q - bought) * (px - PREV_PX.get(s, avg))
        for t in trades:
            if t["asset"] == s and t["side"] == "BUY" and t["ts"][:10] == date:
                move += t["qty"] * (px - t["price"]) - t["costs"]
    return round(ms["total_now"] - move, 2)


def _make_consistent(day: dict, ms: dict, positions: dict, trades: list, prices: dict, date: str, eq: list) -> None:
    """The sample day carries another day's portfolio/safety/targets/decisions; rewrite them from this scenario's own numbers
    so the compact email shows one consistent story (total = cash + holdings, targets = holdings, trades = today's fills)."""
    from simgold import report
    tot, prev = ms["total_now"], eq[-1][1]
    peak = max(START, prev, tot)
    day["portfolio"].update({"open": prev, "prev_close_date": eq[-1][0], "close": tot, "day_pnl": round(tot - prev, 2),
                             "day_pnl_pct": round((tot / prev - 1) * 100, 2), "month_pnl": round(tot - START, 2),
                             "month_pnl_pct": round((tot / START - 1) * 100, 2), "next_value": tot, "distance_to_floor": round(tot - 75000, 2),
                             "cash_in_liquidbees": 0.0, "peak": peak, "drawdown_from_peak": round(max(0.0, 1 - tot / peak), 4)})
    day["safety"] = {"stop": 75000, "peak": peak, "level": 0, "dd1": 0.15, "dd2": 0.25}
    gold = prices.get("GOLDBEES")
    g0 = 116.0
    you = (tot / START - 1) * 100
    day["vs_gold"] = ({"since": "13 Oct", "you": round(you, 3), "gold": round((gold / g0 - 1) * 100, 3), "diff": report.pts_gap(you, (gold / g0 - 1) * 100)}
                      if gold else None)
    if not day["vs_gold"]:
        day.pop("vs_gold")
    day["prev_closes"] = {k: v for k, v in PREV_PX.items() if k in prices}
    day["next_event"] = "JOBS Fri 6 Nov 19:00"
    rows = {r["symbol"]: r for r in ms["funds"]["rows"] if r.get("kind") == "fund"}
    targets = []
    for t in day["targets"]:
        sym, held = t["symbol"], bool(positions.get(t["symbol"], (0,))[0])
        close = prices.get(sym) or QUOTES[sym]
        level = round(close * (0.93 if held else 1.07), 2)
        share = (rows.get(sym) or {}).get("share_pct") or 0.0
        targets.append({**t, "state": "ON" if held else "OFF", "close": close, "level": level, "level_raw": level,
                        "gap_pct": round((level / close - 1) * 100, 2), "target_pct": round(share) if held else 0.0,
                        "actual_pct": round(share) if held else 0.0})
    day["targets"] = targets
    todays = [t for t in trades if t["ts"][:10] == date]
    decs = [{"time": t["ts"][11:16], "action": t["side"], "symbol": t["asset"], "qty": t["qty"], "price": t["price"], "amount": round(t["qty"] * t["price"], 2),
             "pnl": t.get("pnl") if t["side"] == "SELL" else None, "reason": "test", "trigger": "Trend check", "stop": 0, "risk": 0} for t in todays]
    day["decisions"] = decs
    day["counts"] = {"buys": sum(t["side"] == "BUY" for t in todays), "sells": sum(t["side"] == "SELL" for t in todays), "skips": 0, "blocks": 0,
                     "holds": 0, "fast_lane_runs": 0}
    day["holdings_close"] = [{"symbol": r["symbol"], "qty": r["units"], "avg": r["avg_price"], "close": r["price"], "value": r["value"],
                              "gross_value": r.get("gross_value", r["value"]), "unrealized": r["profit"], "stop": 0.0}
                             for r in rows.values() if r.get("held")]


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
