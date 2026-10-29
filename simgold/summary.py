"""'Your money at a glance': seven plain numbers and a breakdown, shared by the email, the dashboard and the live card.

`money_summary()` is a pure function (no I/O). Every amount is computed in whole paise and the parts add up exactly:

    overall profit = booked profit (before costs) + open gain - costs paid on closed trades + interest on idle money

`interest` is what is left over after the three measured parts, so the line always reconciles to the paisa. Before the
22 Oct 2026 switch to cash it is the LIQUIDBEES interest; afterwards it is ~0 (any rounding dust shows up there).
Valuation is the engine's own (Engine.value): metals count at price x (1 - cost/2), i.e. after the estimated cost of
selling them, so `total_now` equals state/equity.csv and the live card. The estimated exit cost is shown on its own line.
Paper money only. Nothing here can trade or change state.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

START_CAPITAL = 100_000.0
BOOK_KEYS = ("ts", "asset", "side", "qty", "price")
FUNDS = (("GOLDBEES", "Gold ETF"), ("SILVERBEES", "Silver ETF"), ("HINDCOPPER", "Copper share"))
METAL = {"GOLDBEES": "gold", "SILVERBEES": "silver", "HINDCOPPER": "copper", "CASH": "cash"}
STYLES = ("A", "B")
MAX_FILLS = 12          # fills listed per side; the totals always cover all of them (keeps the dashboard document small)


def _n(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0


def _p(x) -> int:
    """Rupees -> whole paise (half away from zero, so -0.005 and 0.005 are symmetric)."""
    x = _n(x) * 100
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _r(paise: int) -> float:
    return round(paise / 100, 2) + 0.0


def read_archive(path) -> list[dict]:
    """logs/trades-archive.jsonl -> list of trade dicts. Bad lines and a missing file are skipped. Never raises."""
    out: list[dict] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(line)
            except ValueError:
                continue
            if isinstance(t, dict):
                out.append(t)
    except OSError:
        pass
    return out


def merge_trades(*lists) -> list[dict]:
    """Union of trade lists (archive + portfolio.json), without duplicates, oldest first."""
    seen, out = set(), []
    for lst in lists:
        for t in lst or []:
            if not isinstance(t, dict):
                continue
            k = tuple(str(t.get(f)) for f in BOOK_KEYS)
            if k not in seen:
                seen.add(k)
                out.append(t)
    out.sort(key=lambda t: str(t.get("ts", "")))
    return out


def _price(sym: str, pos: dict, prices: dict) -> float:
    for v in (prices.get(sym), pos.get("last_price"), pos.get("avg_cost")):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
            return float(v)
    return 0.0


def _fills(trades: list[dict], side: str, day: str | None) -> list[dict]:
    rows = []
    for t in trades:
        if t.get("side") != side:
            continue
        d = str(t.get("ts", ""))[:10]
        if (day is None and d) or (day is not None and d == day):
            rows.append({"symbol": t.get("asset"), "time": str(t.get("ts", ""))[11:16], "qty": int(_n(t.get("qty"))),
                         "price": round(_n(t.get("price")), 4), "amount": _r(_p(t.get("value")))})
    return rows


def _inr(p: int) -> str:
    """Whole paise -> '₹1,00,147.50' (Indian grouping, always 2 decimals)."""
    whole, frac = divmod(abs(p), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + parts + [tail])
    return "₹" + s + f".{frac:02d}"


def _pct0(part: int, whole: int) -> str:
    if not whole or part <= 0:
        return "0%"
    x = part * 100 / whole
    return "<1%" if x < 0.5 else f"{x:.0f}%"


def portfolio_style(params: dict | None) -> str:
    """report.portfolio_style: 'A' (cards + bar, default) or 'B' (donut + table); anything else falls back to A."""
    v = str(((params or {}).get("report") or {}).get("portfolio_style", "A")).strip().upper()
    return v if v in STYLES else "A"


def _inr0(p: int) -> str:
    """Whole paise -> whole rupees, Indian grouping: '₹1,00,148'."""
    return _inr(int(round(abs(p) / 100)) * 100)[:-3]


def _sentence(rows: list[dict], base_p: int, idle_p: int, overall_p: int | None) -> str:
    """'₹49,575 (50%) is in gold, ₹22,020 (22%) in silver, ₹0 in copper, ₹27,630 (28%) is cash. Overall −₹775 since start.'"""
    bits = []
    for i, r in enumerate(rows[:3]):
        v = _p(r["value"])
        bits.append((f"{_inr0(v)} ({_pct0(v, base_p)})" if v > 0 else "₹0") + (" is in " if i == 0 else " in ") + r["metal"])
    out = f"{bits[0]}, {bits[1]}, {bits[2]}, {_inr0(idle_p)} ({_pct0(idle_p, base_p)}) is cash."
    if overall_p is not None:
        out += f" Overall {'−' if overall_p < 0 else '+' if overall_p > 0 else ''}{_inr0(overall_p)} since start."
    return out


def funds_table(metals: list[dict], prices: dict, positions: dict, gross_p: int, exit_p: int, idle_p: int, total_p: int,
                idle_cash: bool = True, overall_p: int | None = None, style: str = "A") -> dict:
    """'Where your money is': one row per fund (always all three, held or not), cash, and a total, from the same paise
    that money_summary() uses, so it matches the glance block to the paisa.

    row: {symbol, label, kind (fund|cash), held, units, avg_price, invested (units x avg + buy costs), price, value (units x price),
    profit (value after selling costs - invested, = the 'open gain' of the glance block), profit_pct, share_pct}.
    share_pct is of (metals value + cash). total.value is the total money now (after the estimated cost of selling)."""
    held = {m["symbol"]: m for m in metals}
    base_p = gross_p + idle_p
    rows, inv_p = [], 0
    for sym, kind in FUNDS:
        m = held.get(sym)
        pos = positions.get(sym) if isinstance(positions.get(sym), dict) else {}
        px = _price(sym, pos, prices or {})
        if m:
            invested_p, value_p, profit_p = _p(m["invested"]), _p(m["value"]), _p(m["open_gain"])
            inv_p += invested_p
            rows.append({"symbol": sym, "metal": METAL[sym], "label": kind, "kind": "fund", "held": True, "units": m["qty"], "avg_price": m["avg_price"],
                         "invested": m["invested"], "price": m["price"], "value": m["value"], "profit": m["open_gain"],
                         "profit_pct": round(profit_p / invested_p * 100, 2) if invested_p else 0.0,
                         "share_pct": round(value_p * 100 / base_p, 1) if base_p else 0.0})
        else:
            rows.append({"symbol": sym, "metal": METAL[sym], "label": kind, "kind": "fund", "held": False, "units": 0, "avg_price": None, "invested": 0.0,
                         "price": round(px, 4) if px else None, "value": 0.0, "profit": 0.0, "profit_pct": 0.0, "share_pct": 0.0})
    rows.append({"symbol": "CASH", "metal": "cash", "label": "Idle money, earns nothing" if idle_cash else "Idle money", "kind": "cash", "held": idle_p > 0,
                 "units": None, "avg_price": None, "invested": None, "price": None, "value": _r(idle_p), "profit": None,
                 "profit_pct": None, "share_pct": round(idle_p * 100 / base_p, 1) if base_p else 0.0})
    open_p = sum(_p(r["profit"]) for r in rows if r["kind"] == "fund")
    total = {"invested": _r(inv_p), "value": _r(total_p), "profit": _r(open_p),
             "profit_pct": round(open_p / inv_p * 100, 2) if inv_p else 0.0, "share_pct": 100.0 if base_p else 0.0,
             "exit_cost": _r(exit_p)}
    sentence = _sentence(rows, base_p, idle_p, overall_p)
    total["overall_profit"] = _r(overall_p) if overall_p is not None else None
    return {"rows": rows, "total": total, "sentence": sentence, "style": style if style in STYLES else "A"}


def money_summary(portfolio: dict, prices: dict, equity_rows, day: str, params: dict | None = None,
                  start_capital: float = START_CAPITAL, archive: list | None = None, total: float | None = None,
                  idle_cash: bool = True) -> dict:
    """The money-at-a-glance numbers for session date `day` (YYYY-MM-DD).

    portfolio    state/portfolio.json (dict with cash, liquidbees, positions, trades)
    prices       {symbol: current price}
    equity_rows  [(date, value)] from state/equity.csv; yesterday's close = the last row dated before `day`
    params       config/params.yaml dict (for the per-instrument cost); missing cost counts as 0
    archive      older trades moved out of portfolio.json (logs/trades-archive.jsonl), for 'since start' totals
    total        override for the portfolio value (a finished day's close); cash is then what is left after the metals
    idle_cash    False: LIQUIDBEES is still a separate balance; it is added to the idle money line either way
    """
    pf = portfolio or {}
    prices = prices or {}
    inst = ((params or {}).get("instruments") or {})
    trades = merge_trades(archive, pf.get("trades"))
    trades = [t for t in trades if str(t.get("ts", ""))[:10] <= day]

    metals, gross_p, net_p, open_p, held_buy_costs_p = [], 0, 0, 0, 0
    for sym, pos in (pf.get("positions") or {}).items():
        qty = int(_n(pos.get("qty")))
        if qty <= 0:
            continue
        px = _price(sym, pos, prices)
        cost = _n((inst.get(sym) or {}).get("cost"))
        g, v = _p(qty * px), _p(qty * px * (1 - cost / 2))
        invested = _p(qty * _n(pos.get("avg_cost")) + _n(pos.get("buy_costs")))
        gross_p, net_p, open_p = gross_p + g, net_p + v, open_p + (v - invested)
        held_buy_costs_p += _p(pos.get("buy_costs"))
        metals.append({"symbol": sym, "qty": qty, "price": round(px, 4), "avg_price": round(_n(pos.get("avg_cost")), 4),
                       "value": _r(g), "net_value": _r(v),
                       "invested": _r(invested), "open_gain": _r(v - invested)})
    exit_p = gross_p - net_p

    idle_p = _p(pf.get("cash")) + _p(pf.get("liquidbees"))
    total_p = _p(total) if total is not None else idle_p + net_p
    if total is not None:
        idle_p = total_p - net_p
    start_p = _p(start_capital)

    prev_date, prev_p = None, start_p
    for d, v in equity_rows or []:
        if d < day and (prev_date is None or d >= prev_date):
            prev_date, prev_p = d, _p(v)
    today_p = total_p - prev_p
    overall_p = total_p - start_p

    buys_t, sells_t = _fills(trades, "BUY", day), _fills(trades, "SELL", day)
    buys_all, sells_all = _fills(trades, "BUY", None), _fills(trades, "SELL", None)

    booked_net_p = sum(_p(t.get("pnl")) for t in trades if t.get("side") == "SELL")
    all_costs_p = sum(_p(t.get("costs")) for t in trades)
    closed_costs_p = max(0, all_costs_p - held_buy_costs_p)
    booked_p = booked_net_p + closed_costs_p                     # before costs
    interest_p = overall_p - booked_p - open_p + closed_costs_p  # the rest; see module docstring

    def tot(rows):
        return _r(sum(_p(r["amount"]) for r in rows))

    funds = funds_table(metals, prices, pf.get("positions") or {}, gross_p, exit_p, idle_p, total_p, idle_cash,
                        overall_p, portfolio_style(params))

    return {
        "day": day, "idle_cash": bool(idle_cash), "start_capital": _r(start_p), "idle_label": "Cash" if idle_cash else "Idle money",
        "total_now": _r(total_p),
        "metals": metals, "metals_value": _r(gross_p), "exit_cost": _r(exit_p), "cash": _r(idle_p),
        "today_buys": buys_t[-MAX_FILLS:], "today_sells": sells_t[-MAX_FILLS:],
        "today_buys_more": max(0, len(buys_t) - MAX_FILLS), "today_sells_more": max(0, len(sells_t) - MAX_FILLS), "today_bought": tot(buys_t), "today_sold": tot(sells_t),
        "bought_total": tot(buys_all), "sold_total": tot(sells_all),
        "prev_close": _r(prev_p), "prev_close_date": prev_date,
        "today_profit": _r(today_p), "today_pct": round(today_p / prev_p * 100, 3) if prev_p else None,
        "overall_profit": _r(overall_p), "overall_pct": round(overall_p / start_p * 100, 3) if start_p else None,
        "live_profit": _r(open_p), "held": bool(metals), "funds": funds,
        "breakdown": {"booked": _r(booked_p), "open": _r(open_p), "costs": _r(-closed_costs_p), "interest": _r(interest_p),
                      "booked_after_costs": _r(booked_net_p), "sum": _r(booked_p + open_p - closed_costs_p + interest_p)},
    }
