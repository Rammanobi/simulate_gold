"""One JSON document per trading day for the Daybook Dashboard artifact (no AI, never trades).

Shape (all keys always present): date, weekday, updated, live, open, close, pnl, pnl_pct, floor, cushion,
liquid_income, costs, summary, tomorrow, counts{BUY,SELL,SKIP,BLOCK,HOLD}, alloc{GOLDBEES,SILVERBEES,HINDCOPPER,
LIQUIDBEES}, holdings[] (GOLDBEES, SILVERBEES, HINDCOPPER, LIQUIDBEES rows: units, avg_price, invested, price, value, gain,
gain_pct, share_pct, first_buy; old keys qty/avg/close/unrealized kept), ledger[] (every BUY/SELL up to the day, newest first:
date, time, symbol, side, units, price, amount, costs, pnl, reason; capped at 500 rows) and ledger_total, ticks[[time, value, gold, silver, copper]], decisions[{time, action, symbol, qty, price,
amount, pnl, reason, trigger, stop, risk, value_after}], notes[{time, who, text}], thrift,
checks{syms, texts, rows[[time, value, [text index per symbol]]]} (every minute, also when nothing happened), targets[] (empty on v1 days).
Strategy v2 days only (strategy absent on v1 days): strategy{mode, brake_level 0/1/2, account_stop, ai_stop, invested_pct,
min_invested?, peak?, drawdown_pct?, account_stop_level?} and targets[{symbol, time, trend ON/OFF, state, sigma (the engine's TARGET fields), reason, sigma60, target_pct, actual_pct, stop, exit_level?,
ai_stop?}] adapted from the engine's TARGET records (state, sigma, target_pct, actual_pct, reason) and its
"Drawdown brake" / "Account stop" decisions.

Built from the same inputs as the daily report (simgold.report.build_day). The engine writes today's document to
logs/dashboard-DATE.json at every git sync, so the dashboard page (which reads it from the public repo) shows each
decision within about 5 minutes. Routines copy it into the artifact's database with tools/dashboard.py.
Holdings come from state/portfolio.json, so a document is only exact for the day it was written on.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from simgold import config, holdings as holdings_mod, report
from simgold.market import now_ist

log = logging.getLogger(__name__)

SYMS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")
ACTIONS = ("BUY", "SELL", "SKIP", "BLOCK", "HOLD")
DEC_KEYS = ("time", "action", "symbol", "qty", "price", "amount", "pnl", "reason", "trigger", "stop", "risk",
            "value_after", "exit_level")
MAX_TICKS = 140         # keeps a document far below the 256 KiB database limit
MAX_NOTES = 30
MAX_CHECK_CHARS = 220
NOTE_SOURCES = (("guidance.json", "note"), ("fastlane.json", "note"))


def _f(v):
    try:
        return None if v in (None, "") else round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _ticks(path: Path) -> list[list]:
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("time")]
    except OSError:
        return []
    step = max(1, -(-len(rows) // MAX_TICKS))
    keep = [r for i, r in enumerate(rows) if i % step == 0 or i == len(rows) - 1]
    return [[r["time"][:5], _f(r.get("value")), _f(r.get("GOLDBEES")), _f(r.get("SILVERBEES")),
             _f(r.get("HINDCOPPER"))] for r in keep]


def _raw_decisions(path: Path) -> list[dict]:
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("action") in ACTIONS:
            out.append({k: r.get(k) for k in DEC_KEYS})
    out.sort(key=lambda d: str(d.get("time") or ""))
    return out


V2_TRIGGERS = ("Trend check", "Trend exit", "Rebalance", "Drawdown brake", "Account stop")
_DETAIL = re.compile(r"trend\s+\w+\s+\((.*?)\);\s*sigma60")
_BRAKE_LEVEL = re.compile(r"brake level (\d)")
_BRAKE_MULT = re.compile(r"x ([\d.]+) \(drawdown brake")


def _params(root: Path) -> dict:
    try:
        import yaml
        with (root / "config" / "params.yaml").open(encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("strategy") or {}
    except Exception:  # noqa: BLE001 - config is optional for the dashboard
        return {}


def _num(v):
    return _f(v) if isinstance(v, (int, float, str)) else None


def _v2(path: Path, root: Path, alloc: dict, close) -> tuple[dict | None, list[dict] | None]:
    """Adapter for strategy v2: (strategy block, per-symbol targets), or (None, None) on v1 days."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    recs = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            recs.append(r)
    if not any(r.get("action") == "TARGET" or r.get("trigger") in V2_TRIGGERS for r in recs):
        return None, None
    par = _params(root)
    level, mult_level, acct, ai_all = 0, None, False, False
    last: dict[str, dict] = {}
    stops: dict[str, dict] = {}
    for r in recs:
        trig, reason, sym = r.get("trigger"), str(r.get("reason") or ""), r.get("symbol")
        if trig == "Drawdown brake":
            m = _BRAKE_LEVEL.search(reason)
            level = int(m.group(1)) if m else 0
        elif trig == "Account stop" and r.get("action") == "BLOCK":
            acct = True
        elif trig == "Opus guidance" and r.get("action") == "BLOCK" and sym == "ALL":
            ai_all = True
        if sym in SYMS:
            if r.get("action") == "TARGET":
                last[sym] = r
                m = _BRAKE_MULT.search(reason)
                if m:
                    mult_level = 2 if float(m.group(1)) == 0 else 1
            st = stops.setdefault(sym, {})
            if _num(r.get("exit_level")):
                st["exit_level"] = _num(r.get("exit_level"))
            if _num(r.get("stop")):
                st["ai_stop"] = _num(r.get("stop"))
    if level == 0 and mult_level:                    # brake set on an earlier day, still cutting today's targets
        level = mult_level
    stop_cfg = _num(par.get("account_stop"))
    if not acct and stop_cfg and close is not None and close <= stop_cfg:
        acct = True
    metals = sum(alloc.get(s, 0.0) for s in SYMS)
    total = metals + alloc.get("LIQUIDBEES", 0.0)
    strategy = {"mode": str(par.get("mode") or "v2_trend"), "brake_level": level, "account_stop": acct,
                "ai_stop": ai_all, "invested_pct": round(100.0 * metals / total, 1) if total else 0.0}
    floor = _num(par.get("min_invested", par.get("gold_floor")))
    if floor is not None:
        strategy["min_invested"] = floor if floor > 1 else round(floor * 100, 1)
    targets = []
    for s in SYMS:
        r = last.get(s)
        if not r:
            continue
        reason = str(r.get("reason") or "")
        m = _DETAIL.search(reason)
        stp = stops.get(s, {})
        t = {"symbol": s, "time": str(r.get("time") or "")[:5], "trend": str(r.get("state") or "").upper() or None,
             "state": r.get("state"), "sigma": r.get("sigma"),      # the engine's own field names, kept for readers of the simple schema
             "reason": (m.group(1) if m else reason)[:300],
             "sigma60": round(float(r["sigma"]), 4) if isinstance(r.get("sigma"), (int, float)) else None,
             "target_pct": _num(r.get("target_pct")), "actual_pct": _num(r.get("actual_pct"))}
        real = [v for v in (stp.get("exit_level"), stp.get("ai_stop")) if v]
        if real:
            t["stop"] = max(real)
        t.update({k: v for k, v in stp.items() if v})
        targets.append(t)
    return strategy, targets


