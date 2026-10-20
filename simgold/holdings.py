"""Holdings and the full trade ledger (paper trading; read only, never trades).

Answers "where is all the money (gold, silver, copper, LIQUIDBEES) and what did you buy, how many units, at what price?".
Used by the daily report (simgold/report.py) and the dashboard day document (simgold/dashboard.py).

Pure functions (no I/O): normalize_trade, normalize_decision, merge_trades, ledger_rows, replay, compute.
Readers (I/O): read_trades, last_prices, view.

Source of the ledger, in order of trust: state/portfolio.json `trades` and logs/trades-archive.jsonl (the engine's own
fills, with costs), then BUY/SELL records of logs/decisions-*.jsonl for anything the first two do not hold.
Holdings are replayed from the ledger up to the requested day (average price = weighted fill price of the units still held,
a sell takes out its share of the purchase costs). For the engine's current day, state/portfolio.json positions win.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

SYMS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")
LIQUID = "LIQUIDBEES"
NAMES = {"GOLDBEES": "Gold", "SILVERBEES": "Silver", "HINDCOPPER": "Copper", LIQUID: "Cash (LIQUIDBEES)"}
LIQUID_UNIT_APPROX = 1000.0          # a LIQUIDBEES unit is about Rs 1,000; used only when no price is known
EMAIL_LEDGER_CAP = 60
DOC_LEDGER_CAP = 500                 # keeps the dashboard day document well under 200 KB
REASON_CHARS = 160


def _f(v, default=None):
    try:
        if v is None or v == "" or isinstance(v, bool):
            return default
        x = float(v)
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _clip(s, n=REASON_CHARS) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ normalising
def normalize_trade(t: dict) -> dict | None:
    """One engine trade record (portfolio.json / archive) -> ledger entry, or None if unusable."""
    if not isinstance(t, dict):
        return None
    side = str(t.get("side") or t.get("action") or "").upper()
    sym = str(t.get("asset") or t.get("symbol") or "").upper()
    ts = str(t.get("ts") or "")
    units = _f(t.get("qty"), 0.0)
    price = _f(t.get("price"), 0.0)
    if side not in ("BUY", "SELL") or not sym or len(ts) < 16 or not units or units <= 0:
        return None
    amount = _f(t.get("value"))
    if amount is None:
        amount = _f(t.get("amount"))
    if amount is None:
        amount = units * price
    return {"date": ts[:10], "time": ts[11:16], "ts": ts, "symbol": sym, "side": side, "units": int(units),
            "price": round(price, 4), "amount": round(amount, 2), "costs": _f(t.get("costs")),
            "pnl": (round(_f(t.get("pnl"), 0.0), 2) if side == "SELL" else None),
            "reason": _clip(t.get("reason")), "src": "trade"}


def normalize_decision(r: dict, date: str = "") -> dict | None:
    """A BUY/SELL record of decisions-DATE.jsonl -> ledger entry (costs unknown), or None."""
    if not isinstance(r, dict) or str(r.get("action")) not in ("BUY", "SELL"):
        return None
    ts = str(r.get("ts") or "")
    d = ts[:10] if len(ts) >= 10 else date
    tm = ts[11:16] if len(ts) >= 16 else str(r.get("time") or "")[:5]
    units = _f(r.get("qty"), 0.0)
    price = _f(r.get("price"), 0.0)
    if not units or units <= 0 or not d or not tm:
        return None
    side = str(r["action"])
    amount = _f(r.get("amount")) or units * price
    return {"date": d, "time": tm, "ts": ts or f"{d}T{tm}:00", "symbol": str(r.get("symbol") or "").upper(), "side": side,
            "units": int(units), "price": round(price, 4), "amount": round(amount, 2), "costs": None,
            "pnl": (round(_f(r.get("pnl"), 0.0), 2) if side == "SELL" else None),
            "reason": _clip(r.get("reason")), "src": "decision"}


def _key(e: dict) -> tuple:
    return (e["date"], e["time"], e["symbol"], e["side"], e["units"])


def merge_trades(*sources: list[dict]) -> list[dict]:
    """Merge normalised entries (earlier sources win on a duplicate), oldest first, stable within a minute."""
    seen: dict[tuple, dict] = {}
    order = 0
    for src in sources:
        for e in src:
            if e is None:
                continue
            k = _key(e)
            if k in seen:
                continue
            e = dict(e)
            e["_o"] = order
            order += 1
            seen[k] = e
    out = sorted(seen.values(), key=lambda e: (e["date"], e["time"], e["_o"]))
    for e in out:
        e.pop("_o", None)
    return out


def ledger_rows(trades: list[dict], upto: str | None = None, cap: int | None = None) -> tuple[list[dict], int]:
    """(rows newest first, total number up to `upto`). `cap` keeps only the newest rows."""
    rows = [dict(e) for e in trades if upto is None or e["date"] <= upto]
    rows.reverse()
    total = len(rows)
    if cap is not None:
        rows = rows[:cap]
    for r in rows:
        r.pop("src", None)
    return rows, total


# ------------------------------------------------------------------ the maths
def replay(trades: list[dict], upto: str | None = None) -> dict[str, dict]:
    """Per symbol, replay BUY/SELL oldest first. A buy adds units and cost; a sell removes its share of both.
    Returns {sym: {units, cost_value (units x avg), buy_costs (held), first_buy, realised, bought_units, sold_units}}."""
    st: dict[str, dict] = {}
    for e in trades:
        if upto is not None and e["date"] > upto:
            continue
        s = st.setdefault(e["symbol"], {"units": 0, "cost_value": 0.0, "buy_costs": 0.0, "first_buy": "", "realised": 0.0,
                                        "bought_units": 0, "sold_units": 0})
        if e["side"] == "BUY":
            if s["units"] == 0:
                s["first_buy"] = e["date"]
            s["units"] += e["units"]
            s["cost_value"] += e["amount"]
            s["buy_costs"] += e.get("costs") or 0.0
            s["bought_units"] += e["units"]
        else:
            q = min(e["units"], s["units"])
            if q <= 0:
                continue
            share = q / s["units"]
            s["units"] -= q
            s["cost_value"] -= s["cost_value"] * share
            s["buy_costs"] -= s["buy_costs"] * share
            s["sold_units"] += q
            s["realised"] += e.get("pnl") or 0.0
            if s["units"] == 0:
                s["cost_value"] = s["buy_costs"] = 0.0
    return st


def compute(trades: list[dict], prices: dict[str, float], liquid_value: float, upto: str | None = None,
            positions: dict | None = None, liquid_price: float | None = None) -> list[dict]:
    """The four holding rows (GOLDBEES, SILVERBEES, HINDCOPPER, LIQUIDBEES).

    `positions` (state/portfolio.json positions) is passed only for the engine's current day and then overrides the replay
    for units, average price, purchase costs and first-buy date. Metal rows: units, avg_price, invested (units x avg + costs),
    price, value, gain (value - invested), gain_pct, share_pct, first_buy, realised; compatibility keys qty/avg/close/unrealized.
    The LIQUIDBEES row has value, share_pct and approximate units."""
    st = replay(trades, upto)
    rows: list[dict] = []
    for sym in SYMS:
        s = st.get(sym, {})
        units, cost_value, costs = int(s.get("units", 0)), float(s.get("cost_value", 0.0)), float(s.get("buy_costs", 0.0))
        first = s.get("first_buy", "")
        if positions is not None:
            p = positions.get(sym) if isinstance(positions.get(sym), dict) else None
            q = int(_f(p.get("qty"), 0) or 0) if p else 0
            if q > 0:
                avg_p = _f(p.get("avg_cost"), _f(p.get("avg"), 0.0)) or 0.0
                units, cost_value, costs = q, q * avg_p, _f(p.get("buy_costs"), 0.0) or 0.0
                first = str(p.get("opened") or first or "")[:10]
            else:
                units, cost_value, costs, first = 0, 0.0, 0.0, ""
        avg = cost_value / units if units else 0.0
        px = _f(prices.get(sym))
        if px is None and units:
            px = avg
        invested = cost_value + costs
        value = units * px if (units and px is not None) else 0.0
        gain = value - invested if units else 0.0
        rows.append({"symbol": sym, "name": NAMES[sym], "units": units, "avg_price": round(avg, 4),
                     "invested": round(invested, 2), "price": (round(px, 4) if px is not None else None),
                     "value": round(value, 2), "gain": round(gain, 2),
                     "gain_pct": round(100.0 * gain / invested, 2) if units and invested else 0.0,
                     "share_pct": 0.0, "first_buy": first, "realised": round(float(s.get("realised", 0.0)), 2),
                     "qty": units, "avg": round(avg, 4), "close": (round(px, 4) if px is not None else None),
                     "unrealized": round(gain, 2)})
    lv = max(0.0, float(liquid_value or 0.0))
    lp = _f(liquid_price)
    approx = not (lp and lp > 0)
    unit_px = LIQUID_UNIT_APPROX if approx else lp
    rows.append({"symbol": LIQUID, "name": NAMES[LIQUID], "value": round(lv, 2), "share_pct": 0.0,
                 "units": round(lv / unit_px, 2), "price": round(unit_px, 2), "approx": approx,
                 "note": ("about %s units at ~Rs 1,000 a unit (approximate)" % f"{lv / unit_px:,.1f}") if approx else ""})
    total = sum(r["value"] for r in rows)
    for r in rows:
        r["share_pct"] = round(100.0 * r["value"] / total, 1) if total else 0.0
    return rows


# ------------------------------------------------------------------ readers
def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    except OSError:
        pass
    return out


def read_trades(root: Path, portfolio: dict | None = None) -> list[dict]:
    """The full ledger, oldest first (archive + portfolio.json trades + decision logs)."""
    root = Path(root)
    if portfolio is None:
        try:
            portfolio = json.loads((root / "state" / "portfolio.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            portfolio = {}
    archive = [normalize_trade(t) for t in _read_jsonl(root / "logs" / "trades-archive.jsonl")]
    live = [normalize_trade(t) for t in (portfolio.get("trades") or []) if isinstance(t, dict)]
    dec: list[dict] = []
    for p in sorted((root / "logs").glob("decisions-*.jsonl")):
        d = p.stem.replace("decisions-", "")
        dec += [normalize_decision(r, d) for r in _read_jsonl(p)]
    return merge_trades([e for e in archive if e], [e for e in live if e], [e for e in dec if e])


def last_prices(root: Path, date: str) -> dict[str, float]:
    """Last tick price per metal on `date` (logs/ticks-DATE.csv); empty if there is no tick file."""
    out: dict[str, float] = {}
    try:
        with (Path(root) / "logs" / f"ticks-{date}.csv").open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                for s in SYMS:
                    v = _f(row.get(s))
                    if v:
                        out[s] = v
    except OSError:
        pass
    return out


def view(date: str, root: Path, prices: dict[str, float], liquid_value: float, portfolio: dict | None = None,
         cap: int | None = DOC_LEDGER_CAP) -> dict:
    """Everything a report or the dashboard needs for `date`: {date, rows, ledger, ledger_total, ledger_shown, total_value}.
    `prices` are the day's closing prices (missing ones fall back to the last tick, then the last trade price)."""
    root = Path(root)
    if portfolio is None:
        try:
            portfolio = json.loads((root / "state" / "portfolio.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            portfolio = {}
    trades = read_trades(root, portfolio)
    px = {k: v for k, v in (prices or {}).items() if _f(v)}
    for s, v in last_prices(root, date).items():
        px.setdefault(s, v)
    for e in reversed(trades):                          # last resort: the most recent fill up to that day
        if e["date"] <= date and not px.get(e["symbol"]):
            px[e["symbol"]] = e["price"]
    pf_day = max(str(portfolio.get("last_accrual") or ""), str(portfolio.get("day_costs_date") or ""),
                 str((portfolio.get("counters") or {}).get("date") or ""))
    current = bool(pf_day) and date >= pf_day and isinstance(portfolio.get("positions"), dict)
    rows = compute(trades, px, liquid_value, upto=date, positions=portfolio.get("positions") if current else None)
    ledger, total = ledger_rows(trades, upto=date, cap=cap)
    return {"date": date, "rows": rows, "ledger": ledger, "ledger_total": total, "ledger_shown": len(ledger),
            "total_value": round(sum(r["value"] for r in rows), 2)}
