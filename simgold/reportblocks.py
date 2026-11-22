"""Optional report blocks: India price gap, Default vs AI vs engine target, paper twins. Read-only, never trades.

Each `*_view(...)` returns a small JSON-able dict or None. None means "hide the block": no data, a corrupt or stale file, a switch that is off,
or any exception (every builder is wrapped, so a bad file can never cost the daily email). Absent data adds NOTHING to the day dict, so the
compact email of a day without these files stays byte-identical.

Sources (all written by other parts of the system, read here defensively):
  state/pricegap.json     simgold/pricegap.py (only shown when `pricegap.enabled` and the file is dated the report's day)
  logs/decisions-D.jsonl  the engine's ALLOC record (default, AI proposal, clamps, final), else state/allocation.json (routine proposal).
                          The "engine target" column is what the engine aimed for (the v2 targets in shadow, the final weights otherwise);
                          the dashboard key stays `weights.executed`.
  state/twins.json        simgold/twins.py (PR #78). Read two ways: the summary keys `since`, `asof`, `twins: {id: {value, return_pct, max_dd_pct,
                          days, label}}` when present, else the raw books {"start_day", "last_day", "books": {id: {start_value, value, max_dd
                          (a fraction)}}}: return = value / start_value - 1, labels from LABELS below. Keys without `_pct` are fractions.
                          Unknown shapes hide the block.
"""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

log = logging.getLogger("reportblocks")

NAMES = {"GOLDBEES": "Gold", "SILVERBEES": "Silver", "HINDCOPPER": "Copper"}
SYMS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")
MAX_TWINS = 7
_PHASE_BY_MODE = {"shadow_v2_trades": "shadow", "d5fi_ai_logged_only": "live_default", "v3_live": "live"}
PHASE_LABEL = {"shadow": "shadow: the AI is only logged", "live_default": "live_default: default trades, AI only logged",
               "live": "live: the AI tilt trades"}
_SAFE = re.compile(r"[^A-Za-z0-9_ .:+/&-]")


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def sp(x: float, places: int = 1) -> str:
    """Signed number with a real minus sign: +8.2 / −3.1 / 0.0 (rounded first, so -0.04 shows 0.0)."""
    r = round(x, places)
    if r == 0:
        return f"{0:.{places}f}"
    return ("+" if r > 0 else "−") + f"{abs(r):.{places}f}"