def _checks(path: Path) -> dict:
    """Every minute's check, compact: repeated sentences are stored once ({"texts": [...], "rows": [[time, value,
    [text index per symbol in SYMS order]], ...]})."""
    texts: list[str] = []
    index: dict[str, int] = {}
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"syms": list(SYMS), "texts": texts, "rows": rows}
    seen = set()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or not isinstance(r.get("checks"), dict) or r.get("time") in seen:
            continue
        seen.add(r.get("time"))
        ids = []
        for s in SYMS:
            txt = str(r["checks"].get(s) or "")[:MAX_CHECK_CHARS]
            if txt not in index:
                index[txt] = len(texts)
                texts.append(txt)
            ids.append(index[txt])
        rows.append([str(r.get("time"))[:5], _f(r.get("value")), ids])
    rows.sort(key=lambda x: x[0])
    return {"syms": list(SYMS), "texts": texts, "rows": rows}


def _who(ts: str) -> str:
    hhmm = ts[11:16] if len(ts) >= 16 else ""
    if not hhmm:
        return "routine"
    if hhmm < "09:15":
        return "premarket"
    if hhmm >= "15:35":
        return "daily_report"
    return "hourly"


def _notes(root: Path, date: str, previous: list) -> list[dict]:
    """Notes seen so far today: kept from the previous document, plus the current routine notes if new."""
    notes = [n for n in previous if isinstance(n, dict) and n.get("text")]
    seen = {(n.get("time"), n.get("text")) for n in notes}
    for name, key in NOTE_SOURCES:
        d = report._read_json(root / "state" / name, None)
        if not isinstance(d, dict):
            continue
        ts = str(d.get("updated") or d.get("ts") or "")
        text = str(d.get(key) or "").strip()
        if not ts.startswith(date) or not text:
            continue
        who = "fastlane" if name == "fastlane.json" else _who(ts)
        item = {"time": ts[11:16], "who": who, "text": text[:400]}
        if (item["time"], item["text"]) not in seen:
            notes.append(item)
            seen.add((item["time"], item["text"]))
    notes.sort(key=lambda n: str(n.get("time")))
    return notes[-MAX_NOTES:]


