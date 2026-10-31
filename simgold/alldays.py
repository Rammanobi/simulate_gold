"""Every trading day in one small JSON file for the dashboard (logs/alldays.json). Read-only maths, never trades.

The dashboard page ("Money map") draws everything from this file: the calendar of days, the allocation of any day, the
5-minute chart with trade markers, holdings at close and every trade. It is rebuilt from the engine's own logs:
logs/dashboard-DATE.json (close value, decisions), logs/ticks-DATE.csv (prices, 5-minute chart), the trade ledger
(state/portfolio.json + logs/trades-archive.jsonl + decision logs, via simgold.holdings.read_trades).

Rules (each one is tested in tests/test_alldays.py):
  * day close = the day's closing portfolio value; day profit = close - previous close (first day: starting capital).
  * holdings are valued at the close price (units x close price), so  GOLDBEES + SILVERBEES + HINDCOPPER + Cash == close
    exactly. Cash is the rest (it also holds the tiny exit-cost allowance in the engine's value). In the LIQUIDBEES era
    (before capital.idle: cash, 22 Oct 2026) the idle money counted as Cash too; the day says so in `idle_instrument`.
  * trades: exact price, units, costs and profit from the ledger. BUY and SELL carry a pair id (FIFO lots per fund):
    a BUY gets its own lot id as `pair`, a SELL gets the id of the first lot it takes units from.
  * only NSE trading days appear (no weekends, no holidays); a day with no ticks and no trades is still a valid day.
  * `chg_pct` of a fund = close price vs the previous day's close price.

Shape: {v, start_capital, as_of, idle, days[], trades_all[], now{}}. See build() for the day record.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import date as _date
from pathlib import Path

from simgold import config, holdings as holdings_mod
from simgold.market import is_trading_day, now_ist

log = logging.getLogger(__name__)

SCHEMA = 1
SYMS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")
STEP_MIN = 5                      # intraday chart resolution
OUT_NAME = "alldays.json"
REASON_CHARS = 200
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}")


def _r(x, nd=2):
    try:
        return None if x is None or x == "" else round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def start_capital() -> float:
    try:
        return float(config.load_params()["capital"]["start"])
    except Exception:  # noqa: BLE001 - the dashboard must not depend on the config shape
        return 100000.0


# ------------------------------------------------------------------ sources
def read_ticks(path: Path) -> list[tuple[str, float | None, dict]]:
    """logs/ticks-DATE.csv -> [(HH:MM, value, {symbol: price or None})]. Missing file or bad rows -> skipped."""
    rows = []
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                t = (r.get("time") or "").strip()
                if not _TIME_RE.match(t):
                    continue
                rows.append((t[:5], _r(r.get("value")), {s: _r(r.get(s), 4) for s in SYMS}))
    except OSError:
        return []
    return rows


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def downsample(rows) -> list[list]:
    """First tick of every 5-minute clock bucket, plus the last tick of the day."""
    out, last = [], None
    for t, v, *_ in rows:
        if v is None:
            continue
        b = _minutes(t) // STEP_MIN
        if b != last:
            out.append([t, _r(v)])
            last = b
    for t, v, *_ in reversed(rows):
        if v is not None:
            if not out or out[-1][0] != t:
                out.append([t, _r(v)])
            break
    return out


def _tick_prices(rows) -> dict:
    out = {}
    for s in SYMS:
        vals = [p[s] for _, _, p in rows if p.get(s)]
        out[s] = (vals[0], vals[-1]) if vals else (None, None)
    return out


# ------------------------------------------------------------------ trades
def pair_trades(trades: list[dict]) -> list[dict]:
    """Add id / pair / status to ledger entries (oldest first). FIFO lots per fund: a BUY opens lot P<n>; a SELL takes units
    from the oldest open lots and carries the id of the first one. A BUY ends with status open, part (partly sold) or closed."""
    lots: dict[str, list[list]] = {}
    info: dict[str, dict] = {}
    out, n_lot = [], 0
    for i, t in enumerate(trades, 1):
        e = dict(t)
        e["id"] = f"T{i}"
        e["pair"] = None
        q = lots.setdefault(e["symbol"], [])
        if e["side"] == "BUY":
            n_lot += 1
            e["pair"] = f"P{n_lot}"
            q.append([e["pair"], e["units"]])
            info[e["pair"]] = {"units": e["units"], "left": e["units"], "row": e}
        else:
            need = e["units"]
            while need > 0 and q:
                lot = q[0]
                take = min(need, lot[1])
                e["pair"] = e["pair"] or lot[0]
                lot[1] -= take
                info[lot[0]]["left"] -= take
                need -= take
                if lot[1] == 0:
                    q.pop(0)
        out.append(e)
    for meta in info.values():
        left, units = meta["left"], meta["units"]
        meta["row"]["status"] = "closed" if left == 0 else ("open" if left == units else "part")
    return out


def _trade_row(e: dict, with_date: bool) -> dict:
    row = {"id": e["id"], "pair": e["pair"], "time": e["time"], "symbol": e["symbol"], "side": e["side"], "qty": e["units"],
           "price": _r(e["price"], 4), "amount": _r(e["amount"]), "costs": _r(e.get("costs") or 0.0),
           "profit": _r(e.get("pnl")) if e["side"] == "SELL" else None, "reason": str(e.get("reason") or "")[:REASON_CHARS]}
    if e["side"] == "BUY":
        row["status"] = e.get("status", "open")
    if with_date:
        row = {"date": e["date"], **row}
    return row


# ------------------------------------------------------------------ one day
def _holdings_rows(rows: list[dict]) -> list[dict]:
    out = []
    for h in rows:
        if h["symbol"] not in SYMS or not h.get("units"):
            continue
        out.append({"symbol": h["symbol"], "qty": int(h["units"]), "avg_price": _r(h["avg_price"], 4),
                    "cost": _r(h["invested"]), "price": _r(h["price"], 4), "value": _r(h["value"]),
                    "profit": _r(h["gain"]), "profit_pct": _r(h["gain_pct"])})
    return out


def _counts(d: dict) -> dict:
    keys = ("BUY", "SELL", "BLOCK", "SKIP", "HOLD")
    c = d.get("counts")
    if isinstance(c, dict):
        return {k: int(c.get(k) or 0) for k in keys}
    out = dict.fromkeys(keys, 0)
    for x in d.get("decisions") or []:
        if isinstance(x, dict) and x.get("action") in out:
            out[x["action"]] += 1
    return out


def headline(day_profit: float, trades: list[dict], counts: dict, close: float) -> str:
    pnl = f"day profit {'+' if day_profit >= 0 else '-'}₹{abs(day_profit):,.0f}"
    if trades:
        verbs = [f"{'Bought' if t['side'] == 'BUY' else 'Sold'} {t['qty']} {t['symbol']} at ₹{t['price']:,.2f}" for t in trades]
        return "; ".join(verbs) + f"; {pnl}."
    b = counts.get("BLOCK", 0)
    return f"No trades; {pnl}" + (f"; {b} buy block{'s' if b != 1 else ''}" if b else "") + f"; value ₹{close:,.0f}."


def build_day(date: str, doc: dict, root: Path, ledger: list[dict], prev_close: float, prev_px: dict, start: float) -> dict:
    """One day record from its dashboard document, tick file and the (paired) ledger. See the module docstring."""
    ticks = read_ticks(root / "logs" / f"ticks-{date}.csv")
    tp = _tick_prices(ticks)
    close = _r(doc.get("close"))
    if close is None:
        close = prev_close
    day_trades = [e for e in ledger if e["date"] == date]
    # closing price per fund: the dashboard's own row (the price the close value was computed on), else the last tick, else the last fill
    doc_px = {h.get("symbol"): _r(h.get("price"), 4) for h in doc.get("holdings") or [] if isinstance(h, dict)}
    px = {s: doc_px.get(s) or tp[s][1] for s in SYMS}
    rows = holdings_mod.compute(ledger, {s: v for s, v in px.items() if v}, 0.0, upto=date, idle_cash=True)
    held = _holdings_rows(rows)
    for h in held:
        px[h["symbol"]] = px.get(h["symbol"]) or h["price"]
    alloc = {s: 0.0 for s in SYMS}
    for h in held:
        alloc[h["symbol"]] = h["value"]
    invested = _r(sum(alloc.values()))
    alloc["Cash"] = _r(close - invested)
    prices = {}
    for s in SYMS:
        o, c = tp[s][0], px.get(s)
        pc = prev_px.get(s)
        prices[s] = {"open": _r(o, 4), "close": _r(c, 4), "chg_pct": _r((c / pc - 1) * 100) if c and pc else None}
    day_profit, overall = close - prev_close, close - start
    counts = _counts(doc)
    intraday = downsample(ticks)
    if not intraday:
        intraday = [[str(t[0])[:5], _r(t[1])] for t in doc.get("ticks") or [] if isinstance(t, list) and len(t) > 1 and t[1] is not None
                    and _TIME_RE.match(str(t[0]))]
    tr = [_trade_row(e, False) for e in day_trades]
    return {
        "date": date, "weekday": _date.fromisoformat(date).strftime("%A"),
        "open_value": _r(prev_close), "close_value": close,
        "day_profit": _r(day_profit), "day_profit_pct": _r(day_profit / prev_close * 100, 3) if prev_close else 0.0,
        "overall_profit": _r(overall), "overall_profit_pct": _r(overall / start * 100, 3),
        "invested_value": invested, "cash_value": alloc["Cash"], "alloc": alloc,
        "holdings": held, "trades": tr, "prices": prices, "intraday": intraday,
        "decisions_count": counts, "headline": headline(day_profit, tr, counts, close),
        "idle_instrument": str(doc.get("idle") or "liquidbees"),
    }


# ------------------------------------------------------------------ everything
def _dashboards(root: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((root / "logs").glob("dashboard-*.json")):
        d = _load(p)
        day = p.stem.replace("dashboard-", "")
        if isinstance(d, dict) and _DATE_RE.match(day):
            try:
                if is_trading_day(_date.fromisoformat(day)):
                    out[day] = d
            except ValueError:
                continue
    return out


def build(root: Path) -> dict:
    root = Path(root)
    start = start_capital()
    docs = _dashboards(root)
    ledger = pair_trades(holdings_mod.read_trades(root))
    days, prev_close, prev_px = [], start, {}
    for date in sorted(docs):
        rec = build_day(date, docs[date], root, ledger, prev_close, prev_px, start)
        days.append(rec)
        prev_close = rec["close_value"]
        prev_px = {s: v["close"] for s, v in rec["prices"].items() if v["close"]} or prev_px
    shown = {d["date"] for d in days}
    trades_all = [_trade_row(e, True) for e in ledger if e["date"] in shown]
    live = _load(root / "state" / "live_now.json")
    last = days[-1] if days else None
    as_of = (live or {}).get("ts") or (docs[last["date"]].get("updated") if last else None)
    now = None
    if last:
        now = {"as_of": as_of, "date": last["date"], "close_value": last["close_value"], "invested_value": last["invested_value"],
               "cash_value": last["cash_value"], "alloc": last["alloc"], "holdings": last["holdings"],
               "prices": {s: v["close"] for s, v in last["prices"].items()}, "day_profit": last["day_profit"],
               "idle_instrument": last["idle_instrument"]}
    return {"v": SCHEMA, "start_capital": start, "as_of": as_of, "idle": "cash" if config.idle_is_cash() else "liquidbees",
            "days": days, "trades_all": trades_all, "now": now}


def dumps(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"


def write(root: Path, out: Path | None = None) -> Path:
    """Write logs/alldays.json atomically (or `out`) and return the path."""
    path = Path(out) if out else Path(root) / "logs" / OUT_NAME
    text = dumps(build(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def safe_write(root: Path) -> bool:
    """For the engine: never raises, never leaves a half-written file."""
    try:
        write(root)
        return True
    except Exception:  # noqa: BLE001 - the dashboard must never stop trading
        log.warning("alldays: could not write %s", OUT_NAME, exc_info=True)
        return False


# ------------------------------------------------------------------ the artifact page
TEMPLATE = Path(__file__).resolve().parent.parent / "docs" / "dashboard" / "template.html"


def render_page(data: dict, template: str | None = None) -> str:
    """The dashboard page with `__DATA__` replaced by the alldays JSON (so it draws without any network)."""
    html = template if template is not None else TEMPLATE.read_text(encoding="utf-8")
    if html.count("__DATA__") != 1:
        raise ValueError("template must contain __DATA__ exactly once")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    return html.replace("__DATA__", blob)
