"""Live portfolio profit for the dashboard: pure functions, no I/O except `write_local`, `read_equity`, `series_from_ticks`.

`build()` turns plain dicts and lists into the `now.json` contract (schema v: 1, docs: strategy/LIVE_PNL_DESIGN.md).
It mirrors Engine.value() exactly: value(pos) = qty x price x (1 - cost/2), total = cash + LIQUIDBEES + positions.
Paper money only. Nothing here can trade or change state.
"""
from __future__ import annotations

import csv
import json
import math
import os
from datetime import date, datetime
from pathlib import Path

from .market import IST, is_market_open

SCHEMA = 1
MAX_SERIES = 400


# ---------------------------------------------------------------- small helpers
def _num(v, nd: int | None = 2):
    """Finite float rounded to nd places, else None (JSON has no NaN/inf)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    x = round(float(v), nd) if nd is not None else float(v)
    return x + 0.0 if x == 0 else x          # no "-0.0"


def _clean(o):
    """Replace any non-finite float in a nested structure by None."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _pos_num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0


def _price(sym: str, pos: dict, prices: dict) -> float:
    """mkt price, else the last known price on the position, else its average cost (same fallback as Engine.value)."""
    for v in (prices.get(sym), pos.get("last_price"), pos.get("avg_cost")):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
            return float(v)
    return 0.0


def _cost(params: dict, sym: str) -> float:
    return _pos_num(((params.get("instruments") or {}).get(sym) or {}).get("cost"))


def position_value(pos: dict, price: float, cost: float) -> float:
    return _pos_num(pos.get("qty")) * price * (1 - cost / 2)


def total_value(pf: dict, prices: dict, params: dict) -> float:
    """cash + LIQUIDBEES + positions after the cost to sell them. Equal to Engine.value()."""
    v = _pos_num(pf.get("cash")) + _pos_num(pf.get("liquidbees"))
    for sym, pos in (pf.get("positions") or {}).items():
        if _pos_num(pos.get("qty")) > 0:
            v += position_value(pos, _price(sym, pos, prices), _cost(params, sym))
    return v


# ---------------------------------------------------------------- equity.csv, series
def read_equity(path: Path) -> list[tuple[str, float]]:
    """state/equity.csv -> [(date, value)]. Bad lines and a header are skipped. Never raises."""
    rows: list[tuple[str, float]] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            d, _, v = line.partition(",")
            try:
                date.fromisoformat(d.strip())
                x = float(v)
            except ValueError:
                continue
            if math.isfinite(x):
                rows.append((d.strip(), x))
    except OSError:
        pass
    return rows


def prev_close_from_equity(rows, session_date: str, start_capital: float) -> tuple[str | None, float]:
    """The last equity row dated BEFORE the session (a same-day row from close_day is skipped). None -> start capital."""
    best = None
    for d, v in rows or []:
        if d < session_date and (best is None or d >= best[0]):
            best = (d, v)
    return (best[0], float(best[1])) if best else (None, float(start_capital))


def series_from_ticks(path: Path) -> list[list]:
    """logs/ticks-D.csv ('time,value,...') -> [[HH:MM, value]]; the old and new column shapes both work. Never raises."""
    out: list[list] = []
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    t, v = (r.get("time") or "").strip(), float(r.get("value"))
                except (TypeError, ValueError):
                    continue
                if len(t) == 5 and math.isfinite(v):
                    out = add_point(out, t, v)
    except OSError:
        pass
    return out


def add_point(series: list[list], hhmm: str, value: float) -> list[list]:
    """Return a new series with this minute's value: replaces the last row if it is the same minute (or older)."""
    s = [list(r[:2]) for r in series or []]
    if s and s[-1][0] >= hhmm:
        if s[-1][0] == hhmm:
            s[-1] = [hhmm, value]
        return s
    s.append([hhmm, value])
    return s


def decimate(rows: list, limit: int = MAX_SERIES) -> list:
    """At most `limit` rows, evenly spread, always keeping the first and the last."""
    n = len(rows)
    if n <= limit:
        return list(rows)
    idx = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
    return [rows[i] for i in idx]


# ---------------------------------------------------------------- today's buys
def _today_buys(pf: dict, sym: str, session_date: str) -> tuple[int, float]:
    """(units bought today, their qty-weighted fill price) from pf.trades."""
    q, amt = 0, 0.0
    for t in pf.get("trades") or []:
        if t.get("asset") == sym and t.get("side") == "BUY" and str(t.get("ts", ""))[:10] == session_date:
            n, px = int(_pos_num(t.get("qty"))), _pos_num(t.get("price"))
            q, amt = q + n, amt + n * px
    return q, (amt / q if q else 0.0)


