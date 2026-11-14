"""'Your money at a glance': seven plain numbers and a breakdown, shared by the email, the dashboard and the live card.

`money_summary()` is a pure function (no I/O). Every amount is computed in whole paise and the parts add up exactly:

    overall profit = open gain + booked profit + interest on idle money

open gain  value at the day's close price after the estimated cost of selling, minus (units x avg price + buy costs held)
booked     sum of the SELL trades' pnl (the engine's pnl is already after all costs)
interest   what is left over after those two, so the line always reconciles to the paisa. Before the 22 Oct 2026 switch
           to cash it is the LIQUIDBEES interest; afterwards it is ~0 (any rounding dust shows up there).
Valuation is the engine's own (Engine.value): metals count at price x (1 - cost/2), i.e. after the estimated cost of
selling them, so `total_now` equals state/equity.csv and the live card. The estimated exit cost is shown on its own line.
Paper money only. Nothing here can trade or change state.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

log = logging.getLogger(__name__)
START_CAPITAL = 100_000.0
BOOK_KEYS = ("ts", "asset", "side", "qty", "price")
FUNDS = (("GOLDBEES", "Gold ETF"), ("SILVERBEES", "Silver ETF"), ("HINDCOPPER", "Copper share"))
METAL = {"GOLDBEES": "gold", "SILVERBEES": "silver", "HINDCOPPER": "copper", "CASH": "cash"}
STYLES = ("A", "B", "AB")
MAX_FILLS = 12          # fills listed per side; the totals always cover all of them (keeps the dashboard document small)


def _n(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0


def _p(x) -> int:
    """Rupees -> whole paise (half away from zero, so -0.005 and 0.005 are symmetric)."""
    x = _n(x) * 100
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _r(paise: int) -> float:
    return round(paise / 100, 2) + 0.0


SETTLE_TOL = 10.0       # rupees: the most a close value may differ from cash + valued holdings before we stop trusting the state


def settle(nets_x: dict, cash_x: float | None, total_x: float | None = None) -> tuple[dict, int, int]:
    """Whole-paise values that add up exactly: returns ({symbol: value in paise}, cash in paise, total in paise).

    nets_x     {symbol: exact value of the position after the estimated cost of selling, in rupees}
    cash_x     the real cash balance (hard number from the portfolio), or None when unknown
    total_x    the day's close value (state/equity.csv, 2 decimals) when one is given; else the exact cash + holdings

    The total is the close (or the exact total rounded once). Cash stays the real cash to the paisa; the few paise by which
    separately rounded parts would miss the total go to the positions (largest remainder), so total = cash + sum(values)
    EXACTLY and every surface that uses these numbers agrees to the paisa. Prices on old tick files have only 2 decimals, so
    a position can be off its qty x price by a rupee or two: the close value wins over the price.
    When the cash is unknown, or the close is further than SETTLE_TOL from cash + holdings (a state that is not that day's),
    the holdings are plainly rounded and the cash is what is left."""
    exact = sum(nets_x.values())
    base = (cash_x or 0.0) + exact
    total_p = _p(total_x) if total_x is not None else _p(base)
    if cash_x is None or abs(base - (total_x if total_x is not None else base)) > SETTLE_TOL:
        vals = {s: _p(v) for s, v in nets_x.items()}
        return vals, total_p - sum(vals.values()), total_p
    if not nets_x:
        return {}, total_p, total_p
    cash_p = _p(cash_x)
    target = total_p - cash_p
    scale = (target / 100.0) / exact if exact else 1.0
    want = {s: v * scale * 100.0 for s, v in nets_x.items()}                  # exact paise, adding up to `target`
    vals = {s: int(math.floor(v)) for s, v in want.items()}
    rem = target - sum(vals.values())
    order = sorted(want, key=lambda s: want[s] - vals[s], reverse=True)         # largest remainder first
    i = 0
    while rem > 0 and order:
        vals[order[i % len(order)]] += 1
        rem, i = rem - 1, i + 1
    i = 0
    while rem < 0 and order:
        vals[order[-1 - i % len(order)]] -= 1
        rem, i = rem + 1, i + 1
    return vals, cash_p, total_p


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
    """report.portfolio_style: 'AB' (cards + bar, then the table; default), 'A' (cards + bar) or 'B' (donut/waffle + table);
    anything else falls back to AB."""
    v = str(((params or {}).get("report") or {}).get("portfolio_style", "AB")).strip().upper()
    return v if v in STYLES else "AB"


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
                idle_cash: bool = True, overall_p: int | None = None, style: str = "AB") -> dict:
    """'Where your money is': one row per fund (always all three, held or not), cash, and a total, from the same paise
    that money_summary() uses, so it matches the glance block to the paisa.

    row: {symbol, label, kind (fund|cash), held, units, avg_price, invested (units x avg + buy costs), price, value (units x price after the
    estimated cost of selling), gross_value (units x price), profit (value - invested, = the 'open gain' of the glance block), profit_pct,
    share_pct}. share_pct is of (metals value + cash) = total money now. total.value is the total money now."""
    held = {m["symbol"]: m for m in metals}
    base_p = (gross_p - exit_p) + idle_p           # = total money now
    rows, inv_p = [], 0
    for sym, kind in FUNDS:
        m = held.get(sym)
        pos = positions.get(sym) if isinstance(positions.get(sym), dict) else {}
        px = _price(sym, pos, prices or {})
        if m:
            invested_p, value_p, profit_p = _p(m["invested"]), _p(m["value"]), _p(m["open_gain"])
            inv_p += invested_p
            rows.append({"symbol": sym, "metal": METAL[sym], "label": kind, "kind": "fund", "held": True, "units": m["qty"], "avg_price": m["avg_price"],
                         "invested": m["invested"], "price": m["price"], "value": m["value"], "gross_value": m["gross_value"], "profit": m["open_gain"],
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
    return {"rows": rows, "total": total, "sentence": sentence, "style": style if style in STYLES else "AB"}


def money_summary(portfolio: dict, prices: dict, equity_rows, day: str, params: dict | None = None,
                  start_capital: float = START_CAPITAL, archive: list | None = None, total: float | None = None,
                  idle_cash: bool = True) -> dict:
    """The money-at-a-glance numbers for session date `day` (YYYY-MM-DD).

    portfolio    state/portfolio.json (dict with cash, liquidbees, positions, trades)
    prices       {symbol: current price}
    equity_rows  [(date, value)] from state/equity.csv; yesterday's close = the last row dated before `day`
    params       config/params.yaml dict (for the per-instrument cost); missing cost counts as 0
    archive      older trades moved out of portfolio.json (logs/trades-archive.jsonl), for 'since start' totals
    total        override for the portfolio value (a finished day's close). Cash stays the real cash; the rounding paise go to the
                 holdings (see settle()). Only a state that is not that day's (off by more than SETTLE_TOL) makes cash the plug.
    idle_cash    False: LIQUIDBEES is still a separate balance; it is added to the idle money line either way
    """
    pf = portfolio or {}
    prices = prices or {}
    inst = ((params or {}).get("instruments") or {})
    trades = merge_trades(archive, pf.get("trades"))
    trades = [t for t in trades if str(t.get("ts", ""))[:10] <= day]

    held = []
    for sym, pos in (pf.get("positions") or {}).items():
        qty = int(_n(pos.get("qty")))
        if qty <= 0:
            continue
        px = _price(sym, pos, prices)
        cost = _n((inst.get(sym) or {}).get("cost"))
        held.append((sym, pos, qty, px, qty * px * (1 - cost / 2)))
    cash_x = _n(pf.get("cash")) + _n(pf.get("liquidbees"))
    values_p, idle_p, total_p = settle({h[0]: h[4] for h in held}, cash_x if (pf.get("cash") is not None or pf.get("liquidbees") is not None) else None,
                                       _n(total) if total is not None else None)
    metals, gross_p, net_p, open_p, held_buy_costs_p = [], 0, 0, 0, 0
    for sym, pos, qty, px, _x in held:
        g, v = _p(qty * px), values_p[sym]
        invested = _p(qty * _n(pos.get("avg_cost")) + _n(pos.get("buy_costs")))
        gross_p, net_p, open_p = gross_p + g, net_p + v, open_p + (v - invested)
        held_buy_costs_p += _p(pos.get("buy_costs"))
        metals.append({"symbol": sym, "qty": qty, "price": round(px, 4), "avg_price": round(_n(pos.get("avg_cost")), 4),
                       "value": _r(v), "gross_value": _r(g),
                       "invested": _r(invested), "open_gain": _r(v - invested)})
    exit_p = gross_p - net_p
    start_p = _p(start_capital)

    prev_date, prev_p = None, start_p
    for d, v in equity_rows or []:
        if d < day and (prev_date is None or d >= prev_date):
            prev_date, prev_p = d, _p(v)
    today_p = total_p - prev_p
    overall_p = total_p - start_p

    buys_t, sells_t = _fills(trades, "BUY", day), _fills(trades, "SELL", day)
    buys_all, sells_all = _fills(trades, "BUY", None), _fills(trades, "SELL", None)

    # ONE definition per metric (every surface reads these numbers; nothing recomputes them):
    #   open     = value after the estimated cost of selling (at the day's close price) - units x avg price - buy costs held
    #   booked   = sum of the SELL trades' pnl (the engine's pnl is already after all costs)
    #   interest = overall - open - booked: the pre-22-Oct LIQUIDBEES income (a few paise of rounding dust at most afterwards)
    # so overall profit = open + booked + interest to the paisa, by construction.
    booked_p = _p(sum(_n(t.get("pnl")) for t in trades if t.get("side") == "SELL"))      # sum exact, round once
    closed_costs_p = max(0, _p(sum(_n(t.get("costs")) for t in trades)) - held_buy_costs_p)   # information only: already inside booked
    interest_p = overall_p - open_p - booked_p
    breakdown_extra = {}
    realised = pf.get("realised_pnl")
    if isinstance(realised, (int, float)) and not isinstance(realised, bool) and math.isfinite(realised) \
            and not any(str(t.get("ts", ""))[:10] > day for t in merge_trades(archive, pf.get("trades"))):
        gap_p = _p(realised) - booked_p              # the engine's own running total of realised pnl vs the trade list we were given
        if abs(gap_p) > 5:
            log.warning("money_summary: the sells in the trade list add up to %s but the engine's realised_pnl is %s; "
                        "an older trades archive may be missing, so 'past interest' is off by about that much", _r(booked_p), _r(_p(realised)))
            breakdown_extra["ledger_gap"] = _r(gap_p)

    def tot(rows):
        return _r(sum(_p(r["amount"]) for r in rows))

    funds = funds_table(metals, prices, pf.get("positions") or {}, gross_p, exit_p, idle_p, total_p, idle_cash,
                        overall_p, portfolio_style(params))

    return {
        "day": day, "idle_cash": bool(idle_cash), "start_capital": _r(start_p), "idle_label": "Cash" if idle_cash else "Idle money",
        "total_now": _r(total_p),
        "metals": metals, "metals_value": _r(net_p), "metals_gross_value": _r(gross_p), "exit_cost": _r(exit_p), "cash": _r(idle_p),
        "today_buys": buys_t[-MAX_FILLS:], "today_sells": sells_t[-MAX_FILLS:],
        "today_buys_more": max(0, len(buys_t) - MAX_FILLS), "today_sells_more": max(0, len(sells_t) - MAX_FILLS), "today_bought": tot(buys_t), "today_sold": tot(sells_t),
        "bought_total": tot(buys_all), "sold_total": tot(sells_all),
        "prev_close": _r(prev_p), "prev_close_date": prev_date,
        "today_profit": _r(today_p), "today_pct": round(today_p / prev_p * 100, 3) if prev_p else None,
        "overall_profit": _r(overall_p), "overall_pct": round(overall_p / start_p * 100, 3) if start_p else None,
        "live_profit": _r(open_p), "held": bool(metals), "funds": funds,
        "breakdown": {"booked": _r(booked_p), "open": _r(open_p), "interest": _r(interest_p),
                      "sum": _r(booked_p + open_p + interest_p), "costs_in_booked": _r(-closed_costs_p), **breakdown_extra},
    }
