#!/usr/bin/env python3
"""Desk files for strategy v3 (DORMANT; paper trading only): validated writer and readers for analysis/D/<desk>.json.

  python tools/desk.py write --desk macro|gold|silver|copper|risk --file /tmp/desk.json [--date D]
  python tools/desk.py list [--date D] [--json]      # status of every desk file of the day (ok|abstain|invalid|missing; memory also degraded)
  python tools/desk.py quorum [--date D]             # prints OK or NOT OK (exit 0 either way): memory desk WITH data + enough evidence desks
  python tools/desk.py memory [--write] [--date D]   # the code-only memory desk (analysis/D/memory.json): default, current weights, brake...

A desk file is evidence for the 08:50 / 14:40 allocation, never an order: it has no weight, size, quantity, cash or symbol field and the
engine never reads it. `write` checks the schema, the memsafe text rules (no instructions, no URLs) and the size cap, and refuses
(exit 2, nothing written) on any problem. The file written is the canonical compact JSON, at most 2.5 KB. `tools/desk.py` writes ONLY
under analysis/<date>/ (routine-owned). It never touches memory/ltm/calibration.json: parallel desk sessions would all edit that one file
and conflict. tools/calibration.py reads the committed analysis/D/<desk>.json files when it scores (desk calibration is derived from them).

--date is YYYY-MM-DD exactly (20261118 is refused). Quorum OK = the memory desk written WITH price history (a memory desk without
history is `degraded` and does not count) plus at least desks.quorum (2) of macro, gold, silver, copper with status ok; abstain,
invalid and the risk desk never count.

Desk file schema (all keys required except events, risks, unit):
  {"desk", "date", "written", "model", "facts": [{"metric","value","unit","source","asof"}] (<= 12),
   "view": {"GOLD"|"SILVER"|"COPPER": {"dir_5d": up|flat|down, "p_up", "p_down", "score": -2..2, "why" (<= 200)}},
   "risks": [<= 4 one-liners], "events": [{"date","type","confirmed"}], "confidence": 0..1, "abstain": true|false}
The gold, silver and copper desks may only give their own metal; macro and risk may give any. A desk that abstains may have an empty view.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import ROOT, iso_day, parse_now, utf8_stdout

import alloc_ctx
import calibration
from simgold import allocation, brief, config, memsafe, memstore

DESKS = alloc_ctx.LLM_DESKS
METAL_OF = {"gold": "GOLD", "silver": "SILVER", "copper": "COPPER"}
METALS = ("GOLD", "SILVER", "COPPER")
KEYS = ("desk", "date", "written", "model", "facts", "view", "risks", "events", "confidence", "abstain")
REQUIRED = ("desk", "date", "facts", "view", "confidence")
FACT_KEYS = {"metric", "value", "unit", "source", "asof"}
VIEW_KEYS = {"dir_5d", "p_up", "p_down", "score", "why"}
EVENT_TYPES = ("CPI", "FOMC", "JOBS", "RBI", "HINDCOPPER_RESULTS", "BUDGET", "NSE_HOLIDAY", "MONTH_END", "INDIA_DUTY",
               "CHINA_DATA", "OTHER")
MAX_BYTES = 2560
MAX_RAW = 16384
MAX_FACTS, MAX_RISKS, MAX_EVENTS = 12, 4, 8
MEMORY_MAX = 4096
SLACK = timedelta(minutes=5)


class Refused(Exception):
    pass


def _num(x):
    """A finite float, or None for a bool, a non-number, NaN, infinity or an int too large for a float (10**400 raises OverflowError)."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _day(s, now: datetime) -> str:
    """--date as exactly YYYY-MM-DD, default today."""
    if s is None:
        return now.date().isoformat()
    try:
        return iso_day(s).isoformat()
    except ValueError:
        raise Refused(f"--date must look like 2026-11-18 (YYYY-MM-DD) and be a real date, got {str(s)[:20]!r}")


def _txt(x, kind: str, what: str, cap: int | None = None) -> str:
    try:
        t = memsafe.check_text(x, kind)
    except memsafe.Refused as e:
        raise Refused(f"{what}: {e}")
    if cap is not None and len(t) > cap:
        raise Refused(f"{what} is {len(t)} characters; keep it under {cap}")
    return t