def _with_stops(rows: list[dict], closes: list[dict]) -> list[dict]:
    """Keep the old per-holding stop fields next to the new holdings columns."""
    by = {h["symbol"]: h for h in closes}
    for r in rows:
        h = by.get(r["symbol"])
        if h:
            for k in ("stop", "exit_level"):
                if k in h:
                    r[k] = h[k]
    return rows


def build(date: str, root: Path, now: datetime | None = None) -> dict:
    root = Path(root)
    day = report.build_day(date, root)
    p = day["portfolio"]
    path = root / "logs" / f"dashboard-{date}.json"
    prev = report._read_json(path, {}) or {}
    closes = [h for h in day.get("holdings_close", []) if h.get("symbol") in SYMS]
    alloc = {s: 0.0 for s in SYMS}
    for h in closes:
        alloc[h["symbol"]] = round(float(h.get("value") or 0), 2)
    alloc["LIQUIDBEES"] = round(float(p.get("cash_in_liquidbees") or 0), 2)
    try:        # holdings (4 rows, as of the day) and the full trade ledger up to and including the day
        hv = holdings_mod.view(date, root, {h["symbol"]: h["close"] for h in closes if h.get("close")}, alloc["LIQUIDBEES"])
        holdings = _with_stops(hv["rows"], closes)
        ledger, ledger_total = hv["ledger"], hv["ledger_total"]
    except Exception:  # noqa: BLE001 - never lose the day document over the extra view
        log.warning("dashboard: holdings view failed for %s", date, exc_info=True)
        holdings, ledger, ledger_total = closes, [], 0
    decisions = _raw_decisions(root / "logs" / f"decisions-{date}.jsonl")
    counts = {a: sum(1 for d in decisions if d["action"] == a) for a in ACTIONS}
    strategy, targets = _v2(root / "logs" / f"decisions-{date}.jsonl", root, alloc, p.get("close"))
    stamp = (now or now_ist()).isoformat(timespec="seconds")
    doc = {
        "date": date, "weekday": day.get("weekday", ""), "updated": stamp, "live": not day.get("closed", False),
        "idle": "cash" if config.idle_is_cash() else "liquidbees",
        "open": p["open"], "prev_close_date": p.get("prev_close_date"), "close": p["close"], "pnl": p["day_pnl"], "pnl_pct": p["day_pnl_pct"],
        "floor": p["floor"], "cushion": p["distance_to_floor"], "liquid_income": p["liquidbees_income"],
        "costs": p["costs_paid"], "summary": day.get("one_line_summary", ""), "tomorrow": day.get("tomorrow", ""),
        "counts": counts, "alloc": alloc, "holdings": holdings, "ledger": ledger, "ledger_total": ledger_total,
        "ticks": _ticks(root / "logs" / f"ticks-{date}.csv"), "decisions": decisions,
        "notes": _notes(root, date, prev.get("notes") or []), "thrift": bool(day.get("thrift")),
        "checks": _checks(root / "logs" / f"checks-{date}.jsonl"),
        "targets": [],
        "money": day.get("money_summary"),           # "Your money at a glance" card (`summary` is the one-line banner text)
    }
    if strategy is not None:                         # v2 only; v1 documents keep their old shape
        doc["strategy"], doc["targets"] = strategy, targets
        if p.get("v2"):                              # risk-brake facts for the KPI tile (same numbers as the report)
            strategy["peak"] = p.get("peak")
            strategy["drawdown_pct"] = round(100.0 * float(p.get("drawdown_from_peak") or 0), 2)
            strategy["account_stop_level"] = p.get("account_stop")
            if p.get("brake_level") is not None and not strategy.get("brake_level"):
                strategy["brake_level"] = int(p["brake_level"])
    return doc


def write(date: str, root: Path, now: datetime | None = None) -> Path:
    """Write logs/dashboard-DATE.json atomically and return its path."""
    doc = build(date, root, now)
    path = Path(root) / "logs" / f"dashboard-{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def safe_write(date: str, root: Path) -> bool:
    """For the engine: never raises."""
    try:
        write(date, root)
        return True
    except Exception:  # noqa: BLE001 - the dashboard must never stop trading
        log.warning("dashboard: could not write the day document for %s", date, exc_info=True)
        return False
