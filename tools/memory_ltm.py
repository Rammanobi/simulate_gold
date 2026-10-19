#!/usr/bin/env python3
"""Long-term memory writer (paper trading). The ONLY way to change memory/ltm/*.json and the lesson ledger.

  python tools/memory_ltm.py regime [--write]                       # trend label per metal from daily closes (code)
  python tools/memory_ltm.py calendar add --date D --type T [--time HH:MM] [--metals GOLD,SILVER] [--impact 1-3]
                                          [--expect "fact"] [--source domain] [--confirmed] [--by weekly]
  python tools/memory_ltm.py calendar list | calendar remove --date D --type T | calendar fill
  python tools/memory_ltm.py thesis set --metal GOLD --stance constructive|neutral|cautious
                                        --invalidate close_below:GOLDBEES:112 [--review-by D] [--evidence "fact (src, 17 Oct)"]...
                                        [--risk "fact"]...
  python tools/memory_ltm.py thesis show
  python tools/memory_ltm.py check                                  # flips `invalidated` when a thesis condition is hit
  python tools/memory_ltm.py lesson candidate --text "..." --evidence "2026-10-15: fact" [--source daily|incident|owner]
  python tools/memory_ltm.py lesson contradict ID --evidence "D: fact" | lesson promote ID | lesson retire ID --reason | lesson drop ID --reason "..." | lesson list
  python tools/memory_ltm.py lint                                   # memory/lessons.md rules
  python tools/memory_ltm.py perf [--write]                         # monthly scorecard rows (code)

Exit 0 = ok, exit 2 = refused (nothing written). Text is facts + sources only (simgold/memsafe.py); an LLM-written
field can only restrict (a `cautious` thesis lowers the bias ceiling; nothing here can raise exposure).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import brief, config, evaluate, market, memsafe, memstore, regime
from simgold.memsafe import Refused

ENGINE_TYPES = ("CPI", "FOMC", "JOBS", "RBI", "HINDCOPPER_RESULTS", "BUDGET")
INFO_TYPES = ("NSE_HOLIDAY", "MONTH_END", "INDIA_DUTY", "CHINA_DATA", "OTHER")
CAL_TYPES = ENGINE_TYPES + INFO_TYPES
STANCES = ("constructive", "neutral", "cautious")
INVALID_KINDS = ("close_below", "close_above", "regime_is", "driver_below", "driver_above", "date")
LESSON_MIN_DAYS = 28
LESSON_MIN_SUPPORT = 3
LESSON_STALE_WEEKS = 8
CANDIDATE_DROP_WEEKS = 6
MAX_CANDIDATES = 60
MAX_LESSONS = 40
CAL_PAST_DAYS = 7
CAL_AHEAD_DAYS = 56
CAL_MAX_EVENTS = 40
CAL_MAX_BYTES = 5000
REGIME_MAX_BYTES = 2500
THESES_MD_MAX = 3500
THESIS_MAX_LINES = 12
SYNC_DAYS = 21


class Fail(Refused):
    pass


def _d(s: str, what: str = "date") -> date:
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        raise Refused(f"{what} must look like 2026-10-28, got {s!r}")


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


# ================================================================== regime
def fetch_history(tickers: list[str]) -> dict:
    """Network: daily closes (2 y) for the tickers. Replaced in tests."""
    from simgold import data
    return data.daily_history(tickers, period="2y")


def build_regime(root: Path, now: datetime, hist: dict | None = None) -> dict:
    """regime.json content from daily history (completed bars only). Pure when `hist` is given."""
    drivers = sorted({t for a in config.ASSETS.values() for t in a["drivers"]})
    tickers = sorted({a["ticker"] for a in config.ASSETS.values()} | set(drivers))
    if hist is None:
        hist = fetch_history(tickers)
    close, high, low = hist["close"], hist.get("high"), hist.get("low")
    # only completed bars: drop today's bar while the session is still running
    if len(close) and close.index[-1].date() >= now.date() and (not market.is_trading_day(now.date()) or now.time() < market.CLOSE):
        keep = close.index.map(lambda x: x.date() < now.date())
        close = close[keep]
        high = high[keep] if high is not None else None
        low = low[keep] if low is not None else None
    prev = memstore.regime_data(root).get("metals") or {}
    out: dict = {"updated": now.isoformat(timespec="seconds"), "metals": {}}
    asofs = []
    for m, a in config.ASSETS.items():
        t = a["ticker"]
        if t not in close:
            out["metals"][m] = {"label": "FLAT", "stale": True, "reason": "no price history", "asof": None}
            continue
        r = regime.ltm_regime(close[t], high[t] if high is not None and t in high else None,
                              low[t] if low is not None and t in low else None, prev.get(m))
        r.update(regime.driver_scores(close, a["drivers"]))
        s = close[t].dropna()
        r["recent_closes"] = [round(float(x), 2) for x in s.iloc[-memstore.RECENT_CLOSES:]]
        out["metals"][m] = r
        if r.get("asof"):
            asofs.append(r["asof"])
    out["asof"] = min(asofs) if asofs else None
    ref = next((t for t in (config.ASSETS["GOLD"]["ticker"],) if t in close), None)
    if ref is not None:
        idx = close[ref].dropna().index[-memstore.RECENT_CLOSES:]
        out["recent_dates"] = [x.strftime("%m-%d") for x in idx]      # shared by the metals; the year comes from asof
    def too_big() -> bool:
        return len(_compact(out).encode()) > REGIME_MAX_BYTES
    if too_big():                                   # shrink in stages: contributions, then drivers, then old closes
        for r in out["metals"].values():
            for d in (r.get("drivers") or {}).values():
                d.pop("contrib", None)
    if too_big():
        for r in out["metals"].values():
            r.pop("drivers", None)
    if too_big():
        keep = 15
        out["recent_dates"] = (out.get("recent_dates") or [])[-keep:]
        for r in out["metals"].values():
            r["recent_closes"] = (r.get("recent_closes") or [])[-keep:]
    return out


def cmd_regime(a, root: Path, now: datetime) -> str:
    reg = build_regime(root, now)
    text = _compact(reg)
    if len(text.encode()) > REGIME_MAX_BYTES:
        raise Refused(f"regime.json would be {len(text.encode())} bytes (cap {REGIME_MAX_BYTES})")
    if a.write:
        memstore.write_atomic(memstore.ltm_path(root, "regime.json"), text)
    lines = []
    for m, r in reg["metals"].items():
        lines.append(f"{m}: {r.get('label')} (raw {r.get('raw_label')}, votes {r.get('votes')}/{r.get('tests')}, "
                     f"since {r.get('since')}, vol {r.get('vol_regime')}, asof {r.get('asof')})"
                     + (" STALE" if r.get("stale") else ""))
    return ("regime.json written\n" if a.write else "regime (not written, use --write)\n") + "\n".join(lines)


# ================================================================== calendar
def _holiday_and_month_end(today: date) -> list[dict]:
    out = []
    end = today + timedelta(days=CAL_AHEAD_DAYS)
    d = today
    while d <= end:
        if d.weekday() < 5 and d in market.NSE_HOLIDAYS:
            out.append({"date": d.isoformat(), "type": "NSE_HOLIDAY", "metals": list(memstore.METALS), "impact": 1,
                        "expect": "NSE closed", "source": "nse", "confirmed": True, "by": "code"})
        d += timedelta(days=1)
    for y, mo in {(today.year, today.month), (end.year, end.month), ((today + timedelta(days=28)).year, (today + timedelta(days=28)).month)}:
        days = market.trading_days_in_month(y, mo)
        if days and today <= days[-1] <= end:
            out.append({"date": days[-1].isoformat(), "type": "MONTH_END", "metals": list(memstore.METALS), "impact": 2,
                        "expect": "last trading day of the month; green month-end matters", "source": "nse",
                        "confirmed": True, "by": "code"})
    return out


def _tidy_calendar(events: list[dict], today: date) -> list[dict]:
    keep = []
    seen = set()
    for e in events:
        try:
            d = date.fromisoformat(str(e["date"])[:10])
        except (KeyError, ValueError):
            continue
        if d < today - timedelta(days=CAL_PAST_DAYS) or d > today + timedelta(days=CAL_AHEAD_DAYS):
            continue
        key = (d.isoformat(), e.get("type"))
        if key in seen:
            continue
        seen.add(key)
        keep.append(e)
    keep.sort(key=lambda e: (e["date"], str(e.get("time") or ""), e["type"]))
    return keep[:CAL_MAX_EVENTS]


def write_calendar(root: Path, events: list[dict], now: datetime) -> None:
    today = now.date()
    codes = {(e["date"], e["type"]) for e in events}
    for e in _holiday_and_month_end(today):
        if (e["date"], e["type"]) not in codes:
            e["added"] = today.isoformat()
            events.append(e)
    events = _tidy_calendar(events, today)
    obj = {"updated": now.isoformat(timespec="seconds"), "events": events}
    text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    while len(text.encode()) > CAL_MAX_BYTES and events:
        events = events[:-1]
        obj["events"] = events
        text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    memstore.write_atomic(memstore.ltm_path(root, "calendar.json"), text)


def _sync_engine_event(root: Path, now: datetime, ev: dict) -> str:
    """confirmed engine-type event inside 21 days -> state/events.json via guidance.py (idempotent)."""
    import guidance
    d = date.fromisoformat(ev["date"])
    if ev["type"] not in ENGINE_TYPES or not ev.get("confirmed") or not (now.date() <= d <= now.date() + timedelta(days=SYNC_DAYS)):
        return ""
    ns = argparse.Namespace(action="add", date=ev["date"], type=ev["type"], time=ev.get("time"))
    return guidance.cmd_events(ns, root, now, config.load_params())


def cmd_calendar(a, root: Path, now: datetime) -> str:
    cal = memstore.calendar_events(root)
    if a.action == "list":
        rows = _tidy_calendar(cal, now.date())
        return "\n".join(f"{e['date']} {e['type']}" + (f" {e['time']}" if e.get("time") else "")
                         + f" impact {e.get('impact', 1)} [{','.join(e.get('metals') or [])}]"
                         + (f" - {e['expect']}" if e.get("expect") else "") + ("" if e.get("confirmed") else " (unconfirmed)")
                         for e in rows) or "no events"
    if a.action == "fill":
        write_calendar(root, cal, now)
        return "calendar.json: holidays and month-end filled by code"
    if not a.date or not a.type:
        raise Refused("calendar add/remove need --date and --type")
    d, typ = _d(a.date), a.type.upper()
    if typ not in CAL_TYPES:
        raise Refused(f"--type must be one of {'/'.join(CAL_TYPES)}")
    if a.action == "remove":
        hit = [e for e in cal if e["date"][:10] == d.isoformat() and e["type"] == typ]
        if any(e.get("confirmed") and e["type"] in ENGINE_TYPES for e in hit):
            raise Refused("a confirmed engine event cannot be removed here; flag it for the owner")
        write_calendar(root, [e for e in cal if e not in hit], now)
        return f"removed {typ} {d}"
    if d < now.date():
        raise Refused(f"{d} is in the past")
    if d > now.date() + timedelta(days=CAL_AHEAD_DAYS):
        raise Refused(f"{d} is more than {CAL_AHEAD_DAYS} days ahead")
    if a.time:
        try:
            datetime.strptime(a.time, "%H:%M")
        except ValueError:
            raise Refused("--time must look like 18:00 (IST)")
    metals = [x.strip().upper() for x in (a.metals or ",".join(memstore.METALS)).split(",") if x.strip()]
    if not metals or any(x not in memstore.METALS for x in metals):
        raise Refused("--metals must be a comma list of GOLD, SILVER, COPPER")
    if not 1 <= a.impact <= 3:
        raise Refused("--impact must be 1, 2 or 3")
    row = {"date": d.isoformat(), "type": typ, "metals": metals, "impact": a.impact, "confirmed": bool(a.confirmed),
           "added": now.date().isoformat(), "by": memsafe.check_text(a.by or "weekly", "source")}
    if a.time:
        row["time"] = a.time
    if a.expect:
        row["expect"] = memsafe.check_text(a.expect, "expect")
    if a.source:
        row["source"] = memsafe.check_text(a.source, "source")
    others = [e for e in cal if not (e["date"][:10] == d.isoformat() and e["type"] == typ)]
    write_calendar(root, others + [row], now)
    msg = f"calendar.json: {typ} {d}" + (" (confirmed)" if a.confirmed else "")
    sync = _sync_engine_event(root, now, row)
    return msg + (f"; {sync}" if sync else "")


# ================================================================== theses
def _theses_md_path(root: Path) -> Path:
    return memstore.ltm_path(root, "theses.md")


def parse_invalidation(spec: str) -> dict:
    parts = [p.strip() for p in str(spec).split(":")]
    kind = parts[0] if parts else ""
    if kind not in INVALID_KINDS:
        raise Refused(f"--invalidate kind must be one of {', '.join(INVALID_KINDS)} (form kind:SYMBOL:value)")
    if kind == "date":
        if len(parts) != 2:
            raise Refused("date invalidation looks like date:2026-11-14")
        return {"kind": "date", "value": _d(parts[1]).isoformat()}
    if len(parts) != 3 or not parts[1]:
        raise Refused(f"{kind} needs kind:SYMBOL:value, for example close_below:GOLDBEES:112")
    if kind == "regime_is":
        if parts[1].upper() not in memstore.METALS or parts[2].upper() not in regime.LTM_LABELS:
            raise Refused("regime_is looks like regime_is:GOLD:DOWN")
        return {"kind": kind, "symbol": parts[1].upper(), "value": parts[2].upper()}
    try:
        v = float(parts[2])
    except ValueError:
        raise Refused(f"{kind} value must be a number")
    if kind.startswith("close_"):
        sym = config.symbol_of(parts[1])
        if sym not in ("GOLDBEES", "SILVERBEES", "HINDCOPPER"):
            raise Refused("close_below/close_above symbol must be GOLDBEES, SILVERBEES or HINDCOPPER")
        return {"kind": kind, "symbol": sym, "value": v}
    return {"kind": kind, "symbol": parts[1], "value": v}


def _render_invalidation(inv: dict) -> str:
    k = inv["kind"]
    if k == "date":
        return f"on {inv['value']}"
    if k == "regime_is":
        return f"{inv['symbol']} regime turns {inv['value']}"
    if k.startswith("close_"):
        return f"{inv['symbol']} closes {k.split('_')[1]} {inv['value']:g}"
    return f"driver {inv['symbol']} 63d change {k.split('_')[1]} {inv['value']:g}%"


def _split_md(text: str) -> dict[str, str]:
    blocks, cur, name = {}, [], None
    for ln in text.splitlines():
        m = re.match(r"^## (GOLD|SILVER|COPPER)\b", ln)
        if m:
            if name:
                blocks[name] = "\n".join(cur).rstrip()
            name, cur = m.group(1), [ln]
        elif name:
            cur.append(ln)
    if name:
        blocks[name] = "\n".join(cur).rstrip()
    return blocks


def cmd_thesis(a, root: Path, now: datetime) -> str:
    th = memstore.theses(root)
    if a.action == "show":
        p = _theses_md_path(root)
        return p.read_text(encoding="utf-8") if p.exists() else "(no theses yet)"
    metal = (a.metal or "").upper()
    if metal not in memstore.METALS:
        raise Refused("--metal must be GOLD, SILVER or COPPER")
    if a.stance not in STANCES:
        raise Refused(f"--stance must be one of {'/'.join(STANCES)}")
    if not a.invalidate:
        raise Refused("a thesis needs a machine-checkable --invalidate (e.g. close_below:GOLDBEES:112)")
    inv = parse_invalidation(a.invalidate)
    review = _d(a.review_by) if a.review_by else now.date() + timedelta(days=14)
    if review < now.date() or review > now.date() + timedelta(days=14):
        raise Refused("--review-by must be within the next 14 days")
    ev = [memsafe.check_text(x, "evidence") for x in (a.evidence or [])]
    risk = [memsafe.check_text(x, "thesis_line") for x in (a.risk or [])]
    if len(ev) + len(risk) > THESIS_MAX_LINES - 3:
        raise Refused(f"too many lines; at most {THESIS_MAX_LINES - 3} evidence+risk lines per metal")
    reg = (memstore.regime_data(root).get("metals") or {}).get(metal) or {}
    head = f"## {metal} - {a.stance} (written {now.date().isoformat()}, review by {review.isoformat()})"
    state = (f"Regime {reg.get('label', 'UNKNOWN')} since {reg.get('since')} (votes {reg.get('votes')}/{reg.get('tests')}, "
             f"r63 {_pct(reg.get('r63'))}). Vol {reg.get('vol_regime', 'n/a')}.")
    lines = [head, state]
    if ev:
        lines.append("Evidence: " + "; ".join(ev))
    if risk:
        lines.append("Risk: " + "; ".join(risk))
    lines.append("Invalidated if: " + _render_invalidation(inv) + ".")
    th[metal] = {"stance": a.stance, "written": now.date().isoformat(), "review_by": review.isoformat(),
                 "invalidation": inv, "invalidated": False, "invalidated_on": None}
    p = _theses_md_path(root)
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    blocks = _split_md(old)
    blocks[metal] = "\n".join(lines)
    md = "# Theses (tool-written; facts + sources only)\n\n" + "\n\n".join(blocks[m] for m in memstore.METALS if m in blocks) + "\n"
    if len(md.encode()) > THESES_MD_MAX:
        raise Refused(f"theses.md would be {len(md.encode())} bytes (cap {THESES_MD_MAX}); shorten the evidence")
    if old.strip() and blocks.get(metal) and old != md:
        arch = Path(root) / "archive" / "memory" / f"theses-{now.year}.md"
        arch.parent.mkdir(parents=True, exist_ok=True)
        with arch.open("a", encoding="utf-8") as fh:
            fh.write(f"\n<!-- replaced {now.isoformat(timespec='seconds')} -->\n{old.strip()}\n")
    memstore.write_json(memstore.ltm_path(root, "theses.json"), th)
    memstore.write_atomic(p, md)
    return f"thesis {metal} {a.stance} written; review by {review}; invalidated if {_render_invalidation(inv)}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _latest_price(root: Path, sym: str):
    lp = brief.load_json(Path(root) / "state" / "last_prices.json", {})
    if isinstance(lp, dict) and isinstance(lp.get(sym), dict) and lp[sym].get("price"):
        return float(lp[sym]["price"])
    reg = (memstore.regime_data(root).get("metals") or {})
    for m in memstore.METALS:
        if memstore.etf(m) == sym:
            rc = (reg.get(m) or {}).get("recent_closes")
            if rc:
                return float(rc[-1])
    return None


def invalidation_hit(root: Path, inv: dict, today: date) -> str:
    """'' or a reason string, decided by code from prices/regime only."""
    k = inv.get("kind")
    if k == "date":
        return f"date {inv['value']} reached" if today >= date.fromisoformat(inv["value"]) else ""
    if k == "regime_is":
        cur = ((memstore.regime_data(root).get("metals") or {}).get(inv["symbol"]) or {}).get("label")
        return f"{inv['symbol']} regime is {cur}" if cur == inv["value"] else ""
    if k in ("close_below", "close_above"):
        px = _latest_price(root, inv["symbol"])
        if px is None:
            return ""
        if k == "close_below" and px < inv["value"]:
            return f"{inv['symbol']} {px:g} below {inv['value']:g}"
        if k == "close_above" and px > inv["value"]:
            return f"{inv['symbol']} {px:g} above {inv['value']:g}"
        return ""
    if k in ("driver_below", "driver_above"):
        for r in (memstore.regime_data(root).get("metals") or {}).values():
            dv = (r.get("drivers") or {}).get(inv["symbol"])
            if isinstance(dv, dict) and dv.get("chg_pct") is not None:
                if k == "driver_below" and dv["chg_pct"] < inv["value"]:
                    return f"driver {inv['symbol']} {dv['chg_pct']:g}% below {inv['value']:g}%"
                if k == "driver_above" and dv["chg_pct"] > inv["value"]:
                    return f"driver {inv['symbol']} {dv['chg_pct']:g}% above {inv['value']:g}%"
    return ""


def cmd_check(a, root: Path, now: datetime) -> str:
    th = memstore.theses(root)
    msgs, changed = [], False
    for m, t in th.items():
        if not isinstance(t, dict):
            continue
        if not t.get("invalidated"):
            why = invalidation_hit(root, t.get("invalidation") or {}, now.date())
            if why:
                t["invalidated"], t["invalidated_on"] = True, now.date().isoformat()
                changed = True
                msgs.append(f"{m}: INVALIDATED ({why}); treated as cautious until the weekly review rewrites it")
        try:
            if date.fromisoformat(t.get("review_by", "9999-12-31")) < now.date():
                msgs.append(f"{m}: past review_by {t['review_by']}; rewrite at the next weekly review")
        except ValueError:
            pass
    if changed:
        memstore.write_json(memstore.ltm_path(root, "theses.json"), th)
    return "\n".join(msgs) or "all theses valid"


# ================================================================== lessons
LEDGER = "lesson_ledger.json"


def _ledger(root: Path) -> dict:
    d = brief.load_json(memstore.ltm_path(root, LEDGER), {})
    return d if isinstance(d, dict) and isinstance(d.get("items"), list) else {"items": []}


def _save_ledger(root: Path, led: dict) -> None:
    text = json.dumps(led, ensure_ascii=False, indent=1) + "\n"
    while len(text.encode()) > 8192 and led["items"]:
        # drop the weakest open candidate (fewest supports, oldest) to stay under the cap
        open_ = [x for x in led["items"] if x.get("status") == "candidate"]
        if not open_:
            break
        w = min(open_, key=lambda x: (len(x.get("support", [])), x.get("first_seen", "")))
        led["items"].remove(w)
        text = json.dumps(led, ensure_ascii=False, indent=1) + "\n"
    memstore.write_atomic(memstore.ltm_path(root, LEDGER), text)


def _lid(text: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", text.lower()).strip().encode()).hexdigest()[:8]


def parse_evidence(s: str, kind: str = "candidate") -> dict:
    """'2026-10-15: fact text' -> {date, text}; the text is checked as a one-line fact."""
    m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})\s*[:|-]\s*(.+)$", s or "")
    if not m:
        raise Refused("--evidence must look like '2026-10-15: SILVERBEES -5.6% and 7 buy blocks, 0 trades'")
    _d(m.group(1))
    return {"date": m.group(1), "text": memsafe.check_text(m.group(2), "why")}


def _last_support(it: dict) -> date | None:
    ds = [x["date"] for x in it.get("support", []) if x.get("date")]
    return date.fromisoformat(max(ds)) if ds else None


def auto_drop(led: dict, today: date) -> list[str]:
    gone = []
    for it in list(led["items"]):
        if it.get("status") != "candidate":
            continue
        ref = _last_support(it) or date.fromisoformat(it.get("first_seen", today.isoformat()))
        if (today - ref).days > CANDIDATE_DROP_WEEKS * 7:
            led["items"].remove(it)
            gone.append(it["id"])
    return gone


def add_candidate(root: Path, today: date, text: str, evidence: str, source: str = "daily") -> str:
    text = memsafe.check_text(text, "candidate")
    ev = parse_evidence(evidence)
    src = memsafe.check_text(source, "source")
    led = _ledger(root)
    auto_drop(led, today)
    lid = _lid(text)
    it = next((x for x in led["items"] if x["id"] == lid), None)
    if it is None:
        open_n = sum(1 for x in led["items"] if x.get("status") == "candidate")
        if open_n >= MAX_CANDIDATES:
            raise Refused(f"ledger has {MAX_CANDIDATES} open candidates; drop some first")
        it = {"id": lid, "text": text, "status": "candidate", "first_seen": ev["date"], "source": src,
              "support": [], "contradict": []}
        led["items"].append(it)
    if ev["date"] < it["first_seen"]:
        it["first_seen"] = ev["date"]
    if not any(x["date"] == ev["date"] for x in it["support"]):
        it["support"].append(ev)
    _save_ledger(root, led)
    return lid


def cmd_lesson(a, root: Path, now: datetime) -> str:
    today = now.date()
    led = _ledger(root)
    if a.action == "candidate":
        if not a.text or not a.evidence:
            raise Refused("lesson candidate needs --text and --evidence")
        lid = add_candidate(root, today, a.text, a.evidence, a.source or "daily")
        return f"candidate {lid} recorded"
    if a.action == "list":
        gone = auto_drop(led, today)
        if gone:
            _save_ledger(root, led)
        rows = []
        for it in led["items"]:
            sup = sorted({x["date"] for x in it.get("support", [])})
            rows.append(f"{it['id']} [{it['status']}] first {it.get('first_seen')} support {len(sup)} "
                        f"contra {len(it.get('contradict', []))}: {it['text'][:90]}")
        return "\n".join(rows) or "ledger empty"
    it = next((x for x in led["items"] if x["id"] == a.id), None)
    if it is None:
        raise Refused(f"no ledger item {a.id!r}")
    if a.action == "contradict":
        it.setdefault("contradict", []).append(parse_evidence(a.evidence or ""))
        _save_ledger(root, led)
        return f"{it['id']}: contradiction recorded"
    if a.action == "drop":
        reason = memsafe.check_text(a.reason or "", "why")
        it["status"], it["dropped"], it["drop_reason"] = "dropped", today.isoformat(), reason
        _save_ledger(root, led)
        return f"{it['id']} dropped"
    if a.action == "retire":
        if it.get("status") != "promoted" or not it.get("bullet"):
            raise Refused(f"{it['id']} is not a promoted lesson")
        reason = memsafe.check_text(a.reason or "", "why")
        p = Path(root) / "memory" / "lessons.md"
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        lines = text.splitlines()
        if it["bullet"] not in lines:
            raise Refused("that bullet is not in memory/lessons.md any more; edit the ledger status by dropping it")
        lines.remove(it["bullet"])
        if not any(ln.lstrip().startswith("- ") for ln in lines):
            lines.append("(none yet; the first Saturday review will fill this in)")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        arch = Path(root) / "archive" / "memory" / "lessons-retired.md"
        arch.parent.mkdir(parents=True, exist_ok=True)
        with arch.open("a", encoding="utf-8") as fh:
            fh.write(f"{today.isoformat()} retired: {it['bullet']} (reason: {reason})\n")
        it["status"], it["retired"] = "retired", today.isoformat()
        _save_ledger(root, led)
        return f"{it['id']} retired to archive/memory/lessons-retired.md"
    if a.action == "promote":
        if it.get("status") != "candidate":
            raise Refused(f"{it['id']} is {it.get('status')}, not a candidate")
        first = date.fromisoformat(it["first_seen"])
        if (today - first).days < LESSON_MIN_DAYS:
            raise Refused(f"first seen {first}; a lesson must hold at least {LESSON_MIN_DAYS} days "
                          f"({(today - first).days} so far)")
        dates = sorted({x["date"] for x in it.get("support", [])})
        weeks = {date.fromisoformat(d).isocalendar()[:2] for d in dates}
        if len(dates) < LESSON_MIN_SUPPORT or len(weeks) < 2:
            raise Refused(f"needs {LESSON_MIN_SUPPORT} supporting dates over 2+ ISO weeks (has {len(dates)} over {len(weeks)})")
        last = max(dates)
        if any(x["date"] > last for x in it.get("contradict", [])):
            raise Refused("a contradiction is newer than the last support")
        ev = "; ".join(f"{d[5:]}" for d in dates[:4])
        bullet = f"- {it['text']} (evidence: {ev}; held since {it['first_seen']})"
        memsafe.check_text(bullet[2:], "bullet")
        _append_lesson(root, bullet)
        it["status"], it["promoted"] = "promoted", today.isoformat()
        it["bullet"] = bullet
        _save_ledger(root, led)
        return f"{it['id']} promoted to memory/lessons.md"
    raise Refused(f"unknown lesson action {a.action!r}")


def _append_lesson(root: Path, bullet: str) -> None:
    p = Path(root) / "memory" / "lessons.md"
    text = p.read_text(encoding="utf-8") if p.exists() else "# Lessons\n\n## Lessons\n"
    if "## Lessons" not in text:
        text = text.rstrip() + "\n\n## Lessons\n"
    head, tail = text.split("## Lessons", 1)
    lines = [ln for ln in tail.splitlines() if ln.strip() and not ln.strip().startswith("(none yet")]
    if sum(1 for ln in lines if ln.lstrip().startswith("- ")) >= MAX_LESSONS:
        raise Refused(f"memory/lessons.md already has {MAX_LESSONS} bullets; the weekly review must delete one first")
    p.write_text(head + "## Lessons\n\n" + "\n".join(lines + [bullet]).strip("\n") + "\n", encoding="utf-8")


def lint_lessons(root: Path, today: date) -> tuple[list[str], list[str]]:
    """(errors, warnings) for memory/lessons.md."""
    p = Path(root) / "memory" / "lessons.md"
    if not p.exists():
        return ["memory/lessons.md is missing"], []
    text = p.read_text(encoding="utf-8")
    body = text.split("## Lessons", 1)[1] if "## Lessons" in text else ""
    bullets = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("- ")]
    errs, warns = [], []
    if len(bullets) > MAX_LESSONS:
        errs.append(f"{len(bullets)} bullets; the cap is {MAX_LESSONS}")
    for b in bullets:
        if "(evidence:" not in b or "held since" not in b:
            errs.append(f"missing '(evidence:' or 'held since': {b[:70]}")
        try:
            memsafe.check_text(b[2:], "bullet")
        except Refused as e:
            errs.append(f"{e}: {b[:60]}")
    led = _ledger(root)
    for it in led["items"]:
        if it.get("status") == "promoted":
            ref = _last_support(it)
            if ref and (today - ref).days > LESSON_STALE_WEEKS * 7:
                warns.append(f"stale (no support for {LESSON_STALE_WEEKS} weeks): {it['text'][:70]}")
    return errs, warns


def cmd_lint(a, root: Path, now: datetime) -> str:
    errs, warns = lint_lessons(root, now.date())
    out = [f"ERROR {e}" for e in errs] + [f"WARN {w}" for w in warns]
    if errs:
        raise Refused("lessons.md lint failed:\n" + "\n".join(out))
    return "\n".join(out + ["lessons.md ok"])


# ================================================================== performance
def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    except OSError:
        pass
    return rows


def by_alignment(root: Path, month: str) -> dict:
    """Days spent and realised P&L per alignment state per metal, from logs/alignment-D.jsonl (engine-written)."""
    res: dict = {}
    for p in sorted((Path(root) / "logs").glob(f"alignment-{month}-*.jsonl")):
        day = p.stem.split("alignment-", 1)[1]
        last = {}
        for r in _read_jsonl(p):
            if r.get("metal") and r.get("state"):
                last[r["metal"]] = r["state"]
        decs = _read_jsonl(Path(root) / "logs" / f"decisions-{day}.jsonl")
        for m, st in last.items():
            row = res.setdefault(m, {}).setdefault(st, {"days": 0, "pnl": 0.0})
            row["days"] += 1
            sym = memstore.etf(m) if m in memstore.METALS else m
            row["pnl"] = round(row["pnl"] + sum(float(d.get("pnl") or 0) for d in decs
                                                if d.get("symbol") == sym and d.get("action") == "SELL"), 2)
    return res


def layer_pnl(root: Path, month: str) -> dict:
    out = {"core_pnl": None, "tactical_pnl": None}
    tot = {"core": 0.0, "tactical": 0.0}
    seen = False
    for p in sorted((Path(root) / "logs").glob(f"decisions-{month}-*.jsonl")):
        for d in _read_jsonl(p):
            if d.get("layer") in tot and d.get("action") == "SELL":
                tot[d["layer"]] += float(d.get("pnl") or 0)
                seen = True
    if seen:
        out = {"core_pnl": round(tot["core"], 2), "tactical_pnl": round(tot["tactical"], 2)}
    return out


def perf_rows(root: Path) -> dict:
    eq = evaluate.load_equity(root)
    months = sorted({d.strftime("%Y-%m") for d, _ in eq})
    rows = []
    for mo in months:
        r = evaluate.evaluate(root, mo)
        if not r.get("available"):
            continue
        b = {x["name"]: x for x in r.get("benchmarks", [])}
        gold = b.get("Buy-and-hold GOLDBEES", {})
        mix = b.get("50/50 gold-silver", {})
        cash = next((x for n, x in b.items() if n.startswith("All-cash")), {})
        t = r.get("trades", {})
        row = {"month": mo, "start": r["start_value"], "end": r["end_value"], "pct": r["return_pct"],
               "green": r["green"], "bh_goldbees_pct": gold.get("return_pct") if gold.get("available") else None,
               "bh_gold_silver_pct": mix.get("return_pct") if mix.get("available") else None,
               "liquidbees_pct": cash.get("return_pct"),
               "excess_vs_goldbees": gold.get("excess_pct") if gold.get("available") else None,
               "max_dd_rs": r["max_drawdown"]["rupees"], "trades": t.get("trades"),
               "win_rate": t.get("win_rate_pct"), "costs": r["costs_paid"],
               "by_alignment": by_alignment(root, mo)}
        row.update(layer_pnl(root, mo))
        rows.append(row)
    allr = evaluate.evaluate(root, None)
    trial = None
    if allr.get("available"):
        gold = next((x for x in allr["benchmarks"] if x["name"] == "Buy-and-hold GOLDBEES"), {})
        trial = {"start": allr["start_value"], "end": allr["end_value"], "pct": allr["return_pct"],
                 "green": allr["green"], "bh_goldbees_pct": gold.get("return_pct") if gold.get("available") else None}
    return {"updated": None, "months": rows, "trial": trial}


def cmd_perf(a, root: Path, now: datetime) -> str:
    obj = perf_rows(root)
    obj["updated"] = now.isoformat(timespec="seconds")
    text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    while len(text.encode()) > 4096 and len(obj["months"]) > 1:
        obj["months"][0].pop("by_alignment", None)
        if len(json.dumps(obj, indent=1).encode()) > 4096:
            obj["months"] = obj["months"][1:]
        text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    if a.write:
        memstore.write_atomic(memstore.ltm_path(root, "performance.json"), text)
    last = obj["months"][-1] if obj["months"] else None
    return ("performance.json written\n" if a.write else "performance (not written, use --write)\n") + \
        (f"{last['month']}: {last['pct']:+.2f}% green={last['green']} vs GOLDBEES {last['bh_goldbees_pct']}" if last else "no months yet")


# ================================================================== main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("regime")
    r.add_argument("--write", action="store_true")
    c = sub.add_parser("calendar")
    c.add_argument("action", choices=["add", "list", "remove", "fill"])
    c.add_argument("--date")
    c.add_argument("--type")
    c.add_argument("--time")
    c.add_argument("--metals")
    c.add_argument("--impact", type=int, default=2)
    c.add_argument("--expect")
    c.add_argument("--source")
    c.add_argument("--confirmed", action="store_true")
    c.add_argument("--by")
    t = sub.add_parser("thesis")
    t.add_argument("action", choices=["set", "show"])
    t.add_argument("--metal")
    t.add_argument("--stance")
    t.add_argument("--invalidate")
    t.add_argument("--review-by")
    t.add_argument("--evidence", action="append")
    t.add_argument("--risk", action="append")
    sub.add_parser("check")
    le = sub.add_parser("lesson")
    le.add_argument("action", choices=["candidate", "contradict", "promote", "drop", "retire", "list"])
    le.add_argument("id", nargs="?")
    le.add_argument("--text")
    le.add_argument("--evidence")
    le.add_argument("--source")
    le.add_argument("--reason")
    sub.add_parser("lint")
    pf = sub.add_parser("perf")
    pf.add_argument("--write", action="store_true")
    return ap


def main(argv=None) -> int:
    utf8_stdout()
    a = build_parser().parse_args(argv)
    fn = {"regime": cmd_regime, "calendar": cmd_calendar, "thesis": cmd_thesis, "check": cmd_check,
          "lesson": cmd_lesson, "lint": cmd_lint, "perf": cmd_perf}[a.cmd]
    try:
        print(fn(a, Path(a.root), parse_now(a.now)))
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    except Exception as e:       # data fetch etc.: report, write nothing, exit non-zero (engine defaults stay safe)
        print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