def validate_desk(obj, desk: str, day: str, now: datetime | None = None, root=None) -> tuple[dict, list[str]]:
    """Return (clean desk object, warnings) or raise Refused. now=None skips the clock rules (re-reading a stored file)."""
    if desk not in DESKS:
        raise Refused(f"--desk must be one of {'/'.join(DESKS)}")
    if not isinstance(obj, dict):
        raise Refused("the desk file must be a JSON object")
    bad = sorted(str(k) for k in obj if k not in KEYS)
    if bad:
        raise Refused(f"unknown key(s): {', '.join(bad)[:80]} (a desk file is evidence: no weight, size, quantity, cash or symbol field)")
    miss = [k for k in REQUIRED if k not in obj]
    if miss:
        raise Refused(f"missing key(s): {', '.join(miss)}")
    if obj["desk"] != desk:
        raise Refused(f"desk is {obj['desk']!r} but --desk is {desk!r}")
    if obj["date"] != day:
        raise Refused(f"date is {obj['date']!r}; this desk file is for {day}")
    warn: list[str] = []
    out: dict = {"desk": desk, "date": day}
    wr = obj.get("written")
    if wr is None:
        wr = (now or datetime.fromisoformat(day + "T00:00:00+05:30")).isoformat(timespec="seconds")
    if allocation._dt(wr) is None:                                              # noqa: SLF001
        raise Refused("written is not an ISO timestamp")
    wdt = allocation._dt(wr, now.tzinfo if now else None)                      # noqa: SLF001
    if now is not None and wdt is not None and wdt - now > SLACK:
        raise Refused("written is in the future")
    out["written"] = str(wr)[:25]
    model = obj.get("model", "")
    if not isinstance(model, str) or len(model) > 40 or not all(c.isalnum() or c in "._- " for c in model):
        raise Refused("model must be a short name such as sonnet")
    if model:
        model = _txt(model, "why", "model", 40)
    out["model"] = model
    # ---- facts
    facts = obj["facts"]
    if not isinstance(facts, list) or len(facts) > MAX_FACTS:
        raise Refused(f"facts must be a list of at most {MAX_FACTS}")
    cf = []
    for i, f in enumerate(facts, 1):
        if not isinstance(f, dict) or set(f) - FACT_KEYS or not {"metric", "value", "source", "asof"} <= set(f):
            raise Refused(f"fact {i}: use exactly metric, value, unit (optional), source, asof")
        v = f["value"]
        if _num(v) is None:
            if isinstance(v, bool) or not isinstance(v, str):
                raise Refused(f"fact {i}: value must be a number or short text such as n/a")
            v = _txt(v, "why", f"fact {i} value", 30)
        row = {"metric": _txt(f["metric"], "why", f"fact {i} metric", 60), "value": v}
        if f.get("unit"):
            row["unit"] = _txt(f["unit"], "why", f"fact {i} unit", 16)
        row["source"] = _txt(f["source"], "source", f"fact {i} source")
        row["asof"] = _txt(f["asof"], "why", f"fact {i} asof", 40)
        if not any(ch.isdigit() for ch in row["asof"]):
            raise Refused(f"fact {i}: asof needs a date or time")
        cf.append(row)
    out["facts"] = cf
    # ---- view
    view, abstain = obj["view"], obj.get("abstain", False)
    if not isinstance(abstain, bool):
        raise Refused("abstain must be true or false")
    if not isinstance(view, dict):
        raise Refused("view must be an object keyed by GOLD, SILVER, COPPER")
    allowed = {METAL_OF[desk]} if desk in METAL_OF else set(METALS)
    for k in view:
        if k not in METALS:
            raise Refused(f"view names {str(k)[:20]!r}: only GOLD, SILVER, COPPER (never a symbol or cash)")
        if k not in allowed:
            raise Refused(f"the {desk} desk may only give {METAL_OF.get(desk)}, not {k}")
    if not view and not abstain:
        raise Refused("view is empty: give the call or set abstain to true")
    if desk in METAL_OF and not abstain and METAL_OF[desk] not in view:
        raise Refused(f"the {desk} desk must give {METAL_OF[desk]}")
    reg = (memstore.regime_data(root).get("metals") or {}) if root is not None else {}
    cv = {}
    for m in METALS:
        if m not in view:
            continue
        v = view[m]
        if not isinstance(v, dict) or set(v) - VIEW_KEYS or not VIEW_KEYS <= set(v):
            raise Refused(f"view {m}: use exactly dir_5d, p_up, p_down, score, why")
        if v["dir_5d"] not in ("up", "flat", "down"):
            raise Refused(f"view {m}: dir_5d must be up, flat or down")
        pu, pd_, sc = _num(v["p_up"]), _num(v["p_down"]), _num(v["score"])
        if pu is None or pd_ is None or not (0 <= pu <= 1 and 0 <= pd_ <= 1):
            raise Refused(f"view {m}: p_up and p_down must be numbers in [0, 1]")
        if pu + pd_ > 1.02:
            raise Refused(f"view {m}: p_up + p_down is {pu + pd_:.2f}; the rest is flat, so it cannot exceed 1")
        if sc is None or not -2 <= sc <= 2:
            raise Refused(f"view {m}: score must be a number from -2 to +2")
        if (v["dir_5d"] == "up" and pd_ > pu) or (v["dir_5d"] == "down" and pu > pd_):
            warn.append(f"{m}: dir_5d {v['dir_5d']} contradicts p_up {pu:g} / p_down {pd_:g}")
        label = (reg.get(m) or {}).get("label")
        dated = sum(1 for f in cf if any(ch.isdigit() for ch in f["asof"]))
        if sc > 1 and label == "DOWN" and dated < 3:
            warn.append(f"{m}: score {sc:+g} in a DOWN regime needs 3 dated facts (you gave {dated})")
        if sc >= 2 and label not in ("UP",):
            warn.append(f"{m}: score +2 needs the LTM label UP (it is {label})")
        cv[m] = {"dir_5d": v["dir_5d"], "p_up": round(pu, 4), "p_down": round(pd_, 4), "score": sc if sc != int(sc) else int(sc),
                 "why": _txt(v["why"], "why", f"view {m} why", 200)}
    out["view"] = cv
    # ---- risks, events, confidence, abstain
    risks = obj.get("risks", [])
    if not isinstance(risks, list) or len(risks) > MAX_RISKS:
        raise Refused(f"risks must be a list of at most {MAX_RISKS} one-liners")
    out["risks"] = [_txt(r, "note", f"risk {i}") for i, r in enumerate(risks, 1)]
    ev = obj.get("events", [])
    if not isinstance(ev, list) or len(ev) > MAX_EVENTS:
        raise Refused(f"events must be a list of at most {MAX_EVENTS}")
    cev = []
    for i, e in enumerate(ev, 1):
        if not isinstance(e, dict) or set(e) - {"date", "type", "confirmed"} or not {"date", "type"} <= set(e):
            raise Refused(f"event {i}: use date, type, confirmed")
        try:
            d = iso_day(e["date"])
        except ValueError:
            raise Refused(f"event {i}: date must look like 2026-12-10 (YYYY-MM-DD)")
        if e["type"] not in EVENT_TYPES:
            raise Refused(f"event {i}: type must be one of {'/'.join(EVENT_TYPES)}")
        if "confirmed" in e and not isinstance(e["confirmed"], bool):
            raise Refused(f"event {i}: confirmed must be true or false")
        cev.append({"date": d.isoformat(), "type": e["type"], "confirmed": bool(e.get("confirmed", False))})
    out["events"] = cev
    c = _num(obj["confidence"])
    if c is None or not 0 <= c <= 1:
        raise Refused("confidence must be a number in [0, 1]")
    out["confidence"] = round(c, 4)
    out["abstain"] = abstain
    if len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_BYTES:
        raise Refused(f"the desk file is over {MAX_BYTES} bytes (2.5 KB): fewer facts, shorter text")
    return out, warn


