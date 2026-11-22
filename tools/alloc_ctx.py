"""Shared helpers for the v3 routine tools (DORMANT; paper trading only): the numbers `tools/desk.py memory`, `tools/guidance.py
allocation explain|set` and `tools/context.py --view desk|allocation` print. Read-only on engine state; it writes nothing.

The D5-FI default and the clamp preview are the SAME pure functions the engine uses (simgold/allocation.py), fed from the repo's
files and, when asked, a daily-history download (like `memory_ltm.py regime`). The engine's ALLOC record is authoritative: this
is the tool's estimate before the engine's intraday overrides. Every failure degrades to "n/a" and a warning, never an exception.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

from simgold import allocation, brief, config, feed, guard, market, memstore, trend

SYMS = allocation.SYMS
METAL = allocation.METAL
SYM_OF = {m: s for s, m in METAL.items()}
LLM_DESKS = ("macro", "gold", "silver", "copper", "risk")
EVIDENCE_DESKS = ("macro", "gold", "silver", "copper")


def analysis_dir(root, day: str) -> Path:
    return Path(root) / "analysis" / day


def fetch_daily(tickers: list[str], period: str):
    """Network: daily history frames (close/high/low). Replaced in tests."""
    from simgold import data
    return data.daily_history(tickers, period=period)


def _num(x):
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    try:
        return x if math.isfinite(x) else None             # an int too large for a float raises OverflowError: not a number here
    except OverflowError:
        return None


def _mem_cache(root, day: str) -> dict | None:
    d = brief.load_json(analysis_dir(root, day) / "memory.json", None)
    inp = d.get("inputs") if isinstance(d, dict) else None
    return inp if isinstance(inp, dict) and inp.get("default") else None


def snapshot(root, now: datetime, p: dict | None = None, fetch: bool = True) -> dict:
    """Everything the clamp needs. fetch=True downloads daily history when today's cached memory desk is missing; fetch=False only
    reads files (context views). Missing pieces are None and explained in `warnings`."""
    p = p or config.load_params()
    root = Path(root)
    cfg = allocation.settings(p)
    st = p["strategy"]
    today = now.date()
    warn: list[str] = []
    pf = brief.load_json(root / "state" / "portfolio.json", {})
    pf = pf if isinstance(pf, dict) else {}
    lp = brief.load_json(root / "state" / "last_prices.json", {})
    lp = lp if isinstance(lp, dict) else {}
    prices, pos = {}, pf.get("positions") if isinstance(pf.get("positions"), dict) else {}
    for s in SYMS:
        row = lp.get(s)
        px = _num(row.get("price")) if isinstance(row, dict) else None
        if not px and isinstance(pos.get(s), dict):
            px = _num(pos[s].get("last_price")) or _num(pos[s].get("avg_cost"))
        if px and px > 0:
            prices[s] = float(px)
    value = float(_num(pf.get("cash")) or 0.0) + float(_num(pf.get("liquidbees")) or 0.0)
    for s, q in pos.items():
        if s in p["instruments"] and isinstance(q, dict):
            px = prices.get(s) or _num(q.get("avg_cost")) or 0.0
            value += float(_num(q.get("qty")) or 0) * px * (1 - p["instruments"][s]["cost"] / 2)
    cur = {s: 0.0 for s in SYMS}
    for s in SYMS:
        q = pos.get(s)
        if isinstance(q, dict) and value > 0 and prices.get(s):
            cur[s] = float(_num(q.get("qty")) or 0) * prices[s] / value
    rt = pf.get("runtime") if isinstance(pf.get("runtime"), dict) else {}
    v2 = rt.get("v2") if isinstance(rt.get("v2"), dict) else {}
    br = v2.get("brake") if isinstance(v2.get("brake"), dict) else {}
    level = int(br.get("level") or 0) if _num(br.get("level")) is not None else 0
    peak = _num(br.get("peak")) or _num(pf.get("peak_equity")) or value
    stop = float(_num(st.get("account_stop")) or 75000)
    cd = pf.get("cooldowns") if isinstance(pf.get("cooldowns"), dict) else {}
    halted = isinstance(cd.get("account_stop_latched"), dict)
    v3 = rt.get("v3") if isinstance(rt.get("v3"), dict) else {}
    ledger = v3.get("shadow_ledger" if cfg["shadow"] else "ledger")
    day_left, week_left = allocation.turnover_left(ledger if isinstance(ledger, dict) else {}, today, cfg, market.prev_trading_day)
    snap = {"cfg": cfg, "value": round(value, 2), "peak": round(float(peak), 2), "dd": (value / peak - 1.0) if peak else 0.0,
            "level": level, "halted": halted, "reconfirm": [s for s in (br.get("reconfirm") or []) if s in SYMS],
            "stop": stop, "stop_dist": round(value - stop, 2), "cur": cur, "prices": prices, "day_left": day_left,
            "week_left": week_left, "default": None, "on": None, "sigma": None, "cov": None, "source": "none",
            "halve": [], "zero": [], "warnings": warn, "underwater": value < float(p["capital"]["start"])}
    # ---- alignment halving and the results window, as the engine's v3_context does
    try:
        al = memstore.current_alignment(root, now, p)
        for s in SYMS:
            m = METAL[s]
            if al.get(m, {}).get("state") in allocation.DEFENSIVE_STATES or memstore.thesis_cautious(root, m):
                snap["halve"].append(s)
        snap["align"] = {m: r.get("state") for m, r in al.items()}
    except Exception:                                    # noqa: BLE001
        snap["halve"] = list(SYMS)
        snap["align"] = {}
        warn.append("alignment unreadable: every OFF-trend cap halved")
    try:
        ev = guard.normalise_events(brief.load_json(root / "state" / "events.json", []))
        er = guard.event_rules(today, ev, p)
        zero = {r["sym"] for r in er.rules if r.get("kind") == "all_day" and str(r.get("why", "")).startswith("Hindustan Copper results")}
        snap["zero"] = sorted((zero | set(er.forced_exits)) & set(SYMS))
    except Exception:                                    # noqa: BLE001
        warn.append("events unreadable: results window not applied")
    # ---- trend, default and covariance: today's cached memory desk, else a history download
    cache = _mem_cache(root, today.isoformat())
    if cache:
        snap.update(default={s: float(cache["default"].get(s, 0.0)) for s in SYMS}, on=cache.get("on"), sigma=cache.get("sigma"),
                    cov=cache.get("cov"), source="analysis/memory.json")
    elif fetch:
        try:
            _live(snap, root, now, p)
        except Exception as e:                           # noqa: BLE001
            warn.append(f"no price history ({type(e).__name__}): the default and the preview are unavailable")
    else:
        warn.append("no memory desk for today: run python tools/desk.py memory --write for the default weights")
    return snap


def _live(snap: dict, root: Path, now: datetime, p: dict) -> None:
    import pandas as pd
    st, cfg, today = p["strategy"], snap["cfg"], now.date()
    tick = {s: p["engine"]["tickers"][s] for s in SYMS}
    daily = fetch_daily(list(tick.values()), str(st.get("history_period", "3y")))
    infos, series = {}, {}
    for s, tk in tick.items():
        if tk not in daily["close"]:
            continue
        bars = feed.completed_bars(daily, tk, today)
        live = snap["prices"].get(s) or (bars.close[-1] if bars.close else None)
        info = trend.compute(s, st["symbols"][s]["signal"], bars, live, st)
        if info is not None:
            infos[s] = info
        c = daily["close"][tk].dropna()
        d = {pd.Timestamp(i).date().isoformat(): float(v) for i, v in c.items() if pd.Timestamp(i).date() < today}
        if snap["prices"].get(s):
            d[today.isoformat()] = snap["prices"][s]
        series[s] = d
    if not infos:
        raise ValueError("no trend state")
    default, _detail = allocation.default_d5fi({s: (i.on, i.sigma) for s, i in infos.items()}, st, cfg, snap["reconfirm"])
    snap.update(default=default, on={s: bool(i.on) for s, i in infos.items()},
                sigma={s: (round(i.sigma, 4) if i.sigma is not None else None) for s, i in infos.items()},
                cov=allocation.ewma_cov(series, int(st.get("vol_span", 60))), source="history download")
    if len(infos) < 3:
        snap["warnings"].append("trend state missing for: " + ", ".join(s for s in SYMS if s not in infos))


def preview(snap: dict, obj: dict | None, now: datetime, p: dict, trust: float | None = None):
    """The clamp the engine would run for proposal `obj` at `now` (trust None = the configured ai_trust). Returns (AllocResult, notes).
    The proposal is read as if it were for today (a tool preview); raises ValueError when the default is unavailable."""
    if snap.get("default") is None:
        raise ValueError("the default weights are unavailable")
    cfg = dict(snap["cfg"])
    if trust is not None:
        cfg["ai_trust"] = trust
    adv, notes = None, []
    if obj is not None:
        o = dict(obj)
        o["for_date"] = now.date().isoformat()
        wr = allocation._dt(o.get("written"), now.tzinfo)                     # noqa: SLF001
        if wr is None or wr > now:
            o["written"] = now.isoformat(timespec="seconds")
        adv, notes = allocation.resolve_advice(o, now, cfg)
    on = snap.get("on") or {}
    inval = allocation.invalidated_symbols(adv, snap["prices"])
    st = p["strategy"]
    res = allocation.clamp(
        default=snap["default"], advice=adv.weights if adv else None, confidence=adv.confidence if adv else 0.0, current=snap["cur"],
        on={s: bool(on.get(s, False)) for s in SYMS}, cfg=cfg, cov=snap.get("cov"), halve=snap["halve"], zero=snap["zero"],
        ineligible=snap["reconfirm"], invalidated=inval, brake_level=snap["level"], brake_mult=trend.brake_mult(snap["level"], st),
        halted=snap["halted"], day_left=snap["day_left"], week_left=snap["week_left"], underwater=snap["underwater"],
        notes=notes, advice_source=adv.source if adv else "none")
    return res, notes


def pct(m: dict | None) -> str:
    return "n/a" if not m else " / ".join(f"{m[s] * 100:.1f}" for s in SYMS)


def render_preview(snap: dict, res_cfg, res_full, label: str = "") -> list[str]:
    """Text for `allocation explain` and the set preview: G/S/C percent at each stage, then the clamp lines."""
    L = [f"clamp preview{label} (G/S/C %; the engine decides at 15:10, this is its pipeline on today's numbers):",
         f"  default (D5-FI)   {pct(res_cfg.default)}", f"  proposal          {pct(res_cfg.proposal)}"]
    for res, name in ((res_cfg, f"at configured trust {snap['cfg']['ai_trust']:g}"), (res_full, "at full trust 1.0 (illustrative)")):
        if res is None:
            continue
        L.append(f"  -- {name}: k={res.k:.2f}, advice {res.advice_source}, confidence used {res.confidence:.2f}")
        for k, lab in (("after_tilt", "after tilt"), ("after_caps", "after caps"), ("after_vol", "after vol guard"),
                       ("after_brake", "after brakes"), ("after_turnover", "after turnover"), ):
            L.append(f"     {lab:<16}{pct(res.stages[k])}")
        L.append(f"     {'FINAL':<16}{pct(res.final)}  (current {pct(snap['cur'])})")
        for c in res.clamps:
            if c.get("from") is None and c.get("to") is None:
                L.append(f"     note {c['rule']}: {c.get('why', '')}")
            else:
                big = abs((c["to"] or 0) - (c["from"] or 0)) > 0.05
                L.append(f"     clamp {c['rule']} {c['sym']} {c['from']:.3f}->{c['to']:.3f}{' (more than 5 points)' if big else ''}"
                         + (f": {c['why']}" if c.get("why") else ""))
    L.append(f"  brake level {snap['level']}, halted {snap['halted']}, turnover left day {snap['day_left']:.2f} week {snap['week_left']:.2f}"
             + (f"; vol {res_cfg.vol_before:.2f}->{res_cfg.vol_after:.2f}" if res_cfg.vol_before is not None and res_cfg.vol_after is not None else ""))
    return L


def desk_statuses(root, day: str, p: dict | None = None) -> dict[str, str]:
    """{desk: ok|abstain|invalid|missing} for the five LLM desks plus `memory` (ok|degraded|invalid|missing). Reads analysis/D/ only."""
    import desk as _desk
    out = {}
    for d in LLM_DESKS:
        out[d] = _desk.read_status(root, day, d)[0]
    out["memory"] = _desk.memory_status(root, day)
    return out


def last_alloc(root, now: datetime, back: int = 12) -> dict | None:
    """The newest ALLOC record in logs/decisions-D.jsonl (engine-written): {date, mode, alloc, executed} or None."""
    import calibration
    files = sorted((Path(root) / "logs").glob("decisions-????-??-??.jsonl"))[-back:]
    for f in reversed(files):
        recs = calibration._alloc_records(root, f.stem.split("decisions-", 1)[1])      # noqa: SLF001
        if recs:
            r = recs[-1]
            return {"date": f.stem.split("decisions-", 1)[1], "mode": r.get("alloc_mode"), "alloc": r["alloc"],
                    "executed": r.get("executed")}
    return None


def today_str(now: datetime) -> str:
    return now.date().isoformat()


def trading_today(now: datetime) -> bool:
    return market.is_trading_day(now.date())