# ---------------------------------------------------------------- the payload
def build(pf: dict, prices: dict, prev_close: dict, equity_rows, params: dict, now: datetime, series: list,
          feed_ok: bool = True, market_open: bool | None = None, price_ts: dict | None = None,
          start_capital: float = 100000.0, symbols=None) -> dict:
    """The now.json payload (see the module docstring). `pf` is Portfolio.to-dict (asdict) shape; `prices` {sym: price};
    `prev_close` {sym: Yahoo previous session close}; `equity_rows` [(date, value)]; `series` [[HH:MM, value]] (raw, so far);
    `price_ts` {sym: datetime of the newest bar}. No I/O."""
    now = now.astimezone(IST)
    sd = now.date().isoformat()
    prices, prev_close, price_ts = prices or {}, prev_close or {}, price_ts or {}
    if market_open is None:
        market_open = is_market_open(now)
    total = total_value(pf, prices, params)
    pc_date, pc_val = prev_close_from_equity(equity_rows, sd, start_capital)

    positions, parts_pos, inv_sum, open_sum = [], 0.0, 0.0, 0.0
    for sym, pos in (pf.get("positions") or {}).items():
        qty = int(_pos_num(pos.get("qty")))
        if qty <= 0:
            continue
        px = _price(sym, pos, prices)
        avg, bc = _pos_num(pos.get("avg_cost")), _pos_num(pos.get("buy_costs"))
        val = position_value(pos, px, _cost(params, sym))
        invested = qty * avg + bc
        open_pnl = val - invested
        buy_q, buy_px = _today_buys(pf, sym, sd)
        if buy_q == 0 and str(pos.get("opened", ""))[:10] == sd:
            buy_q, buy_px = qty, avg                      # opened today but the trade row is gone: buy price = avg cost
        buy_q = min(buy_q, qty)
        carried = qty - buy_q
        pcl = prev_close.get(sym)
        pcl = float(pcl) if isinstance(pcl, (int, float)) and not isinstance(pcl, bool) and math.isfinite(pcl) and pcl > 0 else None
        if carried == 0:
            basis = buy_px if buy_px > 0 else avg
        elif pcl is None:
            basis = None                                  # no previous close: unknown, not guessed
        else:
            basis = (carried * pcl + buy_q * buy_px) / qty
        day_pnl = qty * (px - basis) if basis is not None else None
        if day_pnl is not None:
            parts_pos += day_pnl
        inv_sum += invested
        open_sum += open_pnl
        positions.append({
            "symbol": sym, "qty": qty, "avg_cost": _num(avg, 4), "buy_costs": _num(bc), "price": _num(px),
            "value": _num(val), "invested": _num(invested), "open_pnl": _num(open_pnl),
            "open_pct": _num(open_pnl / invested * 100 if invested else None, 3),
            "day_basis": _num(basis), "day_pnl": _num(day_pnl), "bought_today": carried == 0,
            "opened": str(pos.get("opened") or "") or None})

    syms = list(symbols) if symbols else list((params.get("instruments") or {}).keys())
    for sym in (pf.get("positions") or {}):
        if sym not in syms:
            syms.append(sym)
    price_rows = {}
    for sym in syms:
        px = prices.get(sym)
        px = float(px) if isinstance(px, (int, float)) and not isinstance(px, bool) and math.isfinite(px) and px > 0 else None
        pos = (pf.get("positions") or {}).get(sym)
        if px is None and pos:
            px = _price(sym, pos, prices) or None
        pcl = prev_close.get(sym)
        pcl = float(pcl) if isinstance(pcl, (int, float)) and not isinstance(pcl, bool) and math.isfinite(pcl) and pcl > 0 else None
        ts = price_ts.get(sym)
        price_rows[sym] = {
            "price": _num(px), "prev_close": _num(pcl),
            "chg_pct": _num((px / pcl - 1) * 100 if px and pcl else None, 3),
            "bar": ts.astimezone(IST).strftime("%H:%M") if isinstance(ts, datetime) else None}

    same_day = pf.get("day_costs_date") == sd
    lb_income = _pos_num(pf.get("liquidbees_income_today")) if same_day else 0.0
    day_costs = _pos_num(pf.get("day_costs")) if same_day else 0.0
    realised = sum(_pos_num(t.get("pnl")) for t in pf.get("trades") or []
                   if t.get("side") == "SELL" and str(t.get("ts", ""))[:10] == sd)
    today_pnl = total - pc_val
    other = today_pnl - (parts_pos + lb_income + realised - day_costs)

    hhmm = now.strftime("%H:%M")
    pts = add_point(series, hhmm, total) if market_open or not series else [list(r[:2]) for r in series]
    rows = [[t, _num(v), _num(v - pc_val)] for t, v in pts]

    d = {
        "v": SCHEMA, "ts": now.isoformat(timespec="seconds"), "session_date": sd,
        "market_open": bool(market_open), "feed_ok": bool(feed_ok),
        "start_capital": _num(start_capital), "prev_close_date": pc_date, "prev_close_value": _num(pc_val),
        "total_value": _num(total),
        "today": {"pnl": _num(today_pnl), "pct": _num(today_pnl / pc_val * 100 if pc_val else None, 3),
                  "parts": {"positions": _num(parts_pos), "liquidbees": _num(lb_income), "realised": _num(realised),
                            "costs": _num(-day_costs), "other": _num(other)}},
        "open": {"pnl": _num(open_sum), "pct": _num(open_sum / inv_sum * 100 if inv_sum else None, 3),
                 "invested": _num(inv_sum)},
        "total": {"pnl": _num(total - start_capital),
                  "pct": _num((total - start_capital) / start_capital * 100 if start_capital else None, 3)},
        "prices": price_rows,
        "positions": positions,
        "liquidbees": {"value": _num(_pos_num(pf.get("liquidbees"))), "accrued_today": _num(lb_income)},
        "cash": _num(_pos_num(pf.get("cash"))),
        "series": decimate(rows),
    }
    return _clean(d)


def dumps(d: dict) -> bytes:
    return json.dumps(d, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def write_local(path: Path, payload: bytes) -> None:
    """Atomic (tmp + rename) write of the exact bytes that are also pushed to the live branch."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
