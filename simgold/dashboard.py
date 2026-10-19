"""One JSON document per trading day for the Daybook Dashboard artifact (no AI, never trades).

Shape (all keys always present): date, weekday, updated, live, open, close, pnl, pnl_pct, floor, cushion,
liquid_income, costs, summary, tomorrow, counts{BUY,SELL,SKIP,BLOCK,HOLD}, alloc{GOLDBEES,SILVERBEES,HINDCOPPER,
LIQUIDBEES}, holdings[], ticks[[time, value, gold, silver, copper]], decisions[{time, action, symbol, qty, price,
amount, pnl, reason, trigger, stop, risk, value_after}], notes[{time, who, text}], thrift,
checks{syms, texts, rows[[time, value, [text index per symbol]]]} (every minute, also when nothing happened),
targets[{symbol, time, state, sigma, target_pct, actual_pct, reason}] (strategy v2: the day's last TARGET record per symbol;
empty under the v1 score rules).

Built from the same inputs as the daily report (simgold.report.build_day). The engine writes today's document to
logs/dashboard-DATE.json at every git sync, so the dashboard page (which reads it from the public repo) shows each
decision within about 5 minutes. Routines copy it into the artifact's database with tools/dashboard.py.
Holdings come from state/portfolio.json, so a document is only exact for the day it was written on.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from simgold import report
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


def _targets(path: Path) -> list[dict]:
    """Strategy v2: the last TARGET record of the day per symbol (trend state, sigma60, target % and actual %)."""
    last: dict[str, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("action") == "TARGET" and r.get("symbol") in SYMS:
            last[r["symbol"]] = {"symbol": r["symbol"], "time": str(r.get("time") or "")[:5], "state": r.get("state"),
                                 "sigma": round(float(r["sigma"]), 4) if isinstance(r.get("sigma"), (int, float)) else None, "target_pct": _f(r.get("target_pct")),
                                 "actual_pct": _f(r.get("actual_pct")), "reason": str(r.get("reason") or "")[:400]}
    return [last[s] for s in SYMS if s in last]


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


def build(date: str, root: Path, now: datetime | None = None) -> dict:
    root = Path(root)
    day = report.build_day(date, root)
    p = day["portfolio"]
    path = root / "logs" / f"dashboard-{date}.json"
    prev = report._read_json(path, {}) or {}
    holdings = [h for h in day.get("holdings_close", []) if h.get("symbol") in SYMS]
    alloc = {s: 0.0 for s in SYMS}
    for h in holdings:
        alloc[h["symbol"]] = round(float(h.get("value") or 0), 2)
    alloc["LIQUIDBEES"] = round(float(p.get("cash_in_liquidbees") or 0), 2)
    decisions = _raw_decisions(root / "logs" / f"decisions-{date}.jsonl")
    counts = {a: sum(1 for d in decisions if d["action"] == a) for a in ACTIONS}
    stamp = (now or now_ist()).isoformat(timespec="seconds")
    return {
        "date": date, "weekday": day.get("weekday", ""), "updated": stamp, "live": not day.get("closed", False),
        "open": p["open"], "close": p["close"], "pnl": p["day_pnl"], "pnl_pct": p["day_pnl_pct"],
        "floor": p["floor"], "cushion": p["distance_to_floor"], "liquid_income": p["liquidbees_income"],
        "costs": p["costs_paid"], "summary": day.get("one_line_summary", ""), "tomorrow": day.get("tomorrow", ""),
        "counts": counts, "alloc": alloc, "holdings": holdings,
        "ticks": _ticks(root / "logs" / f"ticks-{date}.csv"), "decisions": decisions,
        "notes": _notes(root, date, prev.get("notes") or []), "thrift": bool(day.get("thrift")),
        "checks": _checks(root / "logs" / f"checks-{date}.jsonl"),
        "targets": _targets(root / "logs" / f"decisions-{date}.jsonl"),
    }


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
