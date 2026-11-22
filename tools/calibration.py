#!/usr/bin/env python3
"""Calibration ledger for strategy v3 (DORMANT; paper trading only): records every scenario set and desk call, scores them
later (Brier) and tracks the paired AI-vs-default result. Code only: no web, no AI. The ledger lives in
memory/ltm/calibration.json and is written ONLY by this module through the memory safe writer (memstore.write_atomic).

  python tools/calibration.py show                  # the summary the routines read (desk_brier, ai_vs_default)
  python tools/calibration.py score [--write]       # score everything that is due (needs closes: network only if something is due)
  python tools/calibration.py week --week 2026-W47 [--format json|md]   # weekly stats (also: tools/evaluate.py --week)

Nothing is recorded by hand: `tools/guidance.py allocation set|hold-default` records the scenario set and `tools/desk.py write`
records the desk calls. `tools/memory_ltm.py perf --write` scores what is due and copies the summary into performance.json
(`ai_vs_default`, `desk_brier`). The ledger holds numbers and symbols only (no free text), so nothing in it can steer a later run.

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
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import allocation, brief, market, memstore

FILE = "calibration.json"
MAX_BYTES = 40_000
KEEP_SETS = 46
KEEP_PAIRS = 130
KEEP_SCORES = 40
KEEP_OPEN_DAYS = 45
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
    return {"v": 1, "updated": None, "sets": [], "calls": [], "pairs": [], "scores": {}}


def load(root) -> dict:
    d = brief.load_json(path(root), None)
    if not isinstance(d, dict) or not isinstance(d.get("sets"), list):
        return empty()
    base = empty()
    base.update({k: d[k] for k in base if k in d and isinstance(d[k], type(base[k]))})
    return base


def _text(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def save(root, obj: dict, now: datetime) -> None:
    """Trim to the caps (oldest scored first) and write atomically. The only writer of memory/ltm/calibration.json."""
    obj["updated"] = now.isoformat(timespec="seconds")
    obj["sets"] = sorted(obj["sets"], key=lambda s: (s["d"], s["ph"]))
    scored = [s for s in obj["sets"] if s.get("st") == "s"]
    while len(scored) > KEEP_SETS:
        obj["sets"].remove(scored.pop(0))
    obj["pairs"] = obj["pairs"][-KEEP_PAIRS:]
    for k, rows in list(obj["scores"].items()):
        obj["scores"][k] = rows[-KEEP_SCORES:]
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
    """Record the object `allocation set|hold-default` just wrote (already filtered by allocation.sanitize_allocation).
    One row per (for_date, phase): a second write replaces the first. Never raises into the caller's write."""
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
    led = load(root)
    led["sets"] = [s for s in led["sets"] if not (s["d"] == row["d"] and s["ph"] == row["ph"])] + [row]
    save(root, led, now)
    return f"ledger: {row['d']} {'final' if row['ph'] == 'f' else 'provisional'} recorded ({len(sc)} scenarios)"


def record_desk(root, now: datetime, desk: dict) -> str:
    """Record a desk file's direction calls (numbers only). An abstaining desk records nothing."""
    if desk.get("abstain"):
        return "ledger: abstaining desk, nothing recorded"
    name, day, n = desk["desk"], desk["date"], 0
    led = load(root)
    led["calls"] = [c for c in led["calls"] if not (c["d"] == day and c["k"] == name)]
    for m, v in (desk.get("view") or {}).items():
        if m in METALS and isinstance(v, dict) and v.get("dir_5d") in _DC and v.get("p_up") is not None and v.get("p_down") is not None:
            led["calls"].append({"d": day, "k": name, "m": m, "o": _DC[v["dir_5d"]],
                                 "u": round(float(v["p_up"]), 4), "n": round(float(v["p_down"]), 4)})
            n += 1
    save(root, led, now)
    return f"ledger: desk {name} {n} call(s) recorded"


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
    led = load(root)
    today = now.date()
    due = [s for s in led["sets"] if s.get("st") == "o"]
    pending = lambda d, h: (market.add_trading_days(date.fromisoformat(d), max(h, 1) - 1) < today)        # noqa: E731
    due_sets = [s for s in due if pending(s["d"], s["h"])]
    due_calls = [c for c in led["calls"] if pending(c["d"], 5)]
    res = {"scored_sets": 0, "scored_calls": 0, "pairs": 0, "open": 0}
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
        led["scores"].setdefault(c["k"], []).append([c["d"], c["m"], round(brier(c["u"], c["n"], outcome_of(*wr)), 4)])
        res["scored_calls"] += 1
    led["calls"] = keep
    res["open"] = sum(1 for s in led["sets"] if s.get("st") == "o") + len(led["calls"])
    if write and (res["scored_sets"] or res["scored_calls"] or res["pairs"] or any(s.get("sk") for s in led["sets"])):
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
               "advice": a.get("advice") if a else None, "held": None,
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
    return {"week": week.upper(), "days": out_days, "held_now": None if not isinstance(pf, dict) else {
        "positions": {k: v.get("qty") for k, v in (pf.get("positions") or {}).items() if isinstance(v, dict)}},
            "turnover_used": round(used, 4), "turnover_budget_week": cfg["max_turnover_week"],
            "clamp_counts": clamps, "ai_vs_default_pairs": pairs,
            "ai_vs_default_week_pp": round(sum(x["pp"] for x in pairs), 4) if pairs else None,
            "desk_brier": s["desk_brier"] if s else None, "ai_vs_default": s["ai_vs_default"] if s else None,
            "notes": ["held per day is not logged: only the current positions are shown",
                      "D5 (v2) and GOLDBEES buy-and-hold P&L are in `python tools/evaluate.py --month` (scorecard)"]}


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