def desk_file(root, day: str, desk: str) -> Path:
    return Path(root) / "analysis" / day / f"{desk}.json"


def read_status(root, day: str, desk: str):
    """(status, clean desk or None, reason). status: ok | abstain | invalid | missing."""
    f = desk_file(root, day, desk)
    if not f.exists():
        return "missing", None, "no file"
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        clean, _w = validate_desk(obj, desk, day, None, root)
    except (OSError, ValueError, Refused) as e:
        return "invalid", None, str(e)[:120]
    return ("abstain" if clean["abstain"] else "ok"), clean, ""


# ------------------------------------------------------------------ write
def cmd_write(a, root: Path, now: datetime, p: dict) -> str:
    day = _day(a.date, now)
    d = date.fromisoformat(day)
    if d < now.date() or (d - now.date()).days > allocation.MAX_ADVANCE_DAYS:
        raise Refused(f"--date {day} must be today or at most {allocation.MAX_ADVANCE_DAYS} days ahead")
    src = Path(a.file)
    try:
        if not src.is_file() or src.stat().st_size > MAX_RAW:
            raise Refused(f"--file must be a JSON file of at most {MAX_RAW} bytes")
        obj = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read --file as JSON: {e}")
    clean, warn = validate_desk(obj, a.desk, day, now, root)
    memstore.write_atomic(desk_file(root, day, a.desk), json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n")
    msg = [f"desk {a.desk} {day} written ({len(clean['facts'])} facts, {'abstain' if clean['abstain'] else 'view ' + '/'.join(clean['view'])})",
           "calibration: nothing to record here (the ledger is derived from the committed desk files when it scores)"]
    try:                                                   # advice only: a misquoted number is a WARNING, never a refusal
        from simgold import factcheck
        fc = factcheck.check_desk(root, now.date().isoformat(), a.desk, clean)
        warn_fc = factcheck.warning_lines(fc)
    except Exception:                                      # noqa: BLE001
        warn_fc = []
    return "\n".join(msg + [f"WARNING: {w}" for w in warn] + warn_fc)


# ------------------------------------------------------------------ list, quorum
def memory_status(root, day: str) -> str:
    """ok | degraded | invalid | missing. `degraded` = the memory desk exists but was written without price history (default weights
    n/a): it is not usable for the quorum, the allocation would be built on numbers it does not have."""
    f = desk_file(root, day, "memory")
    if not f.exists():
        return "missing"
    d = brief.load_json(f, None)
    if not isinstance(d, dict) or d.get("desk") != "memory" or d.get("date") != day:
        return "invalid"
    dflt = d.get("default")
    if not isinstance(dflt, dict) or not dflt:
        return "degraded"
    return "ok"


def _row(root, day: str, desk: str) -> dict:
    st, clean, why = read_status(root, day, desk)
    r = {"desk": desk, "status": st}
    if clean:
        r["confidence"] = clean["confidence"]
        r["view"] = {m: f"{v['dir_5d']} pu {v['p_up']:g} pd {v['p_down']:g} s {v['score']:+g}" for m, v in clean["view"].items()}
    elif why and st == "invalid":
        r["why"] = why
    return r


def cmd_list(a, root: Path, now: datetime, p: dict) -> str:
    day = _day(a.date, now)
    rows = [_row(root, day, d) for d in DESKS]
    rows.append({"desk": "memory", "status": memory_status(root, day)})
    if a.json:
        return json.dumps({"date": day, "desks": rows}, indent=1)
    L = [f"desks {day}"]
    for r in rows:
        extra = "; ".join(f"{m} {v}" for m, v in (r.get("view") or {}).items())
        L.append(f"  {r['desk']:<7} {r['status']:<8}" + (f" conf {r['confidence']:g} | {extra}" if r.get("confidence") is not None else "")
                 + (f" ({r['why']})" if r.get("why") else ""))
    return "\n".join(L)


def quorum(root, day: str, p: dict) -> dict:
    cfg = allocation.settings(p)
    fan = "full" if str(cfg["desks"].get("fanout")) == "full" else "compact"
    need = int(cfg["desks"].get("quorum") or 2) if isinstance(cfg["desks"].get("quorum"), (int, float)) else 2
    st = {d: read_status(root, day, d)[0] for d in DESKS}
    mstat = memory_status(root, day)
    mem = mstat == "ok"
    evid = [d for d in alloc_ctx.EVIDENCE_DESKS if st[d] == "ok"]
    expected = list(alloc_ctx.EVIDENCE_DESKS) + (["risk"] if fan == "full" else [])
    missing = [d for d in expected if st[d] != "ok"]
    ok = mem and len(evid) >= need
    return {"ok": ok, "fanout": fan, "need": need, "memory": mem, "memory_status": mstat, "usable": evid, "missing": missing, "status": st,
            "confidence_penalty": round(0.10 * len(missing), 2)}


def cmd_quorum(a, root: Path, now: datetime, p: dict) -> str:
    day = _day(a.date, now)
    q = quorum(root, day, p)
    head = "OK" if q["ok"] else "NOT OK"
    ms = q["memory_status"]
    why = "" if q["ok"] else (
        " (memory desk degraded: it was written without price history and does not count; rerun python tools/desk.py memory --write "
        "when the data download works)" if ms == "degraded" else
        " (memory desk invalid: rerun python tools/desk.py memory --write)" if ms == "invalid" else
        " (memory desk missing: run python tools/desk.py memory --write)" if not q["memory"] else
        f" (need {q['need']} evidence desks)")
    return (f"{head}{why} | fanout {q['fanout']} | memory {ms} | evidence desks usable "
            f"{','.join(q['usable']) or 'none'} ({len(q['usable'])}/{q['need']} needed) | missing {','.join(q['missing']) or 'none'} | "
            f"risk {q['status']['risk']}{' (optional in compact mode: you play it)' if q['fanout'] == 'compact' else ''} | "
            f"confidence hint: -{q['confidence_penalty']:.2f} for missing desks (A7)")


# ------------------------------------------------------------------ memory desk (code only)
def _cal_summary(root, now: datetime):
    """The calibration summary for the memory desk; a ledger that cannot be summarised is n/a, never a crash."""
    try:
        return calibration.summary(root, now)
    except Exception:                                    # noqa: BLE001
        return None


def build_memory(root, now: datetime, p: dict, snap: dict | None = None) -> dict:
    snap = snap or alloc_ctx.snapshot(root, now, p, fetch=True)
    cfg = snap["cfg"]
    root = Path(root)
    day = now.date().isoformat()
    default = snap["default"]
    cov = snap["cov"]
    sig = {}
    if cov:
        for s in alloc_ctx.SYMS:
            v = (cov.get(s) or {}).get(s)
            if isinstance(v, (int, float)) and v > 0:
                sig[s] = math.sqrt(v)
    corr = {}
    if sig and cov:
        for i, a in enumerate(alloc_ctx.SYMS):
            for b in alloc_ctx.SYMS[i + 1:]:
                if a in sig and b in sig and isinstance(cov.get(a, {}).get(b), (int, float)):
                    corr[f"{a[:4]}-{b[:4]}"] = round(cov[a][b] / (sig[a] * sig[b]), 2)
    reg = memstore.regime_data(root).get("metals") or {}
    win = (memstore.window_data(root).get("metals") or {})
    th = memstore.theses(root)
    trend_ = {}
    for s in alloc_ctx.SYMS:
        m = alloc_ctx.METAL[s]
        r, w, t = reg.get(m) or {}, win.get(m) or {}, th.get(m) if isinstance(th, dict) else None
        trend_[m] = {"sym": s, "on": (snap["on"] or {}).get(s), "ltm": r.get("label"), "since": r.get("since"), "r63": r.get("r63"),
                     "vol": r.get("vol_regime"), "stm": w.get("stm_state"), "m5": w.get("m5_pct"), "align": (snap.get("align") or {}).get(m),
                     "thesis": (t or {}).get("stance") if isinstance(t, dict) else None,
                     "invalidated": bool(isinstance(t, dict) and t.get("invalidated")),
                     "sigma_ann": round(sig[s], 3) if s in sig else None,
                     "move_5d_pct": round(sig[s] * math.sqrt(5 / 252) * 100, 2) if s in sig else None}
    if default:
        shares = {s: round(default[s] / sum(default.values()), 4) for s in alloc_ctx.SYMS} if sum(default.values()) > 0 else None
    else:
        shares = None
    dvol = None
    if default and cov:
        dvol = allocation.port_vol(default, cov)
    value = snap["value"]
    ev = []
    try:
        for e in memstore.calendar_events(root):
            dd = date.fromisoformat(str(e["date"])[:10])
            if now.date() <= dd <= now.date() + timedelta(days=7):
                ev.append(f"{dd.isoformat()} {e['type']}")
    except Exception:                                    # noqa: BLE001
        pass
    last = alloc_ctx.last_alloc(root, now)
    perf = brief.load_json(memstore.ltm_path(root, "performance.json"), {})
    months = perf.get("months") if isinstance(perf, dict) else None
    out = {"desk": "memory", "date": day, "written": now.isoformat(timespec="seconds"), "model": "code",
           "config": {"mode": p["strategy"]["mode"], "shadow": cfg["shadow"], "phase": cfg["phase"], "ai_trust": cfg["ai_trust"],
                      "invested_target": cfg["invested_target"], "fanout": cfg["desks"].get("fanout"), "max_tilt": cfg["max_tilt"],
                      "ai_caps": cfg["ai_caps"], "off_trend_caps": cfg["off_trend_caps"]},
           "default": {s: round(v, 4) for s, v in default.items()} if default else None, "default_shares": shares,
           "current": {s: round(v, 4) for s, v in snap["cur"].items()}, "cash_weight": round(max(0.0, 1 - sum(snap["cur"].values())), 4),
           "value": value, "peak": snap["peak"], "drawdown_pct": round(snap["dd"] * 100, 2), "brake_level": snap["level"],
           "halted": snap["halted"], "account_stop": snap["stop"], "stop_distance_rs": snap["stop_dist"],
           "stop_distance_pct": round(snap["stop_dist"] / value * 100, 2) if value else None,
           "turnover_left": {"day": round(snap["day_left"], 3), "week": round(snap["week_left"], 3)},
           "halve": snap["halve"], "results_window": snap["zero"], "trend": trend_, "port_vol_default": round(dvol, 3) if dvol else None,
           "corr": corr, "prices": snap["prices"], "events_7d": ev[:8],
           "by_alignment": ((months or [{}])[-1].get("by_alignment") if months else None),
           "alloc_last": ({"date": last["date"], "mode": last["mode"], "default": last["alloc"].get("default"),
                           "final": last["alloc"].get("final"), "advice": last["alloc"].get("advice")} if last else None),
           "calibration": _cal_summary(root, now), "warnings": snap["warnings"][:4],
           "inputs": {"default": {s: round(v, 6) for s, v in default.items()} if default else None, "on": snap["on"],
                      "sigma": snap["sigma"], "cov": {a: {b: round(v, 6) for b, v in row.items()} for a, row in cov.items()} if cov else None,
                      "source": snap["source"]}}
    for drop in ("by_alignment", "alloc_last", "calibration", "corr", "events_7d", "prices"):
        if len(json.dumps(out, separators=(",", ":")).encode()) <= MEMORY_MAX:
            break
        out.pop(drop, None)
    return out


def cmd_memory(a, root: Path, now: datetime, p: dict) -> str:
    day = _day(a.date, now)
    if day != now.date().isoformat():
        raise Refused("the memory desk is built from today's state: --date must be today")
    m = build_memory(root, now, p)
    text = json.dumps(m, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(text.encode()) > MEMORY_MAX + 1024:
        raise Refused(f"memory desk would be {len(text.encode())} bytes")
    if a.write:
        memstore.write_atomic(Path(root) / "analysis" / day / "memory.json", text)
    d = m["default"]
    fm = lambda x: "n/a" if not x else "/".join(f"{x[s] * 100:.0f}" for s in alloc_ctx.SYMS)            # noqa: E731
    return (f"memory desk {day} {'written to analysis/' + day + '/memory.json' if a.write else '(not written, use --write)'}\n"
            f"default G/S/C {fm(d)} | current {fm(m['current'])} | value {m['value']:,.0f} dd {m['drawdown_pct']}% brake {m['brake_level']} "
            f"stop distance Rs {m['stop_distance_rs']:,.0f} | turnover left day {m['turnover_left']['day']} week {m['turnover_left']['week']}"
            + ("".join("\nwarning: " + w for w in m["warnings"])))


# ------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", allow_abbrev=False)
    w.add_argument("--desk", required=True)
    w.add_argument("--file", required=True)
    w.add_argument("--date")
    ls = sub.add_parser("list", allow_abbrev=False)
    ls.add_argument("--date")
    ls.add_argument("--json", action="store_true")
    q = sub.add_parser("quorum", allow_abbrev=False)
    q.add_argument("--date")
    m = sub.add_parser("memory", allow_abbrev=False)
    m.add_argument("--write", action="store_true")
    m.add_argument("--date")
    return ap


def main(argv=None) -> int:
    utf8_stdout()
    a = build_parser().parse_args(argv)
    fn = {"write": cmd_write, "list": cmd_list, "quorum": cmd_quorum, "memory": cmd_memory}[a.cmd]
    try:
        print(fn(a, Path(a.root), parse_now(a.now), config.load_params()))
        return 0
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    except (OverflowError, RecursionError) as e:          # a number or nesting too large to handle: a refusal, never a traceback
        print(f"REFUSED: the input is out of range ({type(e).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