def _read(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe(builder, *a):
    try:
        return builder(*a)
    except Exception:  # noqa: BLE001 - a block problem hides the block, never the email
        log.warning("report block %s hidden", getattr(builder, "__name__", "?"), exc_info=True)
        return None


# ------------------------------------------------------------------ India price gap
def pricegap_view(date: str, root, params: dict | None = None) -> dict | None:
    from . import pricegap
    if not pricegap.settings(params)["enabled"]:
        return None
    obj = pricegap.load(Path(root) / "state")
    if not obj or obj["date"] != date:
        return None
    gaps = {s: {k: g.get(k) for k in ("premium_pct", "median20_pct", "z", "warn")} for s, g in obj["gaps"].items()
            if g.get("date") == date and not g.get("stale")}          # a symbol whose reading is not from the report's day is not shown
    if not gaps:
        return None
    return {"date": obj["date"], "warn_pct": obj["warn_pct"], "duty_pct": obj.get("duty_pct"), "gaps": gaps}


def pricegap_chips(v: dict) -> list[tuple[str, str]]:
    out = []
    for sym, name in (("GOLDBEES", "Gold"), ("SILVERBEES", "Silver")):
        g = (v.get("gaps") or {}).get(sym)
        if not g or _num(g.get("premium_pct")) is None:
            continue
        t = f"{name} {sp(g['premium_pct'])}%"
        if _num(g.get("median20_pct")) is not None:
            t += f" · 20d median {sp(g['median20_pct'])}%"
        if _num(g.get("z")) is not None:
            t += f" · z {sp(g['z'])}"
        out.append((t, "amber" if g.get("warn") else "grey"))
    return out


def chips_text(chips: list[tuple[str, str]]) -> list[str]:
    """The text version of the chip row: an over-band chip (amber) says so in words."""
    return [t + (" (above band)" if tone == "amber" else "") for t, tone in chips]


def pricegap_note(v: dict) -> str:
    duty = _num(v.get("duty_pct"))
    base = f"over India parity (world price in rupees{f' plus {duty:g}% duty' if duty else ''})"
    return (f"{base}; alert band {v.get('warn_pct', 0):g}%; information only, not a trade signal; "
            "estimate (US vs NSE close timing can move it 2–4 pts), cause not verified")


def pricegap_lines(v: dict) -> list[str]:
    chips = pricegap_chips(v)
    return ["", "INDIA PRICE GAP · " + pricegap_note(v)] + chips_text(chips) if chips else []


# ------------------------------------------------------------------ Default vs AI vs executed
def _weights(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    out = {s: _num(d.get(s)) for s in SYMS}
    return None if all(v is None for v in out.values()) else {s: (None if v is None else round(v * 100)) for s, v in out.items()}


def _clamp_notes(clamps, limit: int = 3) -> list[str]:
    rows = []
    for i, c in enumerate(clamps if isinstance(clamps, list) else []):
        if not isinstance(c, dict) or not isinstance(c.get("rule"), str):
            continue
        rule = _SAFE.sub("", c["rule"])[:40].strip()
        if not rule:
            continue
        sym = _SAFE.sub("", str(c.get("sym") or ""))[:12].strip()
        a, b = _num(c.get("from")), _num(c.get("to"))
        txt = " ".join(x for x in (rule, NAMES.get(sym, sym)) if x)
        if a is not None and b is not None:
            txt += f" {a * 100:.0f}→{b * 100:.0f}%"
        rows.append((-(abs(b - a) if a is not None and b is not None else -1.0), i, txt))
    return [t for _, _, t in sorted(rows)[:limit]]


def _alloc_record(date: str, root) -> dict | None:
    rec = None
    try:
        for ln in (Path(root) / "logs" / f"decisions-{date}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("action") == "ALLOC" and isinstance(r.get("alloc"), dict):
                rec = r
    except OSError:
        return None
    return rec


def alloc_view(date: str, root, params: dict | None = None) -> dict | None:
    root = Path(root)
    rec = _alloc_record(date, root)
    if rec is not None:
        a = rec["alloc"]
        phase = _PHASE_BY_MODE.get(str(rec.get("alloc_mode")))
        default, ai, final = _weights(a.get("default")), _weights(a.get("proposal")), _weights(a.get("final"))
        executed = _weights(rec.get("v2_targets")) if phase == "shadow" else final
        if default is None and ai is None and executed is None:
            return None
        return {"source": "alloc", "phase": phase or "shadow", "conf": _num(a.get("confidence")), "advice": _SAFE.sub("", str(a.get("advice") or ""))[:40] or None,
                "weights": {"default": default, "ai": ai, "executed": executed}, "clamps": _clamp_notes(a.get("clamps"))}
    f = _read(root / "state" / "allocation.json")
    if not isinstance(f, dict) or str(f.get("for_date"))[:10] != date:
        return None
    ai = _weights(f.get("weights")) if isinstance(f.get("weights"), dict) else None
    default = _weights(f.get("default_at_write"))
    if default is None and ai is None:
        return None
    ph = str(((params or {}).get("allocation") or {}).get("phase") or "shadow")
    return {"source": "file", "phase": ph if ph in PHASE_LABEL else "shadow", "conf": _num(f.get("confidence")), "advice": None,
            "weights": {"default": default, "ai": ai, "executed": None}, "clamps": []}


def alloc_title(v: dict) -> str:
    return f"DEFAULT VS AI VS ENGINE TARGET · phase {v['phase']}"


def alloc_rows(v: dict) -> list[tuple[str, str, str, str]]:
    w = v["weights"]
    cell = lambda col, s: "–" if not w.get(col) or w[col].get(s) is None else f"{w[col][s]}%"      # noqa: E731
    return [(NAMES[s], cell("default", s), cell("ai", s), cell("executed", s)) for s in SYMS]


def alloc_footer(v: dict) -> list[str]:
    out = []
    has_ai = bool(v["weights"].get("ai"))
    ai = (f"AI proposal: confidence {v['conf']:.2f}" if v.get("conf") is not None else "AI proposal: confidence not given") if has_ai else "AI proposal: none (default kept)"
    out.append(ai)
    if v.get("clamps"):
        out.append("Clamp changed: " + "; ".join(v["clamps"]))
    elif v["source"] == "alloc":
        out.append("Clamp changed: nothing")
    if v["source"] == "file":
        out.append("Engine check not logged yet")
    return out


def alloc_lines(v: dict) -> list[str]:
    L = ["", alloc_title(v), "          Default   AI   Engine target"]
    L += [f"{n:<8} {d:>8} {a:>6} {e:>13}" for n, d, a, e in alloc_rows(v)]
    return L + alloc_footer(v)


# ------------------------------------------------------------------ paper twins
def _pick(d: dict, keys) -> float | None:
    for k in keys:
        v = _num(d.get(k))
        if v is not None:
            return v
    return None


LABELS = {"T0": "Real book", "T1": "Always-invested default", "T2": "Gold+NIFTY sink", "T3": "No silver rule",
          "T4": "Gold–silver ratio tilt", "T5": "Buy&hold gold", "T6": "Buy&hold gold+NIFTY"}


def _summary_items(raw: dict) -> list[tuple[str, dict]]:
    for key in ("twins", "rows"):
        src = raw.get(key)
        if isinstance(src, dict):
            return [(str(k), v) for k, v in src.items() if isinstance(v, dict)]
        if isinstance(src, list):
            return [(str(d.get("name") or d.get("id") or d.get("label") or ""), d) for d in src if isinstance(d, dict)]
    return []


def _book_items(raw: dict) -> list[tuple[str, dict]]:
    books = raw.get("books")
    out = []
    for tid, b in (books.items() if isinstance(books, dict) else []):
        if not isinstance(b, dict):
            continue
        v, v0 = _num(b.get("value")), _num(b.get("start_value"))
        row = dict(b)
        if v is not None and v0 and v0 > 0:
            row["return_pct"] = (v / v0 - 1.0) * 100
        out.append((str(b.get("id") or tid), row))
    return out


def _twin_rows(items: list[tuple[str, dict]]) -> list[dict]:
    """Rows sorted by id (T0..T6) when ids look like T<n>; names from the label map, else the (cleaned) label or id."""
    rows = []
    for tid, d in sorted(items, key=lambda kv: (0, int(kv[0][1:])) if re.fullmatch(r"T\d+", kv[0]) else (1, 0)):
        label = d.get("label") if isinstance(d.get("label"), str) else None
        name = LABELS.get(tid) or _SAFE.sub("", label or tid)[:28].strip()
        value = _pick(d, ("value", "equity", "total_value", "nav"))
        ret = _pick(d, ("return_pct", "ret_pct", "pnl_pct"))
        if ret is None:
            r = _pick(d, ("return", "ret"))
            ret = None if r is None else r * 100
        dd = _pick(d, ("max_dd_pct", "max_drawdown_pct", "mdd_pct"))
        if dd is None:
            r = _pick(d, ("max_dd", "max_drawdown", "mdd"))
            dd = None if r is None else r * 100
        if not name or (value is None and ret is None):
            continue
        rows.append({"name": name, "value": value, "ret_pct": ret, "dd_pct": None if dd is None else -abs(dd)})
    return rows


def twins_view(root) -> dict | None:
    raw = _read(Path(root) / "state" / "twins.json")
    if not isinstance(raw, dict):
        return None
    rows = _twin_rows(_summary_items(raw)) or _twin_rows(_book_items(raw))      # the summary keys first, else the raw books
    if not rows:
        return None
    since = str(raw.get("since") or raw.get("start_day") or raw.get("start_date") or "")[:10]
    asof = str(raw.get("asof") or raw.get("last_day") or raw.get("updated") or "")[:10]
    ok = lambda s: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))      # noqa: E731
    return {"since": since if ok(since) else None, "asof": asof if ok(asof) else None, "rows": rows[:MAX_TWINS]}


def twins_title(v: dict) -> str:
    return "PAPER TWINS" + (f" · since {v['since']}" if v.get("since") else " · since start") + (f" · as of {v['asof']}" if v.get("asof") else "")


def twins_cells(r: dict) -> tuple[str, str, str, str]:
    from .report import inr          # the report's own Indian digit grouping (lazy: report imports this module)
    val = "–" if r["value"] is None else inr(r["value"])
    ret = "–" if r["ret_pct"] is None else sp(r["ret_pct"], 1) + "%"
    dd = "–" if r["dd_pct"] is None else sp(r["dd_pct"], 1) + "%"
    return r["name"], val, ret, dd


def twins_lines(v: dict) -> list[str]:
    return ["", twins_title(v) + " (value, return, max drawdown)"] + ["  ".join(twins_cells(r)) for r in v["rows"]]


# ------------------------------------------------------------------ attach to the day dict
def attach(day: dict, date: str, root, params: dict | None = None) -> None:
    """Add pricegap_view / alloc_view / twins_view to `day`, each only when it has data. Never raises."""
    if params is None:
        try:
            from . import config
            params = config.load_params()
        except Exception:  # noqa: BLE001
            params = {}
    for key, fn in (("pricegap_view", lambda: pricegap_view(date, root, params)), ("alloc_view", lambda: alloc_view(date, root, params)),
                    ("twins_view", lambda: twins_view(root))):
        v = _safe(fn)
        if v:
            day[key] = v
