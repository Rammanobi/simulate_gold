#!/usr/bin/env python3
"""Calibration ledger for strategy v3 (DORMANT; paper trading only): records every scenario set and desk call, scores them
later (Brier) and tracks the paired AI-vs-default result. Code only: no web, no AI. The ledger lives in
memory/ltm/calibration.json and is written ONLY by this module through the memory safe writer (memstore.write_atomic).

  python tools/calibration.py show                  # the summary the routines read (desk_brier, ai_vs_default)
  python tools/calibration.py score [--write]       # score everything that is due (needs closes: network only if something is due)
  python tools/calibration.py week --week 2026-W47 [--format json|md]   # weekly stats (also: tools/evaluate.py --week)

Nothing is recorded by hand. WHO WRITES THE LEDGER (single writer per kind, so parallel sessions never edit one file):
  * the FINAL / PROVISIONAL synthesis rows: `tools/guidance.py allocation set|hold-default`, run only by premarket.md, allocation.md
    and fastlane.md. Those three sessions `git add memory/ltm/calibration.json` after they write it.
  * desk calls: NOT recorded by the desk sessions. `tools/desk.py write` only writes analysis/D/<desk>.json. When a score run finds a
    desk file whose 5-day window has ended it READS that committed file (validated again with desk.read_status) and scores it; a
    per-date list `dk` remembers which files were already taken. desk_metal.md, desk_risk.md and desk_macro.md never touch the ledger.
  * scoring: `tools/memory_ltm.py perf --write` (daily_report, weekly) scores what is due and copies the summary into performance.json
    (`ai_vs_default`, `desk_brier`); those sessions add the ledger too.
The ledger holds numbers and symbols only (no free text), so nothing in it can steer a later run. A ledger that cannot be read is NEVER
silently reset: it is moved to memory/ltm/calibration.corrupt.<timestamp>.json and a new one is started with a printed warning; a row of
the wrong shape is set aside in memory/ltm/calibration.badrows.<timestamp>.json and the rest of the ledger keeps working.

Definitions (every number a routine sees comes from here):
  horizon     a scenario set or desk call is for the h trading days starting on its for_date (h = horizon_days, default 5).
              base = the last close BEFORE for_date; end = the close of the h-th trading day (for_date is day 1).
  outcome     up if the return is above the flat band, down if below minus the band, else flat. band = max(0.5 percent,
              0.5 x the 20-day daily standard deviation x sqrt(h)); 1 percent when there are fewer than 10 returns.
  Brier       two binary events (up, down) averaged: ((p_up - 1[up])^2 + (p_down - 1[down])^2) / 2. Always saying 50/50 scores 0.25.
              A scenario set's p_up of a metal is the sum of p over its scenarios that say `up` (a metal a scenario leaves out is flat).
  AI vs default  per for_date, the FINAL set if there is one, else the PROVISIONAL. The AI weights are the default at write time
              tilted to the proposal at FULL trust (strength clip((confidence - 0.30) / 0.50, 0, 1), at most max_tilt per symbol,
              the rest to GOLDBEES), the one-day return of those weights minus the one-day return of the default, in percentage
              points of the book. It is what the tilt WOULD have earned had the engine trusted it fully; it is not the engine's P&L.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import ROOT, iso_day, parse_now, utf8_stdout

from simgold import allocation, brief, market, memstore

FILE = "calibration.json"
MAX_BYTES = 40_000
KEEP_SETS = 46
KEEP_PAIRS = 130
KEEP_SCORES = 40                     # per desk; the synthesis keeps KEEP_SYNTH dates so tools/trust.py can reach its 120-day level
KEEP_SYNTH = 140
KEEP_OPEN_DAYS = 45
HARVEST_DAYS = 60                    # desk files older than this many calendar days are never taken up (they would have expired anyway)
KEEP_DK_DAYS = 75                    # `dk` (desk files already taken up) is kept this long: longer than HARVEST_DAYS, so no file is taken twice
MIN_SCORED = 20                      # "n/a" for the confidence cap until this many scored calls
DESKS = ("macro", "gold", "silver", "copper", "risk")
METALS = ("GOLD", "SILVER", "COPPER")
SYMS = allocation.SYMS
SYM_OF = {m: s for s, m in allocation.METAL.items()}
_DC = {"up": "u", "flat": "f", "down": "d"}


# ------------------------------------------------------------------ file
def path(root) -> Path:
    return memstore.ltm_path(root, FILE)


def empty() -> dict:
    return {"v": 1, "updated": None, "sets": [], "calls": [], "pairs": [], "scores": {}, "dk": {}}


# ---- row shapes (a row of any other shape is set aside, never allowed to block a recording or crash a summary)
def _isnum(x) -> bool:
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _isday(x) -> bool:
    try:
        iso_day(x)
        return True
    except ValueError:
        return False


def _is_trip(x, allow_none: bool = True) -> bool:
    return (x is None and allow_none) or (isinstance(x, list) and len(x) == 3 and all(_isnum(v) for v in x))


def _good_set(r) -> bool:
    if not isinstance(r, dict) or not _isday(r.get("d")) or r.get("ph") not in ("f", "p") or r.get("st") not in ("o", "s"):
        return False
    h = r.get("h")
    if not isinstance(h, int) or isinstance(h, bool) or not 1 <= h <= 60 or not _isnum(r.get("c")):
        return False
    if not (_is_trip(r.get("w")) and _is_trip(r.get("dw")) and _is_trip(r.get("px"))):
        return False
    sc = r.get("sc")
    if not isinstance(sc, list) or not all(isinstance(x, list) and len(x) == 4 and _isnum(x[0]) and all(v in ("u", "f", "d") for v in x[1:])
                                           for x in sc):
        return False
    return all(_isnum(r[k]) for k in ("b",) if k in r) and all(isinstance(r[k], int) and not isinstance(r[k], bool) for k in ("sk",) if k in r)


def _good_call(r) -> bool:
    return (isinstance(r, dict) and _isday(r.get("d")) and r.get("k") in DESKS and r.get("m") in METALS and r.get("o") in ("u", "f", "d")
            and _isnum(r.get("u")) and _isnum(r.get("n")))


def _good_pair(r) -> bool:
    return (isinstance(r, dict) and _isday(r.get("d")) and r.get("f") in ("f", "p") and all(_isnum(r.get(k)) for k in ("pp", "ai", "df"))
            and (r.get("rs") is None or _isnum(r.get("rs"))))


def _good_score(k, r) -> bool:
    if k == "synthesis":
        return isinstance(r, list) and len(r) == 2 and _isday(r[0]) and _isnum(r[1])
    return isinstance(r, list) and len(r) == 3 and _isday(r[0]) and r[1] in METALS and _isnum(r[2])


def _oc(led: dict, d: str, m: str, outcome: str) -> None:
    """Remember the 5-day outcome (u/f/d) of a metal for a for_date, so tools/trust.py can build the base-rate forecaster later.
    Stored as a 3-character string per date (GOLD, SILVER, COPPER; '-' = unknown). Additive: the key exists only once something was scored."""
    cur = list((led.setdefault("oc", {}).get(d) or "---")[:3].ljust(3, "-"))
    cur[METALS.index(m)] = _DC[outcome]
    led["oc"][d] = "".join(cur)


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now()).strftime("%Y%m%dT%H%M%S")


def _move(src: Path, dest: Path) -> None:
    os.replace(src, dest)


def _aside(root, kind: str, stamp: str) -> Path:
    base = memstore.ltm_path(root, f"calibration.{kind}.{stamp}.json")
    dest, n = base, 1
    while dest.exists():
        n += 1
        dest = base.with_name(f"calibration.{kind}.{stamp}-{n}.json")
    return dest


def load(root, repair: bool = False, now: datetime | None = None) -> dict:
    """The ledger, always well-formed. Rows of the wrong shape are dropped from the returned object (never raised on); the dropped
    count is in `_warn` (a list of plain sentences, not saved). repair=True (the write paths) also acts on a damaged file: an unreadable
    ledger is moved to memory/ltm/calibration.corrupt.<ts>.json (never silently reset; an OSError while moving it is raised, so nothing
    is overwritten) and the bad rows are copied to memory/ltm/calibration.badrows.<ts>.json. repair=False never writes anything."""
    f = path(root)
    led = empty()
    led["_warn"] = []
    if not f.exists():
        return led
    d, why = None, None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        why = type(e).__name__
    if why is None and (not isinstance(d, dict) or not isinstance(d.get("sets"), list)):
        why = "not a ledger object"
    if why is not None:
        if repair:
            dest = _aside(root, "corrupt", _stamp(now))
            _move(f, dest)
            led["_warn"].append(f"WARNING: the calibration ledger was unreadable ({why}); moved to {dest.name} and a new ledger started")
        else:
            led["_warn"].append(f"WARNING: the calibration ledger is unreadable ({why}); treated as empty (the next write moves it aside)")
        return led
    bad: list = []
    for k, ok in (("sets", _good_set), ("calls", _good_call), ("pairs", _good_pair)):
        rows = d.get(k, [])
        if not isinstance(rows, list):
            bad.append({k: "wrong type"})
            continue
        led[k] = [r for r in rows if ok(r)]
        bad += [{k: r} for r in rows if not ok(r)]
    oc = d.get("oc")
    if isinstance(oc, dict):
        led["oc"] = {k: v for k, v in oc.items() if _isday(k) and isinstance(v, str) and len(v) == 3 and set(v) <= set("ufd-")}
    sc = d.get("scores", {})
    if isinstance(sc, dict):
        for k, rows in sc.items():
            if k not in ("synthesis",) + DESKS or not isinstance(rows, list):
                bad.append({"scores": {str(k)[:20]: "unknown or wrong type"}})
                continue
            led["scores"][k] = [r for r in rows if _good_score(k, r)]
            bad += [{"scores." + k: r} for r in rows if not _good_score(k, r)]
    else:
        bad.append({"scores": "wrong type"})
    dk = d.get("dk", {})
    if isinstance(dk, dict):
        led["dk"] = {k: [x for x in v if x in DESKS] for k, v in dk.items() if _isday(k) and isinstance(v, list)}
    led["updated"] = d.get("updated") if isinstance(d.get("updated"), str) else None
    if bad:
        msg = f"WARNING: {len(bad)} calibration row(s) of the wrong shape were set aside"
        if repair:
            dest = _aside(root, "badrows", _stamp(now))
            memstore.write_atomic(dest, json.dumps({"rows": bad[:40]}, ensure_ascii=False, separators=(",", ":"))[:20000] + "\n")
            msg += f" (copied to {dest.name})"
        led["_warn"].append(msg + "; the rest of the ledger is used")
    return led


def _text(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def save(root, obj: dict, now: datetime) -> None:
    """Trim to the caps (oldest scored first) and write atomically. The only writer of memory/ltm/calibration.json."""
    obj.pop("_warn", None)
    obj["updated"] = now.isoformat(timespec="seconds")
    cut = (now.date() - timedelta(days=KEEP_DK_DAYS)).isoformat()
    obj["dk"] = {k: v for k, v in sorted((obj.get("dk") or {}).items()) if k >= cut and v}
    obj["sets"] = sorted(obj["sets"], key=lambda s: (s["d"], s["ph"]))
    scored = [s for s in obj["sets"] if s.get("st") == "s"]
    while len(scored) > KEEP_SETS:
        obj["sets"].remove(scored.pop(0))
    obj["pairs"] = obj["pairs"][-KEEP_PAIRS:]
    if obj.get("oc"):
        obj["oc"] = dict(sorted(obj["oc"].items())[-KEEP_PAIRS:])
    for k, rows in list(obj["scores"].items()):
        obj["scores"][k] = rows[-(KEEP_SYNTH if k == "synthesis" else KEEP_SCORES):]
    while len(_text(obj).encode()) > MAX_BYTES:
        if obj["calls"] and len(obj["calls"]) > 30:
            obj["calls"] = obj["calls"][len(obj["calls"]) // 4:]
        elif obj["pairs"] and len(obj["pairs"]) > 20:
            obj["pairs"] = obj["pairs"][10:]
        elif obj["sets"]:
            obj["sets"].pop(0)
        else:
            break
    memstore.write_atomic(path(root), _text(obj))


# ------------------------------------------------------------------ recording
def _trip(m: dict | None):
    return None if not isinstance(m, dict) else [round(float(m[s]), 4) for s in SYMS]


def record_proposal(root, now: datetime, clean: dict) -> str:
    """Record the object `allocation set|hold-default` just wrote (already filtered by allocation.sanitize_allocation). The ONLY writer of
    synthesis rows (premarket, allocation and fastlane sessions run it; each then stages memory/ltm/calibration.json).
    One row per (for_date, phase): a second write replaces the first (idempotent). The caller catches any exception."""
    hold = clean.get("weights") == "default"
    sc = []
    if not hold:
        for s in clean.get("scenarios") or []:
            sc.append([round(float(s["p"]), 4)] + [_DC.get(s.get(m), "f") for m in METALS])
    row = {"d": clean["for_date"], "ph": "f" if clean["phase"] == "final" else "p", "t": clean["written"][:19],
           "c": round(float(clean.get("confidence") or 0.0), 4), "w": None if hold else _trip(clean["weights"]),
           "dw": _trip(clean.get("default_at_write")), "px": _trip(clean.get("prices_at_update")),
           "h": int(clean["horizon_days"]) if isinstance(clean.get("horizon_days"), (int, float)) and not isinstance(
               clean.get("horizon_days"), bool) else 5, "sc": sc, "st": "o"}
    try:                                                 # fact-check result of the stated numbers: [ok 0/1, n_mismatch] (numbers only)
        from simgold import factcheck
        fc = factcheck.check_proposal(root, now.date().isoformat(), clean)["factcheck"]
        row["fc"] = [1 if fc["ok"] else 0, int(fc["n_mismatch"])]
    except Exception:                                    # noqa: BLE001
        pass
    led = load(root, repair=True, now=now)
    warn = led.get("_warn") or []
    led["sets"] = [s for s in led["sets"] if not (s["d"] == row["d"] and s["ph"] == row["ph"])] + [row]
    save(root, led, now)
    return "\n".join([f"ledger: {row['d']} {'final' if row['ph'] == 'f' else 'provisional'} recorded ({len(sc)} scenarios)"] + warn)


def _due(d: str, h: int, today: date) -> bool:
    """The window of h trading days starting on d has ended before today."""
    return market.add_trading_days(date.fromisoformat(d), max(h, 1) - 1) < today


def harvest_desks(root, led: dict, today: date) -> int:
    """Take up the desk calls of the committed analysis/D/<desk>.json files whose 5-day window has ended, into led["calls"] (in memory;
    the caller saves). A file is validated again by tools/desk.py read_status (the writer's own rules), an abstaining or invalid desk
    gives no call, and `dk` (date -> desks already taken) makes it idempotent: a file is never taken twice, a rewritten one is not
    taken again. This replaces desk sessions writing the ledger (parallel desks would all edit one JSON file)."""
    base = Path(root) / "analysis"
    if not base.is_dir():
        return 0
    import desk as _desk                                   # lazy: desk imports this module
    lo, hi, n = (today - timedelta(days=HARVEST_DAYS)).isoformat(), today.isoformat(), 0
    for dd in sorted(base.iterdir()):
        day = dd.name
        if not dd.is_dir() or not _isday(day) or day < lo or day > hi or not _due(day, 5, today):
            continue
        done = list(led["dk"].get(day, []))
        for k in DESKS:
            if k in done:
                continue
            st, clean, _why = _desk.read_status(root, day, k)
            if st == "missing":
                continue
            done.append(k)
            if st != "ok" or not clean:
                continue
            led["calls"] = [c for c in led["calls"] if not (c["d"] == day and c["k"] == k)]
            try:
                from simgold import factcheck
                fcr = factcheck.check_desk(root, day, k, clean)["factcheck"]
                fc = [1 if fcr["ok"] else 0, int(fcr["n_mismatch"])]
            except Exception:                           # noqa: BLE001
                fc = None
            for m, v in (clean.get("view") or {}).items():
                if m in METALS and v.get("dir_5d") in _DC:
                    row = {"d": day, "k": k, "m": m, "o": _DC[v["dir_5d"]],
                           "u": round(float(v["p_up"]), 4), "n": round(float(v["p_down"]), 4)}
                    if fc is not None:
                        row["fc"] = fc
                    led["calls"].append(row)
                    n += 1
        if done:
            led["dk"][day] = done
    return n


# ------------------------------------------------------------------ prices
def fetch_closes(tickers: dict[str, str], days: int = 120) -> dict[str, list[tuple[str, float]]]:
    """Network: daily closes of the three ETFs ({sym: [(iso date, close)]}). Replaced in tests."""
    from simgold import data
    h = data.daily_history(list(tickers.values()), period="6mo")["close"]
    out = {}
    for sym, tk in tickers.items():
        if tk in h:
            s = h[tk].dropna()
            out[sym] = [(i.date().isoformat(), float(v)) for i, v in s.items() if math.isfinite(float(v)) and float(v) > 0]
    return out


def closes_from_regime(root) -> dict[str, list[tuple[str, float]]]:
    """Fallback without network: the recent closes kept in memory/ltm/regime.json (code-written)."""
    reg = memstore.regime_data(root)
    out: dict = {}
    try:
        asof = date.fromisoformat(str(reg.get("asof"))[:10])
        dates = reg.get("recent_dates") or []
        for m, r in (reg.get("metals") or {}).items():
            cl = r.get("recent_closes") or []
            if m not in SYM_OF or not cl or len(dates) < len(cl):
                continue
            ds = dates[-len(cl):]
            rows = []
            for md, c in zip(ds, cl):
                mo, dd = int(md[:2]), int(md[3:5])
                y = asof.year if (mo, dd) <= (asof.month, asof.day) else asof.year - 1
                rows.append((date(y, mo, dd).isoformat(), float(c)))
            out[SYM_OF[m]] = rows
    except (TypeError, ValueError, AttributeError):
        return {}
    return out


# ------------------------------------------------------------------ scoring maths
def brier(p_up: float, p_down: float, outcome: str) -> float:
    return ((p_up - (outcome == "up")) ** 2 + (p_down - (outcome == "down")) ** 2) / 2.0


def _idx(series: list[tuple[str, float]], d: str) -> int | None:
    """Index of the first close on or after d."""
    for i, (k, _v) in enumerate(series):
        if k >= d:
            return i
    return None


def window_return(series: list[tuple[str, float]], d: str, h: int):
    """(return, band) for the h trading days starting on d, or None while the window is incomplete or the base is missing."""
    i0 = _idx(series, d)
    if i0 is None or i0 < 1 or i0 + h - 1 >= len(series):
        return None
    base, end = series[i0 - 1][1], series[i0 + h - 1][1]
    if base <= 0 or end <= 0:
        return None
    prior = [series[i][1] / series[i - 1][1] - 1 for i in range(max(1, i0 - 20), i0)]
    band = max(0.005, 0.5 * statistics.pstdev(prior) * math.sqrt(h)) if len(prior) >= 10 else 0.01
    return end / base - 1.0, band


def day_return(series: list[tuple[str, float]], d: str):
    r = window_return(series, d, 1)
    return None if r is None else r[0]


def outcome_of(ret: float, band: float) -> str:
    return "up" if ret > band else "down" if ret < -band else "flat"


def set_probs(row: dict) -> dict[str, tuple[float, float]]:
    out = {}
    for j, m in enumerate(METALS):
        out[m] = (sum(s[0] for s in row["sc"] if s[1 + j] == "u"), sum(s[0] for s in row["sc"] if s[1 + j] == "d"))
    return out


def ai_weights(row: dict, cfg: dict) -> list[float] | None:
    """The AI mix at full trust: default at write + k_full x clip(proposal - default, +-max_tilt), the residual to GOLDBEES."""
    if row.get("w") is None or row.get("dw") is None:
        return None
    k = allocation.tilt_strength(1.0, row["c"])
    mt = cfg["max_tilt"]
    w = [max(0.0, d + k * max(-mt, min(mt, a - d))) for a, d in zip(row["w"], row["dw"])]
    gap = sum(row["dw"]) - sum(w)
    w[0] = max(0.0, w[0] + gap)
    return w


def _closes_for(root, need: bool, closes, cfg_tickers) -> dict:
    if closes is not None:
        return closes
    if not need:
        return {}
    try:
        got = fetch_closes(cfg_tickers)
        if got and all(got.get(s) for s in SYMS):
            return got
    except Exception:                                    # noqa: BLE001 - no network or a data problem: use what the repo has
        pass
    return closes_from_regime(root)


def score(root, now: datetime, closes=None, write: bool = True, p: dict | None = None) -> dict:
    """Score every set, call and pair that is due (its window ended before today). Network only when something is due and no
    `closes` were given. Returns {"scored_sets", "scored_calls", "pairs", "open"}. Never raises on missing data."""
    from simgold import config
    p = p or config.load_params()
    cfg = allocation.settings(p)
    led = load(root, repair=write, now=now)
    today = now.date()
    harvest_desks(root, led, today)
    due = [s for s in led["sets"] if s.get("st") == "o"]
    pending = lambda d, h: _due(d, h, today)                       # noqa: E731
    due_sets = [s for s in due if pending(s["d"], s["h"])]
    due_calls = [c for c in led["calls"] if pending(c["d"], 5)]
    res = {"scored_sets": 0, "scored_calls": 0, "pairs": 0, "open": 0}
    if led["_warn"]:
        res["warnings"] = list(led["_warn"])
    if not due_sets and not due_calls:
        res["open"] = len(due) + len(led["calls"])
        return res
    tick = {s: p["engine"]["tickers"][s] for s in SYMS}
    cl = _closes_for(root, True, closes, tick)
    if not cl:
        res["open"] = len(due) + len(led["calls"])
        return res
    # ---- one-day pairs and scenario Brier, FINAL preferred per for_date
    by_day: dict[str, list[dict]] = {}
    for s in led["sets"]:
        by_day.setdefault(s["d"], []).append(s)
    for d, rows in sorted(by_day.items()):
        pref = next((r for r in rows if r["ph"] == "f"), rows[0])
        for r in rows:
            if r.get("st") != "o" or not pending(r["d"], r["h"]):
                continue
            if r is not pref:
                r["st"] = "s"                          # superseded by the FINAL of the same day: not scored twice
                r["sk"] = 1
                continue
            probs = set_probs(r) if r["sc"] else None
            bs, ok = [], True
            for j, m in enumerate(METALS):
                wr = window_return(cl.get(SYM_OF[m], []), r["d"], r["h"])
                if wr is None:
                    ok = False
                    break
                if r["h"] == 5:
                    _oc(led, r["d"], m, outcome_of(*wr))
                if probs:
                    bs.append(brier(probs[m][0], probs[m][1], outcome_of(*wr)))
            if not ok:
                if market.add_trading_days(date.fromisoformat(r["d"]), r["h"] + 30) < today:
                    r["st"] = "s"                      # never got prices: expired, not scored
                    r["sk"] = 2
                continue
            r["st"] = "s"
            if bs:
                r["b"] = round(sum(bs) / len(bs), 4)
                led["scores"].setdefault("synthesis", []).append([r["d"], r["b"]])
                res["scored_sets"] += 1
        if pref.get("st") == "s" and not pref.get("sk") and not any(x["d"] == d for x in led["pairs"]):
            pr = _pair(pref, cl, cfg, root)
            if pr:
                led["pairs"].append(pr)
                res["pairs"] += 1
    # ---- desk calls
    keep = []
    for c in led["calls"]:
        if not pending(c["d"], 5):
            keep.append(c)
            continue
        wr = window_return(cl.get(SYM_OF[c["m"]], []), c["d"], 5)
        if wr is None:
            if market.add_trading_days(date.fromisoformat(c["d"]), 35) >= today:
                keep.append(c)
            continue
        _oc(led, c["d"], c["m"], outcome_of(*wr))
        led["scores"].setdefault(c["k"], []).append([c["d"], c["m"], round(brier(c["u"], c["n"], outcome_of(*wr)), 4)])
        res["scored_calls"] += 1
    led["calls"] = keep
    res["open"] = sum(1 for s in led["sets"] if s.get("st") == "o") + len(led["calls"])
    if write and (res["scored_sets"] or res["scored_calls"] or res["pairs"] or any(s.get("sk") for s in led["sets"]) or led["_warn"]):
        save(root, led, now)
    return res


def _pair(row: dict, cl: dict, cfg: dict, root) -> dict | None:
    aw = ai_weights(row, cfg)
    if aw is None:
        return {"d": row["d"], "f": row["ph"], "pp": 0.0, "ai": 0.0, "df": 0.0, "rs": 0.0} if row.get("dw") else None
    rets = [day_return(cl.get(s, []), row["d"]) for s in SYMS]
    if any(r is None for r in rets):
        return None
    ai = sum(w * r for w, r in zip(aw, rets)) * 100
    df = sum(w * r for w, r in zip(row["dw"], rets)) * 100
    rs = None
    try:
        from simgold import evaluate
        v = evaluate.at_or_before(evaluate.load_equity(root), date.fromisoformat(row["d"]) - timedelta(days=1))
        rs = round((ai - df) / 100 * v[1], 2) if v else None
    except Exception:                                    # noqa: BLE001
        rs = None
    return {"d": row["d"], "f": row["ph"], "pp": round(ai - df, 4), "ai": round(ai, 4), "df": round(df, 4), "rs": rs}


# ------------------------------------------------------------------ summary
def _mean_last(rows: list, idx: int, n: int = MIN_SCORED):
    vals = [r[idx] for r in rows]
    return round(sum(vals[-n:]) / min(n, len(vals)), 4) if len(vals) >= n else None


def summary(root, now: datetime | None = None) -> dict | None:
    """{"desk_brier": {...}, "ai_vs_default": {...}} from the ledger, or None when there is no ledger yet."""
    if not path(root).exists():
        return None
    led = load(root)
    sc = led["scores"]
    db: dict = {}
    allrows = []
    for k in ("synthesis",) + DESKS:
        rows = sc.get(k) or []
        if k == "synthesis":
            db[k] = {"n": len(rows), "last20": _mean_last(rows, 1)}
        else:
            db[k] = {"n": len(rows), "last20": _mean_last(rows, 2)}
            allrows += rows
    allrows.sort(key=lambda r: r[0])
    db["desks_all"] = {"n": len(allrows), "last20": _mean_last(allrows, 2)}
    pairs = led["pairs"]
    now = now or market.now_ist()
    cut3 = (now.date() - timedelta(days=92)).isoformat()
    by_month: dict[str, float] = {}
    for x in pairs:
        by_month[x["d"][:7]] = round(by_month.get(x["d"][:7], 0.0) + x["pp"], 4)
    avd = {"n": len(pairs), "d20_pp": round(sum(x["pp"] for x in pairs[-20:]), 4) if len(pairs) >= MIN_SCORED else None,
           "m3_pp": round(sum(x["pp"] for x in pairs if x["d"] >= cut3), 4) if pairs else None,
           "by_month_pp": dict(sorted(by_month.items())[-6:]),
           "month_rs": {m: round(sum(x["rs"] or 0.0 for x in pairs if x["d"][:7] == m), 2) for m in sorted(by_month)[-3:]}}
    return {"desk_brier": db, "ai_vs_default": avd}


def brier_text(s: dict | None) -> list[str]:
    """Lines for the context views: the numbers the confidence cap (A7) needs, 'n/a' while there are fewer than 20 scored calls."""
    if not s:
        return ["desk_brier: n/a (no calibration ledger yet)", "ai_vs_default: n/a"]
    db, av = s["desk_brier"], s["ai_vs_default"]
    f = lambda x: "n/a" if x is None else f"{x:.3f}"            # noqa: E731
    parts = [f"{k} {f(v['last20'])} (n={v['n']})" for k, v in db.items()]
    ln2 = ("ai_vs_default (full-trust tilt vs default, pp of book): " +
           f"n={av['n']} last20d {('n/a' if av['d20_pp'] is None else format(av['d20_pp'], '+.2f'))} "
           f"3m {('n/a' if av['m3_pp'] is None else format(av['m3_pp'], '+.2f'))}")
    if av["by_month_pp"]:
        ln2 += " | " + ", ".join(f"{m} {v:+.2f}" for m, v in av["by_month_pp"].items())
    return ["desk_brier (last 20 scored; 0.25 = always 50/50): " + "; ".join(parts), ln2]


# ------------------------------------------------------------------ weekly stats
def _week_days(week: str) -> list[date]:
    try:
        y, w = week.upper().split("-W")
        mon = date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, TypeError):
        raise ValueError(f"--week must look like 2026-W47, got {week!r}")
    return [mon + timedelta(days=i) for i in range(5)]


def _alloc_records(root, day: str) -> list[dict]:
    out = []
    try:
        for ln in (Path(root) / "logs" / f"decisions-{day}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("action") == "ALLOC" and isinstance(r.get("alloc"), dict):
                out.append(r)
    except OSError:
        pass
    return out


def week_stats(root, week: str, now: datetime | None = None, closes=None) -> dict:
    """Per day: default vs final weights (ALLOC records), the account's day P&L, the default's one-day return, turnover used,
    clamp counts by rule; the week's AI-vs-default pairs and the desk Brier summary. Missing data is null, never a guess."""
    from simgold import config, evaluate
    days = _week_days(week)
    p = config.load_params()
    cfg = allocation.settings(p)
    eq = evaluate.load_equity(root)
    led = load(root)
    cl = closes if closes is not None else closes_from_regime(root)
    out_days, clamps, used = [], {}, 0.0
    for d in days:
        iso = d.isoformat()
        recs = _alloc_records(root, iso)
        a = recs[-1]["alloc"] if recs else None
        cur = evaluate.at_or_before(eq, d)
        prev = evaluate.at_or_before(eq, d - timedelta(days=1))
        row = {"date": iso, "alloc_mode": recs[-1].get("alloc_mode") if recs else None,
               "default": a.get("default") if a else None, "final": a.get("final") if a else None,
               "advice": a.get("advice") if a else None,
               "pnl_rs": round(cur[1] - prev[1], 2) if cur and prev and cur[0] == d else None,
               "turnover": (a.get("turnover") or {}).get("discretionary") if a else None, "default_ret_pct": None}
        if a and a.get("default"):
            rets = [day_return(cl.get(s, []), iso) for s in SYMS]
            if all(r is not None for r in rets):
                row["default_ret_pct"] = round(sum(a["default"][s] * r for s, r in zip(SYMS, rets)) * 100, 4)
        for c in (a or {}).get("clamps") or []:
            clamps[c.get("rule")] = clamps.get(c.get("rule"), 0) + 1
        used += float(row["turnover"] or 0.0)
        out_days.append(row)
    pf = brief.load_json(Path(root) / "state" / "portfolio.json", {})
    pairs = [x for x in led["pairs"] if days[0].isoformat() <= x["d"] <= days[-1].isoformat()]
    s = summary(root, now)
    return _with_trust(root, week, now, {"week": week.upper(), "days": out_days, "held_now": None if not isinstance(pf, dict) else {
        "positions": {k: v.get("qty") for k, v in (pf.get("positions") or {}).items() if isinstance(v, dict)}},
            "turnover_used": round(used, 4), "turnover_budget_week": cfg["max_turnover_week"],
            "clamp_counts": clamps, "ai_vs_default_pairs": pairs,
            "ai_vs_default_week_pp": round(sum(x["pp"] for x in pairs), 4) if pairs else None,
            "desk_brier": s["desk_brier"] if s else None, "ai_vs_default": s["ai_vs_default"] if s else None,
            "notes": ["there is no per-day `held` record: only the current positions (held_now) are shown",
                      "D5 (v2) and GOLDBEES buy-and-hold P&L are not in this report: take them from `python tools/evaluate.py --month` (scorecard)"]})


def _with_trust(root, week: str, now, w: dict) -> dict:
    """Additive: `trust_line` only when a ledger file exists (no ledger -> the output is exactly what it was before)."""
    if path(root).exists():
        try:
            from simgold import trust
            w["trust_line"] = trust.summary_line(trust.compute(root, now, week))
        except Exception:                                # noqa: BLE001 - a bonus line, never an error
            pass
    return w


def week_md(w: dict) -> str:
    L = [f"Week {w['week']} (code-computed; ALLOC records, equity.csv, calibration ledger)"]
    for d in w["days"]:
        f = lambda m: "n/a" if not m else "/".join(f"{m[s] * 100:.0f}" for s in SYMS)            # noqa: E731
        L.append(f"{d['date']} default {f(d['default'])} final {f(d['final'])} advice {d['advice']} "
                 f"pnl {d['pnl_rs']} turnover {d['turnover']} default-ret {d['default_ret_pct']}%")
    L.append(f"turnover used {w['turnover_used']} of {w['turnover_budget_week']}; clamps {w['clamp_counts']}")
    L.append(f"AI vs default this week: {w['ai_vs_default_week_pp']} pp over {len(w['ai_vs_default_pairs'])} day(s)")
    if w["desk_brier"]:
        L += brier_text({"desk_brier": w["desk_brier"], "ai_vs_default": w["ai_vs_default"]})
    if w.get("trust_line"):
        L.append(w["trust_line"])
    return "\n".join(L)


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    sc = sub.add_parser("score")
    sc.add_argument("--write", action="store_true")
    wk = sub.add_parser("week")
    wk.add_argument("--week", required=True)
    wk.add_argument("--format", choices=("json", "md"), default="json")
    a = ap.parse_args(argv)
    utf8_stdout()
    root, now = Path(a.root), parse_now(a.now)
    try:
        if a.cmd == "show":
            for w in load(root)["_warn"]:
                print(w, file=sys.stderr)
            print("\n".join(brier_text(summary(root, now))))
        elif a.cmd == "score":
            r = score(root, now, write=a.write)
            print(("scored: " if a.write else "would score (not written, use --write): ") + json.dumps(r))
        else:
            w = week_stats(root, a.week, now)
            print(week_md(w) if a.format == "md" else json.dumps(w, indent=1))
        return 0
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
