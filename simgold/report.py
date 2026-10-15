"""End-of-day report generator for the paper-trading simulator.

Reads the engine's logs/state files (all defensively), builds one day dict in the
shape of templates/sample_day.json, and renders it as an email (inline-CSS tables),
a plain-text mail, and the interactive page. Paper trading only; nothing here
touches a broker.
"""
from __future__ import annotations

import csv
import datetime as dt
import html as _html
import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from simgold import budget
from simgold.inboxlink import INBOX_LABEL, daily_item_id, inbox_link

START_CAPITAL = 100_000.0
LOCKED_FLOOR = 100_000.0
INITIAL_FLOOR = 99_500.0
LOCK_PROFIT = 500.0
RATCHET_STEP = 1_000.0       # best profit step used for the "next step" marker
LIQUIDBEES_YIELD = 0.063     # rulebook carry assumption, used only when no figure is logged
GREEN, RED, GREY, INK = "#1a7f4b", "#c0392b", "#6b7280", "#1f2937"

# NSE equity holidays. Weekends are detected separately. Extra dates can be added in
# config/holidays.txt ("YYYY-MM-DD optional name"). Check against the NSE circular.
NSE_HOLIDAYS = {
    "2026-01-26": "Republic Day", "2026-03-03": "Holi", "2026-03-26": "Ram Navami",
    "2026-03-31": "Mahavir Jayanti", "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti", "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id", "2026-06-26": "Muharram", "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti", "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada", "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}
NEUTRAL_MARKETS = ("dollar index", "usd/inr", "dxy", "usdinr")
ETF_NAMES = {"GOLDBEES": "gold", "SILVERBEES": "silver", "HINDCOPPER": "copper"}
SAMPLE_INBOX_URL = "https://claude.ai/artifact/23b6gaJRa2bT4wNyQCKMvh"  # sample templates only
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


# ---------------------------------------------------------------- formatting
def _round(n: float, places: int = 0) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return Decimal(repr(float(n))).quantize(q, rounding=ROUND_HALF_UP)


def inr(n: float, places: int = 0) -> str:
    """Indian grouping, rupee sign, no sign character: 100147 -> ₹1,00,147."""
    d = abs(_round(n, places))
    whole, _, frac = f"{d:.{places}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return "₹" + whole + ("." + frac if places else "")


def num_in(n: float) -> str:
    return inr(n)[1:]


def sg(n: float, places: int = 0) -> str:
    """Signed rupees using a real minus sign: +₹147 / −₹44 / ₹0."""
    r = _round(n, places)
    return ("+" if r > 0 else "−" if r < 0 else "") + inr(n, places)


def pc(n: float, places: int = 1) -> str:
    r = _round(n, places)
    return ("+" if r > 0 else "−" if r < 0 else "") + f"{abs(r):.{places}f}%"


def esc(s) -> str:
    return _html.escape(str(s), quote=False)


def _tone_color(n: float) -> str:
    r = _round(n)
    return GREEN if r > 0 else RED if r < 0 else INK


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def _num(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _hhmm(s) -> str | None:
    m = re.search(r"(\d{1,2}):(\d{2})", str(s or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _norm_symbol(s) -> str:
    s = re.sub(r"\.(NS|BO)$", "", str(s or "").strip().upper())
    return "All metals" if s == "ALL" else s


# ------------------------------------------------------------------ loading
def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_decisions(path: Path) -> list[dict] | None:
    """Return decisions, or None when the log file is missing."""
    if not path.exists():
        return None
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("action"):
            out.append(row)
    return out


def load_ticks(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    out = []
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                t = _hhmm(row.get("time"))
                v = _num(row.get("value"))
                if t and v is not None:
                    out.append({"t": t, "value": v, "floor": _num(row.get("floor")),
                                "cash": _num(row.get("cash")),
                                "liquidbees": _num(row.get("liquidbees"))})
    except (OSError, csv.Error):
        return None
    out.sort(key=lambda r: _mins(r["t"]))
    return out


def load_equity(path: Path) -> list[tuple[str, float]]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            a, _, b = line.partition(",")
            v = _num(b)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.strip()) and v is not None:
                rows.append((a.strip(), v))
    except OSError:
        pass
    rows.sort()
    return rows


def _holidays(root: Path) -> dict[str, str]:
    out = dict(NSE_HOLIDAYS)
    try:
        for line in (root / "config" / "holidays.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                d, _, name = line.partition(" ")
                out[d] = name.strip() or "Market holiday"
    except OSError:
        pass
    return out


def market_closed_reason(date: str, root: Path, override: str | None = None) -> str | None:
    if override:
        return override
    d = dt.date.fromisoformat(date)
    if d.weekday() >= 5:
        return d.strftime("%A")
    return _holidays(root).get(date)


# ----------------------------------------------------------------- building
def trade_cost_estimate(value: float, side: str, kind: str) -> float:
    brokerage = min(value * 0.0003, 20.0)
    exchange = value * 0.0000345
    gst = (brokerage + exchange) * 0.18
    stamp = value * 0.00015 if side == "BUY" else 0.0
    stt = value * 0.001 if kind == "equity" else 0.0
    return brokerage + exchange + gst + stamp + stt


def _clean_decision(r: dict) -> dict:
    action = str(r.get("action", "")).upper()
    t = _hhmm(r.get("time")) or _hhmm(r.get("ts")) or "00:00"
    d = {
        "time": t, "action": action, "symbol": _norm_symbol(r.get("symbol")),
        "qty": int(_num(r.get("qty"), 0) or 0), "price": _num(r.get("price"), 0.0) or 0.0,
        "amount": _num(r.get("amount"), 0.0) or 0.0,
        "pnl": _num(r.get("pnl")) if action == "SELL" else None,
        "reason": str(r.get("reason") or "").strip() or "No reason was logged.",
        "trigger": str(r.get("trigger") or "").strip() or "Engine",
    }
    if action == "BUY":
        for k in ("stop", "risk"):
            v = _num(r.get(k))
            if v is not None:
                d[k] = v
    return d


def _lowfirst(s: str) -> str:
    return s if len(s) > 1 and (s[1].isupper() or s[1].isdigit()) else s[:1].lower() + s[1:]


def no_trade_note(decisions: list[dict], have_log: bool) -> str:
    reasons = []
    for d in decisions:
        if d["action"] in ("SKIP", "BLOCK") and d["reason"] not in reasons:
            reasons.append(d["reason"])
    if reasons:
        shown = [r.rstrip(". ") for r in reasons[:3]]
        more = f" (and {len(reasons) - 3} more)" if len(reasons) > 3 else ""
        body = "; ".join(_lowfirst(r) for r in shown)
        return f"No trades today. The system waited because {body}{more}."
    if not have_log:
        return "No trades today. No decision log was found for this date, so the system may not have run."
    return "No trades today. The system waited because no entry met its rules, so the money stayed in LIQUIDBEES."


def _downsample(ticks: list[dict], step: int = 30) -> list[list]:
    if not ticks:
        return []
    first, last = _mins(ticks[0]["t"]), _mins(ticks[-1]["t"])
    picked = {first: ticks[0]["value"]}
    target = (first // step + 1) * step
    while target < last:
        prior = [t for t in ticks if _mins(t["t"]) <= target]
        if prior:
            picked[_mins(prior[-1]["t"])] = prior[-1]["value"]
        target += step
    picked[last] = ticks[-1]["value"]
    return [[f"{m // 60:02d}:{m % 60:02d}", round(v)] for m, v in sorted(picked.items())]


def _month_start(portfolio: dict, equity: list[tuple[str, float]], date: str) -> float:
    if str(portfolio.get("month", "")) == date[:7]:
        v = _num(portfolio.get("month_start_equity"))
        if v:
            return v
    prior = [v for d, v in equity if d < date[:7] + "-01"]
    if prior:
        return prior[-1]
    return _num(portfolio.get("month_start_equity")) or START_CAPITAL


def _floor_ladder(floor: float, best_profit: float) -> tuple[float, float, str]:
    """Return (next_value, floor_after, sentence) for the Green Guard rule book."""
    if floor < LOCKED_FLOOR:
        nv, nf = START_CAPITAL + LOCK_PROFIT, LOCKED_FLOOR
        nprofit = LOCK_PROFIT
    else:
        nprofit = (int(max(best_profit - LOCK_PROFIT, 0) // RATCHET_STEP) + 1) * RATCHET_STEP + LOCK_PROFIT
        nv, nf = START_CAPITAL + nprofit, LOCKED_FLOOR + 0.5 * (nprofit - LOCK_PROFIT)
    return nv, nf, f"Floor moves to {inr(nf)} once profit reaches {inr(nprofit)}"


def _market_from_decisions(decisions: list[dict]) -> list[dict]:
    seen: dict[str, list[float]] = {}
    for d in decisions:
        if d["symbol"] in ETF_NAMES and d["price"] > 0:
            seen.setdefault(d["symbol"], []).append(d["price"])
    out = []
    for sym, prices in seen.items():
        if len(prices) >= 2 and prices[0]:
            out.append({"name": f"{sym} (NSE)", "move_pct": round((prices[-1] / prices[0] - 1) * 100, 2),
                        "note": f"{inr(prices[0], 2)} to {inr(prices[-1], 2)} between the first and last logged price"})
    return out


def fixed_today(date: str, root: Path, limit: int = 4) -> list[str]:
    """Incidents opened on `date` that the routine fixed itself (a safe-fix action), one short line each."""
    out = []
    for f in sorted((Path(root) / "incidents").glob(f"{date}-*.json")):
        rec = _read_json(f, None)
        if not isinstance(rec, dict):
            continue
        fixes = [a.get("text", "") for a in rec.get("actions", []) if a.get("kind") == "safe-fix"]
        if fixes:
            out.append(f"{rec.get('title', f.stem)}: {fixes[0]}")
    return out[:limit] + ([f"and {len(out) - limit} more"] if len(out) > limit else [])


def system_health(date: str, root: Path) -> str:
    """One line from logs/health-DATE.jsonl, which tools/healthcheck.py appends to on every run."""
    f = Path(root) / "logs" / f"health-{date}.jsonl"
    runs, bad = 0, set()
    try:
        for ln in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            runs += 1
            bad |= {p.get("check") for p in r.get("results", [])}
    except OSError:
        return ""
    if not runs:
        return ""
    if not bad:
        return f"Watchdog: {runs} checks today, all green"
    return f"Watchdog: {runs} checks today, {len(bad)} {'problem' if len(bad) == 1 else 'problems'}, see incidents"


# ----------------------------------------------------------- day at a glance
MARKET_OPEN = "09:15"
LATE_START_MIN = 20          # engine counts as "started late" this many minutes after the open
GLANCE_SYMBOLS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")
TRIGGER_LABELS = {"news trigger": "news", "price shock": "price shock"}


def _glance_engine(date: str, root: Path) -> dict | None:
    """First/last minute and count of the engine's checks (logs/checks-DATE.jsonl, else ticks rows)."""
    times, source = [], "checks"
    try:
        for ln in (root / "logs" / f"checks-{date}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            t = (_hhmm(r.get("time")) or _hhmm(r.get("ts"))) if isinstance(r, dict) else None
            if t:
                times.append(t)
    except OSError:
        times = []
    if not times:
        source = "ticks"
        times = [t["t"] for t in (load_ticks(root / "logs" / f"ticks-{date}.csv") or [])]
    if not times:
        return None
    times.sort(key=_mins)
    late = _mins(times[0]) - _mins(MARKET_OPEN) > LATE_START_MIN
    return {"first": times[0], "last": times[-1], "count": len(times), "source": source, "late": late}


def _glance_instruments(date: str, root: Path) -> list[dict]:
    """Open/close/low/high per metal from the price columns of logs/ticks-DATE.csv."""
    seen: dict[str, list[tuple[int, float]]] = {}
    try:
        with (root / "logs" / f"ticks-{date}.csv").open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                t = _hhmm(row.get("time"))
                if not t:
                    continue
                for sym in GLANCE_SYMBOLS:
                    v = _num(row.get(sym))
                    if v is not None and v > 0:
                        seen.setdefault(sym, []).append((_mins(t), v))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []
    out = []
    for sym in GLANCE_SYMBOLS:
        pts = sorted(seen.get(sym, []), key=lambda p: p[0])
        if not pts:
            continue
        o, c = pts[0][1], pts[-1][1]
        vals = [v for _, v in pts]
        out.append({"symbol": sym, "open": o, "close": c, "change_pct": round((c / o - 1) * 100, 2),
                    "low": min(vals), "high": max(vals)})
    return out


def _glance_decisions(decisions: list[dict]) -> dict:
    n = lambda a: sum(1 for d in decisions if d.get("action") == a)
    by: dict[str, int] = {}
    for d in decisions:
        if d.get("action") == "BLOCK":
            trig = str(d.get("trigger") or "Engine").strip()
            label = TRIGGER_LABELS.get(trig.lower(), _lowfirst(trig))
            by[label] = by.get(label, 0) + 1
    buys, sells = n("BUY"), n("SELL")
    return {"allowed": buys + sells, "buys": buys, "sells": sells, "blocked": n("BLOCK"),
            "blocked_by": dict(sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))),
            "skipped": n("SKIP"), "held": n("HOLD")}


def _glance_money(day: dict) -> dict:
    p = day.get("portfolio", {})
    parts = [(h["symbol"], _num(h.get("value"), 0.0) or 0.0) for h in day.get("holdings_close", [])]
    parts.append(("LIQUIDBEES (cash)", _num(p.get("cash_in_liquidbees"), 0.0) or 0.0))
    total = sum(v for _, v in parts)
    split = [{"name": name, "pct": round(v / total * 100)} for name, v in parts if v > 0] if total > 0 else []
    return {"value": p.get("close"), "day_pnl": p.get("day_pnl"), "split": split}


def day_glance(date: str, root: Path, day: dict) -> dict:
    """The 'Day at a glance' data, built only from repo files and the day dict. Missing pieces are left out."""
    root = Path(root)
    g: dict = {}
    for key, fn in (("engine", lambda: _glance_engine(date, root)),
                    ("decisions", lambda: _glance_decisions(day.get("decisions", []))),
                    ("instruments", lambda: _glance_instruments(date, root)),
                    ("money", lambda: _glance_money(day))):
        try:
            v = fn()
        except Exception:      # a broken log line must never stop the report
            v = None
        if v:
            g[key] = v
    if day.get("no_trade_note"):
        g["why_no_trades"] = _short_note(day["no_trade_note"])
    return g


def _short_note(note: str, limit: int = 180) -> str:
    """The no-trade note, cut to its first reason when it is long (the full list stays under 'What I did')."""
    if len(note) <= limit:
        return note
    head = note.split("; ", 1)[0].rstrip(". ")
    if len(head) > limit:
        head = head[:limit].rsplit(" ", 1)[0].rstrip(".,;: ")
    return head + "; more reasons under What I did."


def glance_lines(day: dict) -> list[tuple[str, str]]:
    """(label, text) rows of the 'Day at a glance' section; shared by the email and the text mail."""
    g = day.get("glance") or {}
    out = []
    e = g.get("engine")
    if e:
        what = "minute checks" if e.get("source") == "checks" else "minute ticks logged"
        out.append(("Engine", f"Ran {e['first']}–{e['last']} IST · {e['count']} {what}"
                              + (" (started late)" if e.get("late") else "")))
    d = g.get("decisions")
    if d:
        blocked = f"Blocked {d['blocked']}"
        if d.get("blocked_by"):
            blocked += " (" + ", ".join(f"{k} {v}" for k, v in d["blocked_by"].items()) + ")"
        out.append(("Decisions", f"Allowed {d['allowed']} (buys {d['buys']}, sells {d['sells']}) · {blocked}"
                                 f" · Skipped {d['skipped']} · Held {d['held']}"))
    for i in g.get("instruments") or []:
        out.append((i["symbol"], f"{inr(i['open'], 2)} → {inr(i['close'], 2)} · {pc(i['change_pct'], 2)}"
                                 f" · low {inr(i['low'], 2)} – high {inr(i['high'], 2)}"))
    m = g.get("money")
    if m and m.get("value") is not None:
        s = f"Value {inr(m['value'], 2)}"
        if m.get("day_pnl") is not None:
            s += f" · day {sg(m['day_pnl'], 2)}"
        split = m.get("split") or []
        if len(split) == 1:
            s += f" · 100% in {split[0]['name']}"
        elif split:
            s += " · " + ", ".join(f"{x['pct']}% {x['name']}" for x in split)
        out.append(("Money", s))
    if g.get("why_no_trades"):
        out.append(("Why no trades", g["why_no_trades"]))
    return out


GLANCE_LINK_TEXT = "Every decision, every minute and the market price at each decision: open the dashboard"


def build_day(date: str, root: Path, holiday: str | None = None) -> dict:
    day = _build_day(date, root, holiday)
    if not day.get("closed"):
        day["glance"] = day_glance(date, root, day)
    day["fixed_today"] = fixed_today(date, root)
    day["system_health"] = system_health(date, root)
    day["thrift"] = budget.is_thrift(Path(root) / "state")
    day["fallback_dates"] = fallback_dates(root)
    return day


THRIFT_LINE = "\U0001F4A1 Thrift mode: running on plan limits"


def fallback_dates(root) -> list[str]:
    """Days the DeepSeek fallback was used (state/fallback.json, written by tools/fallback_brain.py), newest last."""
    try:
        d = json.loads((Path(root) / "state" / "fallback.json").read_text(encoding="utf-8"))
        return [str(x)[:10] for x in (d.get("dates") or [])][-5:] if isinstance(d, dict) else []
    except (OSError, ValueError):
        return []


def _notes(day: dict) -> list[str]:
    out = []
    if day.get("thrift"):
        out.append(THRIFT_LINE)
    if day.get("fallback_dates"):
        out.append("Fallback was used on " + ", ".join(day["fallback_dates"]) + " (Claude routines were not running).")
    return out


def _ops_html(day: dict) -> str:
    """Small 'Fixed today' list and the watchdog line; empty (hidden) when there is nothing to say."""
    fixed, health = day.get("fixed_today") or [], day.get("system_health") or ""
    notes = _notes(day)
    if not fixed and not health and not notes:
        return ""
    h = ""
    for n in notes:
        h += (f'<tr><td style="padding:14px 20px 0 20px;font-family:{FONT};font-size:13px;line-height:19px;color:#6b7280;">'
              f'{esc(n)}</td></tr>\n')
    if fixed:
        h += _section("Fixed today") + "".join(
            f'<tr><td style="padding:2px 20px;font-family:{FONT};font-size:14px;line-height:21px;color:#1f2937;">&#10003; {esc(x)}</td></tr>\n' for x in fixed)
    if health:
        h += (f'<tr><td style="padding:{10 if fixed else 22}px 20px 0 20px;font-family:{FONT};font-size:13px;line-height:19px;color:#6b7280;">'
              f'{esc(health)}</td></tr>\n')
    return h


def _ops_text(day: dict) -> list[str]:
    L = []
    for n in _notes(day):
        L += [n, ""]
    if day.get("fixed_today"):
        L += ["FIXED TODAY"] + [f"- {x}" for x in day["fixed_today"]] + [""]
    if day.get("system_health"):
        L += [day["system_health"], ""]
    return L


def _build_day(date: str, root: Path, holiday: str | None = None) -> dict:
    root = Path(root)
    d0 = dt.date.fromisoformat(date)
    weekday = d0.strftime("%A")
    portfolio = _read_json(root / "state" / "portfolio.json", {}) or {}
    equity = load_equity(root / "state" / "equity.csv")
    tomorrow_path = root / "state" / "tomorrow.txt"
    try:
        tomorrow = tomorrow_path.read_text(encoding="utf-8").strip()
    except OSError:
        tomorrow = ""
    warnings: list[str] = []

    closed = market_closed_reason(date, root, holiday)
    if closed:
        prior = [v for d, v in equity if d <= date]
        close = prior[-1] if prior else (_num(portfolio.get("equity")) or START_CAPITAL)
        floor = _num(portfolio.get("floor"), INITIAL_FLOOR)
        msg = f"NSE is closed today ({closed}). No trades were made and the system did nothing."
        return {
            "date": date, "weekday": weekday, "closed": True, "holiday_name": closed,
            "start_capital": START_CAPITAL,
            "portfolio": {"open": close, "close": close, "day_pnl": 0, "day_pnl_pct": 0,
                          "month_pnl": close - _month_start(portfolio, equity, date),
                          "month_pnl_pct": 0, "floor": floor, "floor_next_step": "",
                          "distance_to_floor": close - floor, "cash_in_liquidbees": 0,
                          "liquidbees_income": 0, "costs_paid": 0, "next_value": 0},
            "pnl_breakdown": {"realized_trades": 0, "unrealized_open": 0, "liquidbees_income": 0},
            "market": [], "holdings_close": [], "decisions": [],
            "counts": {"buys": 0, "sells": 0, "skips": 0, "blocks": 0, "holds": 0, "fast_lane_runs": 0},
            "intraday": [["09:15", round(close)], ["15:30", round(close)]],
            "tomorrow": tomorrow or "Trading resumes at the next market open.",
            "one_line_summary": msg, "warnings": [],
        }

    raw = load_decisions(root / "logs" / f"decisions-{date}.jsonl")
    ticks = load_ticks(root / "logs" / f"ticks-{date}.csv")
    decisions = [_clean_decision(r) for r in (raw or [])]
    decisions.sort(key=lambda d: _mins(d["time"]))
    if raw is None:
        warnings.append("No decision log was found for this date.")
    if ticks is None:
        warnings.append("The ticks file was missing, so the intraday chart uses decision times only.")
    ticks = ticks or []

    # --- values
    prior_eq = [v for d, v in equity if d < date]
    today_eq = dict(equity).get(date)
    last_dec_val = next((_num(r.get("value_after")) for r in reversed(raw or []) if _num(r.get("value_after"))), None)
    if ticks:
        close = ticks[-1]["value"]
    elif today_eq is not None:
        close = today_eq
    elif last_dec_val is not None:
        close = last_dec_val
    else:
        close = (prior_eq[-1] if prior_eq else _num(portfolio.get("equity")) or START_CAPITAL)
    if prior_eq:
        open_ = prior_eq[-1]
    elif ticks:
        open_ = ticks[0]["value"]
    else:
        open_ = START_CAPITAL if close == START_CAPITAL else min(close, START_CAPITAL)
        if not ticks and raw is not None and not raw:
            open_ = close
    pf_open = _num(portfolio.get("day_open_value"))
    if pf_open and portfolio.get("day_costs_date") == date:
        open_ = pf_open                       # engine-recorded value at the day's first tick
    day_pnl = close - open_

    # --- floor
    floor = (ticks[-1]["floor"] if ticks and ticks[-1]["floor"] is not None else None)
    if floor is None:
        floor = next((_num(r.get("floor")) for r in reversed(raw or []) if _num(r.get("floor")) is not None), None)
    if floor is None:
        floor = _num(portfolio.get("floor"), INITIAL_FLOOR)
    best_profit = _num(portfolio.get("best_profit"))
    if best_profit is None:
        best_profit = max([close] + [t["value"] for t in ticks]) - START_CAPITAL
    next_value, floor_after, next_step = _floor_ladder(floor, best_profit)

    # --- month
    ms = _month_start(portfolio, equity, date)
    month_pnl = close - ms

    # --- positions at close
    last_price: dict[str, float] = {}
    for d in decisions:
        if d["price"] > 0:
            last_price[d["symbol"]] = d["price"]
    holdings = []
    for sym, p in (portfolio.get("positions") or {}).items():
        if not isinstance(p, dict):
            continue
        qty = int(_num(p.get("qty"), 0) or 0)
        if qty <= 0:
            continue
        s = _norm_symbol(sym)
        avg = _num(p.get("avg"), None)
        if avg is None:
            avg = _num(p.get("avg_cost"), 0.0)
        px = last_price.get(s) or _num(p.get("last")) or avg
        h = {"symbol": s, "qty": qty, "avg": avg, "close": px, "value": round(qty * px, 2),
             "unrealized": round(qty * (px - avg), 2)}
        stop = _num(p.get("stop"))
        h["stop"] = stop if stop is not None else 0.0
        holdings.append(h)

    # --- P&L breakdown
    realized = sum(d["pnl"] for d in decisions if d["action"] == "SELL" and d["pnl"] is not None)
    unrealized = sum(h["unrealized"] for h in holdings)

    costs, costs_estimated = None, False
    pf_costs = _num(portfolio.get("day_costs"), _num(portfolio.get("costs_today")))
    if pf_costs is not None and portfolio.get("day_costs_date", date) == date:
        costs = pf_costs
    if costs is None:
        matched = [t for t in (portfolio.get("trades") or []) if isinstance(t, dict)
                   and any(str(t.get(k, "")).startswith(date) for k in ("ts", "date", "time"))
                   and _num(t.get("costs"), _num(t.get("cost"))) is not None]
        if matched:
            costs = sum(_num(t.get("costs"), _num(t.get("cost"))) for t in matched)
    if costs is None:
        logged = [_num(r.get("costs"), _num(r.get("cost"))) for r in (raw or []) if r.get("action") in ("BUY", "SELL")]
        if logged and all(v is not None for v in logged):
            costs = sum(logged)
    if costs is None:
        costs = sum(trade_cost_estimate(d["amount"], d["action"], "equity" if d["symbol"] == "HINDCOPPER" else "etf")
                    for d in decisions if d["action"] in ("BUY", "SELL"))
        costs_estimated = bool(costs)

    lb_income = _num(portfolio.get("liquidbees_income_today"), _num(portfolio.get("liquidbees_income_day")))
    lb_estimated = False
    if lb_income is None:
        lbv = next((t["liquidbees"] for t in ticks if t["liquidbees"]), None)
        if lbv is None:
            lbv = _liquidbees_value(portfolio)
        lb_income = (lbv or 0.0) * LIQUIDBEES_YIELD / 365
        lb_estimated = bool(lb_income)

    unrealized_open = unrealized
    # SELL pnl is already NET of fees and taxes, so costs are NOT subtracted again.
    parts = realized + unrealized_open + lb_income
    residual = day_pnl - parts
    if abs(residual) >= 0.5:
        # Overnight moves on carried positions and fill slippage land here, so the rows add up.
        unrealized_open += residual
    breakdown = {"realized_trades": round(realized, 2),
                 "unrealized_open": round(unrealized_open, 2), "liquidbees_income": round(lb_income, 2)}

    # --- cash
    if ticks and ticks[-1]["cash"] is not None:
        cash_lb = ticks[-1]["cash"] + (ticks[-1]["liquidbees"] or 0.0)
    else:
        cash_lb = (_num(portfolio.get("cash"), 0.0) or 0.0) + (_liquidbees_value(portfolio) or 0.0)

    # --- counts
    cnt = {k: sum(1 for d in decisions if d["action"] == a)
           for k, a in (("buys", "BUY"), ("sells", "SELL"), ("skips", "SKIP"), ("blocks", "BLOCK"), ("holds", "HOLD"))}
    cnt["fast_lane_runs"] = sum(1 for d in decisions if "news" in d["trigger"].lower())

    # --- intraday
    intraday = _downsample(ticks)
    if not intraday:
        pts = {"09:15": round(open_)}
        for r in raw or []:
            t, v = _hhmm(r.get("time")) or _hhmm(r.get("ts")), _num(r.get("value_after"))
            if t and v is not None:
                pts[t] = round(v)
        pts["15:30"] = round(close)
        intraday = sorted(pts.items(), key=lambda kv: _mins(kv[0]))
        intraday = [list(p) for p in intraday]
    if len(intraday) < 2 or intraday[0][0] == intraday[-1][0]:
        intraday = [["09:15", round(open_)], ["15:30", round(close)]]

    # --- market
    market = _read_json(root / "state" / "market_today.json", None)
    if isinstance(market, dict):
        market = market.get("market")
    if isinstance(market, list):
        market = [{"name": str(m.get("name", "")), "move_pct": _num(m.get("move_pct"), 0.0),
                   "note": str(m.get("note", ""))} for m in market if isinstance(m, dict) and m.get("name")]
    else:
        market = _market_from_decisions(decisions)

    day_pct = day_pnl / open_ * 100 if open_ else 0.0
    day = {
        "date": date, "weekday": weekday,
        "day_of_month_run": 1 + sum(1 for d, _ in equity if d[:7] == date[:7] and d < date),
        "start_capital": START_CAPITAL,
        "portfolio": {
            "open": round(open_, 2), "close": round(close, 2), "day_pnl": round(day_pnl, 2),
            "day_pnl_pct": round(day_pct, 2), "month_pnl": round(month_pnl, 2),
            "month_pnl_pct": round(month_pnl / ms * 100, 2) if ms else 0.0,
            "floor": floor, "floor_next_step": next_step, "next_value": next_value,
            "floor_after_next": floor_after, "distance_to_floor": round(close - floor, 2),
            "cash_in_liquidbees": round(cash_lb, 2), "liquidbees_income": round(lb_income, 2),
            "costs_paid": round(costs, 2),
        },
        "pnl_breakdown": breakdown, "market": market, "holdings_close": holdings,
        "decisions": decisions, "counts": cnt, "intraday": intraday,
        "tomorrow": tomorrow or "Nothing special is scheduled. The system follows its normal rules at the next open.",
        "warnings": warnings,
    }
    if costs_estimated:
        day["costs_estimated"] = True
    if lb_estimated:
        day["liquidbees_income_estimated"] = True
    if not any(d["action"] in ("BUY", "SELL") for d in decisions):
        day["no_trade_note"] = no_trade_note(decisions, raw is not None)
    day["one_line_summary"] = summarize(day)
    day["subject"] = subject_line(day)
    return day


def _liquidbees_value(portfolio: dict) -> float | None:
    lb = portfolio.get("liquidbees")
    if isinstance(lb, dict):
        v = _num(lb.get("value"))
        if v is None:
            v = (_num(lb.get("qty"), 0.0) or 0.0) * (_num(lb.get("price"), _num(lb.get("avg"), 0.0)) or 0.0)
        return v
    return _num(lb)


def summarize(day: dict) -> str:
    p, d = day["portfolio"], day["decisions"]
    pnl, pct = p["day_pnl"], abs(p["day_pnl_pct"])
    size = "Small" if pct < 0.5 else "Solid" if pct < 1.5 else "Big"
    r = _round(pnl)
    if r == 0:
        head = "Flat day: no change in value"
    else:
        head = f"{size} {'green' if r > 0 else 'red'} day: {sg(pnl)}"
    sells = [x for x in d if x["action"] == "SELL" and x["pnl"] is not None]
    bits = []
    wins = [x for x in sells if _round(x["pnl"]) > 0]
    losses = [x for x in sells if _round(x["pnl"]) < 0]
    if wins:
        bits.append(f"{len(wins)} winning {_plural(len(wins), 'sale')}")
    if losses:
        bits.append(f"{len(losses)} losing {_plural(len(losses), 'sale')} (lost {inr(-sum(x['pnl'] for x in losses))})")
    if not any(x["action"] in ("BUY", "SELL") for x in d):
        bits.append("no trades")
    floor_bit = (f"The floor is {inr(p['distance_to_floor'])} away" if p["distance_to_floor"] > 0
                 else f"Value is {inr(-p['distance_to_floor'])} below the floor")
    sentences = [head] + ([", ".join(bits).capitalize()] if bits else []) + [floor_bit]
    return ". ".join(sentences) + "."


def subject_line(day: dict) -> str:
    d = dt.date.fromisoformat(day["date"])
    label = f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
    if day.get("closed"):
        return f"Metals Daybook · 🏖️ {label} · Market closed"
    pnl = day["portfolio"]["day_pnl"]
    r = _round(pnl)
    if r > 0:
        return f"Metals Daybook · 📈 {label} · {sg(pnl)} (green)"
    if r < 0:
        return f"Metals Daybook · 📉 {label} · {sg(pnl)} (red)"
    return f"Metals Daybook · 📊 {label} · ₹0 (flat)"


# ------------------------------------------------------------------ helpers
def prepare(day: dict) -> dict:
    """Fill derived fields on a day dict (also used for the sample JSON)."""
    for m in day.get("market", []):
        if "neutral" not in m:
            m["neutral"] = any(k in m["name"].lower() for k in NEUTRAL_MARKETS)
    day.setdefault("warnings", [])
    p = day.get("portfolio", {})
    if "next_value" not in p and p.get("floor") is not None and not day.get("closed"):
        bp = max(p.get("close", START_CAPITAL), START_CAPITAL) - START_CAPITAL
        p["next_value"], p["floor_after_next"], _ = _floor_ladder(p["floor"], bp)
    day.setdefault("counts", {})
    for k in ("buys", "sells", "skips", "blocks", "holds", "fast_lane_runs"):
        day["counts"].setdefault(k, 0)
    if not any(x["action"] in ("BUY", "SELL") for x in day.get("decisions", [])) and not day.get("closed"):
        day.setdefault("no_trade_note", no_trade_note(day.get("decisions", []), True))
    return day


def _holding_label(day: dict) -> str:
    h = day["holdings_close"]
    if len(h) == 1:
        word = ETF_NAMES.get(h[0]["symbol"])
        return f"Open {word} position (not sold yet)" if word else f"Open {h[0]['symbol']} position (not sold yet)"
    return "Open positions (not sold yet)"


# -------------------------------------------------------------------- email
def _td(style: str, inner: str, extra: str = "") -> str:
    return f'<td{extra} style="{style}">{inner}</td>'


def _section(title: str) -> str:
    return (f'<tr><td style="padding:26px 20px 8px 20px;font-family:{FONT};font-size:13px;letter-spacing:1px;'
            f'text-transform:uppercase;font-weight:700;color:#6b7280;border-top:1px solid #e5e7eb;">{esc(title)}</td></tr>')


def _tile(label: str, value: str, color: str, sub: str) -> str:
    return (f'<td width="33%" valign="top" style="padding:0 4px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="background:#f8f7f3;border-radius:10px;"><tr><td style="padding:12px 10px;font-family:{FONT};">'
            f'<div style="font-size:11px;color:#6b7280;line-height:14px;">{label}</div>'
            f'<div style="font-size:18px;font-weight:700;color:{color};padding-top:6px;line-height:22px;">{value}</div>'
            f'<div style="font-size:11px;color:#6b7280;padding-top:3px;line-height:14px;">{sub}</div></td></tr></table></td>')


ACTION_STYLE = {
    "BUY": ("#e6f4ec", "#1a7f4b"), "SELL": ("#fbeae8", "#b4443a"),
    "SKIP": ("#fdf1d6", "#92620f"), "BLOCK": ("#fdf1d6", "#92620f"), "HOLD": ("#eceef1", "#4b5563"),
}


def _decision_row(d: dict) -> str:
    bg, fg = ACTION_STYLE.get(d["action"], ACTION_STYLE["HOLD"])
    price = inr(d["price"], 2)
    if d["action"] in ("SKIP", "BLOCK"):
        what = f"{esc(d['symbol'])} @ {price}"
    else:
        what = f"{d['qty']} {esc(d['symbol'])} @ {price}"
    pl = ""
    if d["action"] == "SELL" and d.get("pnl") is not None:
        pl = f'<span style="color:{_tone_color(d["pnl"])};font-weight:700;"> &nbsp;{sg(d["pnl"])}</span>'
    return (f'<tr><td style="padding:10px 20px;border-bottom:1px solid #f1f1f1;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td width="50" valign="top" style="font-family:{FONT};font-size:13px;color:#6b7280;padding-top:3px;">{esc(d["time"])}</td>'
            f'<td valign="top" style="font-family:{FONT};"><span style="display:inline-block;background:{bg};color:{fg};font-size:12px;font-weight:700;'
            f'border-radius:4px;padding:2px 8px;letter-spacing:.5px;">{d["action"]}</span>'
            f'<span style="font-size:15px;color:#1f2937;font-weight:600;"> &nbsp;{what}</span>{pl}'
            f'<div style="font-size:13px;color:#4b5563;line-height:19px;padding-top:4px;">{esc(d["reason"])}</div></td></tr></table></td></tr>')


def _chip(n: int, label: str) -> str:
    return (f'<span style="display:inline-block;background:#f3f1ea;color:#1f2937;border-radius:14px;padding:5px 11px;margin:0 6px 6px 0;'
            f'font-family:{FONT};font-size:13px;"><b>{n}</b> {label}</span>')


def _market_row(m: dict) -> str:
    up = m["move_pct"] >= 0
    color = GREY if m.get("neutral") else (GREEN if up else RED)
    arrow = "▲" if up else "▼"
    return (f'<tr><td style="padding:9px 20px;font-family:{FONT};border-bottom:1px solid #f1f1f1;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td valign="top" style="font-family:{FONT};font-size:15px;color:#1f2937;"><b>{esc(m["name"])}</b>'
            f'<div style="font-size:13px;color:#6b7280;padding-top:2px;line-height:18px;">{esc(m.get("note", ""))}</div></td>'
            f'<td valign="top" align="right" width="82" style="font-family:{FONT};font-size:15px;font-weight:700;color:{color};white-space:nowrap;">'
            f'{arrow} {pc(m["move_pct"])}</td></tr></table></td></tr>')


def _money_row(label: str, amount: float, border: bool = False, bold: bool = False) -> str:
    b = "border-top:1px solid #e5e7eb;" if border else ""
    w = "font-weight:700;" if bold else ""
    return (f'<tr><td style="padding:7px 0;font-family:{FONT};font-size:15px;color:#1f2937;{b}{w}">{label}</td>'
            f'<td align="right" style="padding:7px 0;font-family:{FONT};font-size:15px;font-weight:700;color:{_tone_color(amount)};{b}">{sg(amount)}</td></tr>')


def _glance_html(day: dict, link: str | None) -> str:
    rows = glance_lines(day)
    if not rows:
        return ""
    h = ""
    for label, text in rows:
        h += (f'<tr><td width="104" valign="top" style="padding:6px 10px 6px 0;font-family:{FONT};font-size:13px;line-height:20px;'
              f'color:#6b7280;font-weight:700;">{esc(label)}</td>'
              f'<td valign="top" style="padding:6px 0;font-family:{FONT};font-size:14px;line-height:20px;color:#1f2937;">{esc(text)}</td></tr>\n')
    if link:
        h += (f'<tr><td colspan="2" style="padding:10px 0 0 0;font-family:{FONT};font-size:14px;line-height:21px;color:#1f2937;">'
              f'<a href="{_html.escape(link, quote=True)}" style="color:#8a6d12;font-weight:700;text-decoration:underline;">'
              f'{esc(GLANCE_LINK_TEXT)} &rarr;</a></td></tr>\n')
    return (_section("Day at a glance") + f'<tr><td style="padding:0 20px;"><table role="presentation" width="100%" '
            f'cellpadding="0" cellspacing="0">\n{h}</table></td></tr>\n')


def render_email(day: dict, report_url: str | None = None, sample: bool = False, inbox_url: str | None = None) -> str:
    day = prepare(day)
    p = day["portfolio"]
    d0 = dt.date.fromisoformat(day["date"])
    date_line = f"{day['weekday']}, {d0.day} {d0.strftime('%B %Y')}"
    badge = (f'<span style="display:inline-block;border:1px solid #c9a227;color:#8a6d12;font-size:11px;font-weight:700;letter-spacing:1.5px;'
             f'border-radius:4px;padding:2px 8px;">SAMPLE</span>') if sample else ""
    head = (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="light"><title>Metals report</title>\n<style>@media only screen and (max-width:480px)'
            f'{{.px{{padding-left:14px!important;padding-right:14px!important}}}}</style></head>\n'
            f'<body style="margin:0;padding:0;background:#f1f0ec;">\n<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f0ec;">'
            f'<tr><td align="center" style="padding:16px 8px;">\n<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="max-width:600px;background:#ffffff;border-radius:12px;border-top:4px solid #c9a227;">\n'
            f'<tr><td style="padding:20px 20px 0 20px;font-family:{FONT};">{badge}<div style="font-size:14px;color:#6b7280;padding-top:{10 if badge else 0}px;">{esc(date_line)}</div></td></tr>\n')
    foot = (f'<tr><td align="center" style="padding:22px 20px 24px 20px;font-family:{FONT};font-size:12px;line-height:18px;color:#6b7280;">'
            f'Paper trading simulation. No real money was used.</td></tr>\n</table></td></tr></table></body></html>\n')

    if day.get("closed"):
        body = (f'<tr><td style="padding:14px 20px 4px 20px;font-family:{FONT};"><div style="font-size:30px;line-height:36px;font-weight:800;color:#1f2937;">Market closed</div>'
                f'<div style="font-size:16px;line-height:24px;color:#1f2937;padding-top:10px;">{esc(day["one_line_summary"])}</div></td></tr>\n'
                f'<tr><td style="padding:20px 16px 0 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                + _tile("Portfolio value", inr(p["close"]), INK, "unchanged")
                + _tile("Month so far", sg(p["month_pnl"]), _tone_color(p["month_pnl"]), "")
                + _tile("Distance to safety floor", inr(max(p["distance_to_floor"], 0)),
                        GREEN if p["distance_to_floor"] > 0 else RED, f"above {inr(p['floor'])}")
                + '</tr></table></td></tr>\n' + _section("Tomorrow")
                + f'<tr><td style="padding:0 20px;font-family:{FONT};font-size:15px;line-height:23px;color:#1f2937;">{esc(day["tomorrow"])}</td></tr>\n')
        return head + body + _ops_html(day) + foot

    pnl = p["day_pnl"]
    r = _round(pnl)
    color = _tone_color(pnl)
    if r > 0:
        headline = f'You\'re in the green: <span style="white-space:nowrap;">{sg(pnl)}</span> today'
    elif r < 0:
        headline = f'You\'re in the red: <span style="white-space:nowrap;">{sg(pnl)}</span> today'
    else:
        headline = 'You\'re flat today: <span style="white-space:nowrap;">₹0</span>'
    extra = ""
    if r < 0:
        if p["distance_to_floor"] > 0:
            note = ("The safety floor is " + inr(p["distance_to_floor"])
                    + " below today's value and the rules are unchanged.")
        else:
            note = "Value is at or below the safety floor, so the engine is protecting cash."
        extra = ('<div style="font-size:14px;line-height:21px;color:#4b5563;padding-top:8px;">'
                 + note + '</div>')
    for w in day["warnings"]:
        extra += f'<div style="font-size:13px;line-height:19px;color:#8a6d12;padding-top:8px;">Note: {esc(w)}</div>'
    hero = (f'<tr><td style="padding:14px 20px 4px 20px;font-family:{FONT};"><div style="font-size:30px;line-height:36px;font-weight:800;color:{color};">{headline}</div>'
            f'<div style="font-size:16px;line-height:24px;color:#1f2937;padding-top:10px;">{esc(day["one_line_summary"])}</div>{extra}</td></tr>\n')

    dist = p["distance_to_floor"]
    tiles = ('<tr><td style="padding:20px 16px 0 16px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>\n'
             + _tile("Portfolio value", inr(p["close"]), INK, f"started at {inr(day['start_capital'])}") + "\n"
             + _tile("Month so far", sg(p["month_pnl"]), _tone_color(p["month_pnl"]), pc(p["month_pnl_pct"], 2)) + "\n"
             + _tile("Distance to safety floor", inr(abs(dist)), GREEN if dist > 0 else RED,
                     f"{'above' if dist >= 0 else 'below'} {inr(p['floor'])}")
             + "\n</tr></table></td></tr>\n")

    span = (p["next_value"] - p["floor"]) or 1
    frac = max(0.0, min(1.0, (p["close"] - p["floor"]) / span))
    gold = round(frac * 100)
    nxt, nf = p["next_value"], p.get("floor_after_next", LOCKED_FLOOR)
    gauge = (f'<tr><td style="padding:18px 20px 0 20px;font-family:{FONT};"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
             f'<td style="font-size:12px;color:#6b7280;">Floor<br><b style="color:#1f2937">{inr(p["floor"])}</b></td>'
             f'<td align="right" style="font-size:12px;color:#6b7280;">Next step<br><b style="color:#1f2937">{inr(nxt)}</b></td></tr></table>\n'
             f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;"><tr>'
             f'<td width="{gold}%" height="12" style="background:#c9a227;border-radius:6px 0 0 6px;font-size:0;line-height:0;">&nbsp;</td>'
             f'<td width="14" height="12" style="background:#1f2937;font-size:0;line-height:0;">&nbsp;</td>'
             f'<td height="12" style="background:#e8e6df;border-radius:0 6px 6px 0;font-size:0;line-height:0;">&nbsp;</td></tr></table>\n'
             f'<div style="font-size:12px;color:#6b7280;padding-top:6px;line-height:17px;">Dark marker = today\'s value ({inr(p["close"])}). '
             f'At {inr(nxt)} the floor moves up to {inr(nf)}.</div></td></tr>\n')

    link = report_url or ("https://claude.ai/artifact/REPORT_LINK" if sample else None)
    glance = _glance_html(day, link)

    market = ""
    if day["market"]:
        market = _section("Market today") + "".join(_market_row(m) for m in day["market"]) + "\n"

    c = day["counts"]
    chips = [_chip(c["buys"], _plural(c["buys"], "buy")), _chip(c["sells"], _plural(c["sells"], "sell"))]
    if c["skips"]:
        chips.append(_chip(c["skips"], "skipped"))
    if c["blocks"]:
        chips.append(_chip(c["blocks"], "blocked"))
    if c["holds"]:
        chips.append(_chip(c["holds"], _plural(c["holds"], "hold")))
    if c["fast_lane_runs"]:
        chips.append(_chip(c["fast_lane_runs"], _plural(c["fast_lane_runs"], "news trigger")))
    did = _section("What I did") + f'<tr><td style="padding:2px 20px 6px 20px;">{"".join(chips)}</td></tr>\n'
    if day.get("no_trade_note"):
        did += (f'<tr><td style="padding:6px 20px 10px 20px;font-family:{FONT};font-size:15px;line-height:23px;color:#1f2937;">{esc(day["no_trade_note"])}</td></tr>\n')
    did += "".join(_decision_row(x) for x in day["decisions"]) + "\n"

    rows = ""
    for h in day["holdings_close"]:
        rows += (f'<tr><td style="padding:8px 0;font-family:{FONT};font-size:15px;color:#1f2937;"><b>{esc(h["symbol"])}</b> &nbsp;{h["qty"]} {_plural(h["qty"], "unit")}'
                 f'<div style="font-size:13px;color:#6b7280;padding-top:2px;">bought at {inr(h["avg"], 2)}, now {inr(h["close"], 2)}'
                 f'{" · safety stop " + inr(h["stop"], 2) if h.get("stop") else ""}</div></td>'
                 f'<td align="right" valign="top" style="padding:8px 0;font-family:{FONT};font-size:15px;color:#1f2937;"><b>{inr(h["value"])}</b>'
                 f'<div style="font-size:13px;color:{_tone_color(h["unrealized"])};font-weight:700;padding-top:2px;">{sg(h["unrealized"])}</div></td></tr>\n')
    if not rows:
        rows = (f'<tr><td colspan="2" style="padding:8px 0;font-family:{FONT};font-size:15px;color:#1f2937;">No positions held overnight.</td></tr>\n')
    rows += (f'<tr><td style="padding:8px 0;border-top:1px solid #f1f1f1;font-family:{FONT};font-size:15px;color:#1f2937;"><b>Cash</b> in LIQUIDBEES'
             f'<div style="font-size:13px;color:#6b7280;padding-top:2px;">parked safely, earns a little</div></td>'
             f'<td align="right" valign="top" style="padding:8px 0;border-top:1px solid #f1f1f1;font-family:{FONT};font-size:15px;color:#1f2937;"><b>{inr(p["cash_in_liquidbees"])}</b></td></tr>\n')
    money = _section("Where the money is now") + f'<tr><td style="padding:0 20px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">\n{rows}</table></td></tr>\n'

    b = day["pnl_breakdown"]
    title = f"Where today's {inr(abs(pnl))}{' loss' if r < 0 else ''} came from" if r else "Where today's ₹0 came from"
    brows = _money_row("Trades closed (realized)", b["realized_trades"])
    if day["holdings_close"] or _round(b["unrealized_open"]) != 0:
        brows += _money_row(_holding_label(day), b["unrealized_open"])
    brows += _money_row("LIQUIDBEES income", b["liquidbees_income"])
    brows += _money_row("Total today", pnl, border=True, bold=True)
    brows += (f'<tr><td colspan="2" style="padding:6px 0 0;font-family:{FONT};font-size:13px;color:#6b7280;">'
              f'Costs (fees and taxes): {inr(p["costs_paid"])} paid today, already included above.</td></tr>')
    pnlsec = _section(title) + f'<tr><td style="padding:0 20px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">\n{brows}</table></td></tr>\n'

    tom = _section("Tomorrow") + f'<tr><td style="padding:0 20px;font-family:{FONT};font-size:15px;line-height:23px;color:#1f2937;">{esc(day["tomorrow"])}</td></tr>\n'

    button = ""
    if link:
        button = (f'<tr><td align="center" style="padding:28px 20px 8px 20px;"><table role="presentation" cellpadding="0" cellspacing="0"><tr>'
                  f'<td align="center" bgcolor="#1f2937" style="border-radius:8px;"><a href="{_html.escape(link, quote=True)}" style="display:inline-block;padding:15px 28px;'
                  f'font-family:{FONT};font-size:16px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:8px;border-bottom:3px solid #c9a227;">'
                  f'Open this day on the dashboard</a></td></tr></table></td></tr>\n')
    ib = inbox_link(inbox_url or (SAMPLE_INBOX_URL if sample else None), daily_item_id(day["date"]))
    inbox = ""
    if ib:
        inbox = (f'<tr><td align="center" style="padding:28px 20px 4px 20px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                 f'<td align="center" bgcolor="#1f2937" style="border-radius:8px;"><a href="{_html.escape(ib, quote=True)}" style="display:block;padding:16px 18px;'
                 f'font-family:{FONT};font-size:17px;font-weight:700;color:#ffffff;text-decoration:none;border-radius:8px;border-bottom:3px solid #c9a227;">'
                 f'{INBOX_LABEL}</a></td></tr></table></td></tr>\n')
        button = button.replace("padding:28px 20px 8px 20px", "padding:14px 20px 8px 20px", 1)
    return head + hero + tiles + gauge + glance + market + did + money + pnlsec + tom + _ops_html(day) + inbox + button + foot


# --------------------------------------------------------------------- text
def _inbox_text(day: dict, inbox_url: str | None, sample: bool) -> list[str]:
    ib = inbox_link(inbox_url or (SAMPLE_INBOX_URL if sample else None), daily_item_id(day["date"]))
    return [f"{INBOX_LABEL}: {ib}", ""] if ib else []


def render_text(day: dict, sample: bool = False, inbox_url: str | None = None, report_url: str | None = None) -> str:
    day = prepare(day)
    p = day["portfolio"]
    d0 = dt.date.fromisoformat(day["date"])
    when = f"{day['weekday']} {d0.day} {d0.strftime('%b %Y')}" + (" (sample data, not a real trading day)" if sample else "")
    L = ["GOLDBEES / SILVERBEES / HINDCOPPER - Daily report", when, ""]
    if day.get("closed"):
        L += ["MARKET CLOSED", day["one_line_summary"], "", f"Portfolio: {inr(p['close'])}   Floor: {inr(p['floor'])}", "",
              "TOMORROW", day["tomorrow"], "", *_ops_text(day), *_inbox_text(day, inbox_url, sample),
              "This is a paper simulation. No real money or orders were placed."]
        return "\n".join(L) + "\n"
    L += ["HEADLINE", f"Day P&L:      {sg(p['day_pnl'])} ({pc(p['day_pnl_pct'], 2)})",
          f"Month P&L:    {sg(p['month_pnl'])} ({pc(p['month_pnl_pct'], 2)})",
          f"Portfolio:    {inr(p['close'])} (opened the day at {inr(p['open'])})", day["one_line_summary"], ""]
    for w in day["warnings"]:
        L.append("Note: " + w)
    if day["warnings"]:
        L.append("")
    L += ["PORTFOLIO AND FLOOR", f"Floor:              {inr(p['floor'])}",
          f"Distance to floor:  {'-' if p['distance_to_floor'] < 0 else ''}{inr(p['distance_to_floor'])}",
          f"Next step:          {p['floor_next_step'][:1].lower() + p['floor_next_step'][1:]}" if p.get("floor_next_step") else "Next step:          n/a",
          f"Cash in LIQUIDBEES: {inr(p['cash_in_liquidbees'])} (LIQUIDBEES income today: {inr(p['liquidbees_income'])})",
          f"Costs paid today:   {inr(p['costs_paid'])}", ""]
    gl = glance_lines(day)
    if gl:
        L.append("DAY AT A GLANCE")
        L += [f"{label}: {text}" for label, text in gl]
        if report_url:
            L.append(f"{GLANCE_LINK_TEXT}: {report_url}")
        L.append("")
    if day["market"]:
        L.append("MARKET TODAY")
        w = max(len(m["name"]) for m in day["market"])
        L += [f"{m['name']:<{w}}  {pc(m['move_pct']):>6}  {m.get('note', '')}" for m in day["market"]] + [""]
    L.append("DECISIONS")
    if day.get("no_trade_note"):
        L.append(day["no_trade_note"])
    for d in day["decisions"]:
        qty = "--" if d["action"] in ("SKIP", "BLOCK") else str(d["qty"])
        pl = f" P&L {sg(d['pnl'], 2)}." if d["action"] == "SELL" and d.get("pnl") is not None else ""
        L.append(f"{d['time']}  {d['action']:<5} {d['symbol']:<10} {qty:>4} @ {inr(d['price'], 2)}  {d['reason']}{pl}")
    L += ["", "HOLDINGS AT CLOSE"]
    if day["holdings_close"]:
        for h in day["holdings_close"]:
            L.append(f"{h['symbol']}: {h['qty']} {_plural(h['qty'], 'unit')}, avg {inr(h['avg'], 2)}, close {inr(h['close'], 2)}, "
                     f"value {inr(h['value'])}, unrealised {sg(h['unrealized'])}")
    else:
        L.append("No positions held overnight.")
    b = day["pnl_breakdown"]
    L += ["", "WHERE TODAY'S P&L CAME FROM", f"Trades closed (realized): {sg(b['realized_trades'])}",
          f"Open positions (unrealised): {sg(b['unrealized_open'])}", f"LIQUIDBEES income: {sg(b['liquidbees_income'])}",
          f"Total: {sg(p['day_pnl'])}", f"(Costs of {inr(p['costs_paid'])} are already included above.)", "", "TOMORROW", day["tomorrow"], "",
          *_ops_text(day), *_inbox_text(day, inbox_url, sample), "This is a paper simulation. No real money or orders were placed."]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------- page
def render_page(day: dict, template_path: Path, sample: bool = False) -> str:
    day = prepare(dict(day))
    if not sample:
        day.pop("_note", None)
    blob = json.dumps(day, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    tpl = Path(template_path).read_text(encoding="utf-8")
    title = f"Metals Daily Report · {day['date']}"
    return tpl.replace("<title>Metals Daily Report</title>", f"<title>{esc(title)}</title>", 1).replace("__DATA__", blob, 1)


# -------------------------------------------------------------------- write
def write_reports(day: dict, out_dir: Path, stem: str, template_path: Path,
                  report_url: str | None = None, sample: bool = False, inbox_url: str | None = None) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    day = prepare(day)
    if sample:
        day["subject"] = subject_line(day)
    paths = {
        "json": out_dir / f"{stem}.json", "email": out_dir / f"{stem}.email.html",
        "txt": out_dir / f"{stem}.txt", "page": out_dir / f"{stem}.page.html",
    }
    clean = {k: v for k, v in day.items() if sample or k != "_note"}
    paths["json"].write_text(json.dumps(clean, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    paths["email"].write_text(render_email(clean, report_url, sample, inbox_url), encoding="utf-8")
    paths["txt"].write_text(render_text(clean, sample, inbox_url, report_url), encoding="utf-8")
    paths["page"].write_text(render_page(clean, template_path, sample), encoding="utf-8")
    return paths
