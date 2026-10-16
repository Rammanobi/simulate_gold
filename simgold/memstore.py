"""Read/write helpers for memory/ltm and memory/stm plus the live alignment computation (paper trading).

Everything here is advisory input: missing, corrupt or stale files fall back to the safe reading (LTM FLAT,
ceiling neutral/lower), never an exception. Machine JSON in memory/ltm and memory/stm is written only through
tools/memory_ltm.py and tools/memory_stm.py (atomic writes via write_json).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from . import brief, config, market, regime

METALS = ("GOLD", "SILVER", "COPPER")
LTM_DIR = ("memory", "ltm")
STM_DIR = ("memory", "stm")
RECENT_CLOSES = 25


def ltm_path(root, name: str) -> Path:
    return Path(root).joinpath(*LTM_DIR, name)


def stm_path(root, *name: str) -> Path:
    return Path(root).joinpath(*STM_DIR, *name)


def etf(metal: str) -> str:
    """GOLD -> GOLDBEES (the instrument symbol the engine logs use)."""
    return config.ASSETS[metal]["ticker"].split(".")[0]


def write_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, obj) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def write_compact(path: Path, obj) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def load(root, rel: str, default=None):
    return brief.load_json(Path(root) / rel, default)


def regime_data(root) -> dict:
    d = brief.load_json(ltm_path(root, "regime.json"), {})
    return d if isinstance(d, dict) else {}


def window_data(root) -> dict:
    d = brief.load_json(stm_path(root, "window.json"), {})
    return d if isinstance(d, dict) else {}


def theses(root) -> dict:
    d = brief.load_json(ltm_path(root, "theses.json"), {})
    return d if isinstance(d, dict) else {}


def calendar_events(root) -> list[dict]:
    d = brief.load_json(ltm_path(root, "calendar.json"), {})
    ev = d.get("events") if isinstance(d, dict) else None
    return [e for e in ev if isinstance(e, dict) and e.get("date") and e.get("type")] if isinstance(ev, list) else []


CANON = {"us_cpi": "CPI", "us_jobs": "JOBS", "fomc": "FOMC", "rbi": "RBI", "union_budget": "BUDGET",
         "hindcopper_results": "HINDCOPPER_RESULTS"}


def engine_events(root) -> list[dict]:
    """state/events.json as [{date, type (CPI/FOMC/JOBS/RBI/HINDCOPPER_RESULTS/BUDGET), time}] (the engine's own parser)."""
    out = []
    for e in brief.normalise_events(brief.load_json(Path(root) / "state" / "events.json", [])):
        out.append({"date": e["date"], "type": CANON.get(e["type"], str(e["type"]).upper()), "time": e.get("time")})
    return out


# ------------------------------------------------------------------ overrides (computed from state, never from an LLM)
def regime_stale(entry: dict, today: date) -> bool:
    """True when the metal's regime entry is missing, flagged stale, or older than 3 trading sessions."""
    if not isinstance(entry, dict) or entry.get("stale") or entry.get("label") not in regime.LTM_LABELS:
        return True
    try:
        asof = date.fromisoformat(str(entry.get("asof"))[:10])
    except ValueError:
        return True
    if asof >= today:
        return False
    return market.trading_days_between(asof + timedelta(days=1), today - timedelta(days=1)) > regime.STALE_SESSIONS


def thesis_cautious(root, metal: str) -> bool:
    t = theses(root).get(metal)
    if not isinstance(t, dict):
        return False
    return t.get("stance") == "cautious" or bool(t.get("invalidated"))


def event_override(root, metal: str, today: date) -> tuple[str, str]:
    """('none'|'neutral'|'block', note) from the engine's events.json (same rules the premarket playbook uses)."""
    evs = engine_events(root)
    tomorrow = today + timedelta(days=1)
    worst, why = 0, ""

    def bump(level: int, text: str) -> None:
        nonlocal worst, why
        if level > worst:
            worst, why = level, text

    for e in evs:
        typ, d = str(e.get("type", "")).upper(), e.get("date")
        if not isinstance(d, date):
            continue
        within24 = d in (today, tomorrow)
        label = f"{typ} {d.isoformat()}"
        if typ in ("CPI", "JOBS") and metal in ("GOLD", "SILVER") and within24:
            bump(1, label)
        elif typ in ("FOMC", "BUDGET") and metal in ("GOLD", "SILVER"):
            if d == today:
                bump(2, label)
            elif within24:
                bump(1, label)
        elif typ == "RBI" and within24:
            bump(1, label)
        elif typ == "HINDCOPPER_RESULTS" and metal == "COPPER":
            if today <= d <= market.add_trading_days(today, 2):
                bump(2, label)
    return ("none", "neutral", "block")[worst], why


def month_end_window(today: date) -> bool:
    """Last 3 trading days of the month (and non-trading days after the third-last)."""
    days = market.trading_days_in_month(today.year, today.month)
    return len(days) >= 3 and today >= days[-3]


def cushion_low(root, params: dict, now: datetime) -> bool:
    try:
        return brief.portfolio_view(root, params, now)["cushion"] < params["routines"]["cushion_below"]
    except Exception:
        return False


def stm_for(root, metal: str, window: dict | None = None) -> dict:
    """Live STM state for a metal: recent closes + the latest price (state/last_prices.json); falls back to the
    state stored in window.json, then FLAT."""
    w = (window if window is not None else window_data(root)).get("metals", {})
    row = w.get(metal) if isinstance(w, dict) else None
    row = row if isinstance(row, dict) else {}
    closes = row.get("closes")
    lp = brief.load_json(Path(root) / "state" / "last_prices.json", {})
    live = None
    if isinstance(lp, dict) and isinstance(lp.get(etf(metal)), dict):
        live = lp[etf(metal)].get("price")
    if isinstance(closes, list) and len(closes) >= 11:
        return regime.stm_state(closes, live_px=live if _live_is_newer(lp, metal, row.get("closes_asof")) else None)
    return {"state": row.get("stm_state") if row.get("stm_state") in regime.STM_STATES else "FLAT"}


def _live_is_newer(lp, metal: str, closes_asof) -> bool:
    """Use the live price only when it is from a later day than the last close already in the window."""
    try:
        ts = datetime.fromisoformat(str(lp[etf(metal)]["ts"])).date()
        return closes_asof is None or ts > date.fromisoformat(str(closes_asof)[:10])
    except (KeyError, TypeError, ValueError):
        return False


def current_alignment(root, now: datetime | None = None, params: dict | None = None) -> dict[str, dict]:
    """{metal: align() record plus why/asof}. Never raises."""
    now = now or market.now_ist()
    p = params or config.load_params()
    root = Path(root)
    reg = regime_data(root)
    win = window_data(root)
    cl = cushion_low(root, p, now)
    out: dict[str, dict] = {}
    for m in METALS:
        try:
            entry = (reg.get("metals") or {}).get(m) or {}
            stale = regime_stale(entry, now.date())
            ev, ev_note = event_override(root, m, now.date())
            st = stm_for(root, m, win)
            ov = {"stale": stale, "event": ev, "event_note": ev_note, "thesis_cautious": thesis_cautious(root, m),
                  "month_end": month_end_window(now.date()), "cushion_low": cl,
                  "vol_high": entry.get("vol_regime") == "HIGH" and not stale}
            rec = regime.align(entry.get("label") if entry else None, st.get("state"), ov, metal=m)
            bits = []
            if entry.get("votes") is not None:
                bits.append(f"{entry['votes']}/{entry.get('tests', 6)} trend votes")
            if entry.get("since"):
                bits.append(f"since {entry['since']}")
            if st.get("z_day") is not None:
                bits.append(f"today z={st['z_day']:+.1f}")
            rec["why"] = ", ".join(bits)
            rec["asof"] = entry.get("asof")
        except Exception as e:   # advisory only: the safest reading, never a crash
            rec = regime.align(None, None, {"stale": True}, metal=m)
            rec["why"] = f"error: {type(e).__name__}"
        out[m] = rec
    return out


def alignment_lines(root, now: datetime | None = None) -> list[str]:
    al = current_alignment(root, now)
    return [regime.format_line(al[m]) for m in METALS]
