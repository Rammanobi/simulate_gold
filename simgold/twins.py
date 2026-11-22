"""Shadow paper-twin ledgers (DORMANT: `twins.enabled` is false in config/params.yaml). Paper trading only, no real money.

Seven VIRTUAL books that never trade, never touch state/portfolio.json and never feed back into the engine. Once a day, at the
15:10 trend check, each book is marked to the live prices and (for the rule-based twins) rebalanced to what its rule says,
so the owner can read live A/B evidence of the ideas in strategy/PROFIT_BRAINSTORM.md in weeks instead of years.

  T0  real book mirror          the real portfolio's units and cash (+ LIQUIDBEES), marked the same way (comparison only)
  T1  D5-FI default             always fully invested: allocation.default_d5fi (v2 trend + vol sizing), residual to the GOLDBEES sink
  T2  D5-FI, split sink         as T1, but `nifty_share` (50%) of the GOLDBEES weight sits in NIFTYBEES (price feed only)
  T3  D5-FI, no silver trend    the silver trend sleeve is switched off; silver is only a static `silver_sink_share` of the sink
  T4  D5-FI + gold-silver tilt  as T1, plus a slow tilt of at most +-`gsr_cap` (10 points) between GOLDBEES and SILVERBEES from
                                the z-score of log(GOLDBEES / SILVERBEES) (an ETF-price proxy of the ratio; the unit scale cancels)
  T5  buy and hold GOLDBEES     bought on the start day, never touched
  T6  buy and hold 50/50        GOLDBEES + NIFTYBEES, bought on the start day, never rebalanced

Rules shared by T1 to T4 (the "D5-FI family"), two modes (`twins.t1_mode`, applies to all four so they stay comparable):
  v3      (default) the merged v3 default pipeline: allocation.default_d5fi, then allocation.clamp with no AI (caps, the 60-day EWMA
          portfolio-volatility guard from the engine's daily history, brakes with rotate-to-gold, the latched Rs 75,000 account stop,
          the 0.20/day 0.40/week turnover limiter with the ramp-in limits while the book is mostly idle), the 3-point band, whole units,
          and the 3%/day 6%/week dust sweep of leftover cash into the sink. A twin therefore RAMPS IN from cash like the real book.
  simple  the older shortcut: default_d5fi, the plain brake, the band, and a sweep of ALL leftover cash. A twin is fully invested on its
          first check (no ramp, no volatility guard, no turnover limit).
Not modelled in either mode: the AI, the alignment/thesis halving, the results window, the risk-sell credit carried over to later days,
the intraday stops and the event rules. "Sink" means the GOLDBEES weight of the D5-FI default (sleeve and floor together), as in the
research backtest. T5 and T6 are fully invested from their first day, so against a ramping twin they differ in exposure, not only in rule.

Costs: the engine's own fill model (`broker.trade_costs` + `config.COSTS.slippage_pct`, whole units, the same shrink-to-afford loop as
`Portfolio.execute`), or the research backtest's flat per-side cost with `twins.cost_model: flat`. Every twin starts from the real
portfolio's mark-to-market value on its start day (T2 and T6 wait for the first NIFTYBEES price; without one they stay pending and
a missing price later only means "no trade, last price carried, note n/a": it never fails anything).

Everything in `run_day` is a plain function of its inputs (no clock, no network, no randomness). The only I/O is at the bottom:
`load_state`, `save_state` (atomic, size-capped, schema versioned) and `append_rows` (logs/twins-YYYY-MM.csv). `daily_hook` is the
one entry the engine calls; it reads the engine's objects and never mutates them.
"""
from __future__ import annotations

import copy
import csv
import json
import logging
import math
import os
import random
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

from . import allocation, broker, config, market, trend

log = logging.getLogger(__name__)

SCHEMA = 1
SYMS = allocation.SYMS                                  # GOLDBEES, SILVERBEES, HINDCOPPER: the real trading universe
SINK = allocation.SINK
GOLD, SILVER, COPPER = SYMS
NIFTY = "NIFTYBEES"                                     # price-only: NEVER part of the real universe
BOOK_SYMS = SYMS + (NIFTY,)
TWINS = ("T0", "T1", "T2", "T3", "T4", "T5", "T6")
LABELS = {
    "T0": "Real book (mirror)",
    "T1": "D5-FI default (gold sink)",
    "T2": "D5-FI, sink 50/50 gold+NIFTY",
    "T3": "D5-FI, no silver trend rule",
    "T4": "D5-FI + gold-silver tilt",
    "T5": "Buy and hold GOLDBEES",
    "T6": "Buy and hold 50/50 gold+NIFTY",
}
NIFTY_TWINS = ("T2", "T6")
RULE_TWINS = ("T1", "T2", "T3", "T4")
STATE_NAME = "twins.json"
MAX_STATE_BYTES = 32_768
MAX_CSV_BYTES = 262_144
MAX_NOTES = 6
MAX_TRIES = 3                                           # per day: the daily hook retries a failed write this often (the feed is asked once)
EPS = 1e-9
CSV_COLUMNS = ["date", "twin", "value", "cash", "w_GOLDBEES", "w_SILVERBEES", "w_HINDCOPPER", "w_NIFTYBEES", "w_cash",
               "peak", "dd", "days", "days_green", "green", "note"]

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "nifty_ticker": "NIFTYBEES.NS",
    "nifty_share": 0.5,
    "silver_sink_share": 0.15,
    "gsr_window": 504,
    "gsr_min_obs": 252,
    "gsr_k": 0.05,
    "gsr_cap": 0.10,
    "cost_model": "engine",
    "flat_cost_per_side": {GOLD: 0.0015, SILVER: 0.0015, COPPER: 0.0030, NIFTY: 0.0015},
    "nifty_max_age_min": 15,
    "nifty_jump": 0.25,
    "nifty_timeout_s": 5.0,
    "t1_mode": "v3",
    "split_override": None,
}


# ---------------------------------------------------------------- settings
def _bound(raw: Mapping, key: str, lo: float, hi: float) -> float:
    v = raw.get(key, DEFAULTS[key])
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        v = DEFAULTS[key]
    return min(max(float(v), lo), hi)


def settings(p: Mapping | None = None) -> dict:
    """The `twins:` block of config/params.yaml with defaults and bounds. Garbage falls back to the default; only a literal
    `enabled: true` switches the twins on."""
    raw = p.get("twins") if isinstance(p, Mapping) and isinstance(p.get("twins"), Mapping) else {}
    flat = dict(DEFAULTS["flat_cost_per_side"])
    rf = raw.get("flat_cost_per_side")
    if isinstance(rf, Mapping):
        for k in flat:
            v = rf.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 0.05:
                flat[k] = float(v)
    tk = raw.get("nifty_ticker")
    return {
        "enabled": raw.get("enabled") is True,
        "nifty_ticker": tk.strip() if isinstance(tk, str) and tk.strip() and len(tk) <= 32 else DEFAULTS["nifty_ticker"],
        "nifty_share": _bound(raw, "nifty_share", 0.0, 1.0),
        "silver_sink_share": _bound(raw, "silver_sink_share", 0.0, 0.5),
        "gsr_window": int(_bound(raw, "gsr_window", 60, 2000)),
        "gsr_min_obs": int(_bound(raw, "gsr_min_obs", 30, 2000)),
        "gsr_k": _bound(raw, "gsr_k", 0.0, 0.5),
        "gsr_cap": _bound(raw, "gsr_cap", 0.0, 0.10),            # the hard ceiling is 10 points, whatever the file says
        "cost_model": "flat" if str(raw.get("cost_model", "")).strip().lower() == "flat" else "engine",
        "flat_cost_per_side": flat,
        "nifty_max_age_min": _bound(raw, "nifty_max_age_min", 1, 240),
        "nifty_jump": _bound(raw, "nifty_jump", 0.05, 1.0),
        "nifty_timeout_s": _bound(raw, "nifty_timeout_s", 0.05, 30.0),
        "t1_mode": "simple" if str(raw.get("t1_mode", "")).strip().lower() == "simple" else "v3",
        "split_override": _split_override(raw.get("split_override")),
    }


def _split_override(v) -> dict | None:
    """Owner-set NIFTYBEES split: {date: YYYY-MM-DD, factor: 2..100}. From that date every NIFTYBEES unit held becomes `factor` units and
    the carried price is divided by `factor` (once). Anything else is ignored."""
    if not isinstance(v, Mapping):
        return None
    d, f = str(v.get("date", "")), v.get("factor")
    try:
        date.fromisoformat(d)
    except ValueError:
        return None
    if isinstance(f, bool) or not isinstance(f, int) or not 2 <= f <= 100:
        return None
    return {"date": d, "factor": f}


# ---------------------------------------------------------------- cost model (same fill code as broker.Portfolio.execute)
def _kind(sym: str) -> str:
    return "equity" if sym == COPPER else "etf"


def fill_price(px: float, side: str, cfg: Mapping) -> float:
    if cfg.get("cost_model") == "flat":
        return px
    slip = config.COSTS.slippage_pct
    return px * (1 + slip) if side == "BUY" else px * (1 - slip)


def order_cost(value: float, side: str, sym: str, cfg: Mapping) -> float:
    if cfg.get("cost_model") == "flat":
        return value * float(cfg["flat_cost_per_side"].get(sym, 0.0015))
    return broker.trade_costs(value, side, _kind(sym))


def buy(book: dict, sym: str, qty: int, px: float, cfg: Mapping) -> int:
    """Buy up to `qty` whole units at the paper fill price; the order shrinks until cash covers value + costs (the engine's loop).
    Returns the units bought."""
    if qty <= 0 or not (px > 0):
        return 0
    fill = fill_price(px, "BUY", cfg)
    qty = min(int(qty), int(book["cash"] // fill))
    while qty > 0 and qty * fill + order_cost(qty * fill, "BUY", sym, cfg) > book["cash"]:
        qty -= 1
    if qty <= 0:
        return 0
    value = qty * fill
    cost = order_cost(value, "BUY", sym, cfg)
    book["cash"] -= value + cost
    book["units"][sym] = int(book["units"].get(sym, 0)) + qty
    book["costs"] += cost
    book["traded"] += value
    return qty


def sell(book: dict, sym: str, qty: int, px: float, cfg: Mapping) -> int:
    held = int(book["units"].get(sym, 0))
    qty = min(int(qty), held)
    if qty <= 0 or not (px > 0):
        return 0
    fill = fill_price(px, "SELL", cfg)
    value = qty * fill
    cost = order_cost(value, "SELL", sym, cfg)
    book["cash"] += value - cost
    if held - qty > 0:
        book["units"][sym] = held - qty
    else:
        book["units"].pop(sym, None)
    book["costs"] += cost
    book["traded"] += value
    return qty


# ---------------------------------------------------------------- small helpers
def _px_ok(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) and x > 0 else None


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def mark(book: Mapping, px: Mapping[str, float]) -> float:
    """Cash plus units at the last price (no exit-cost haircut; the same mark for every twin, the real mirror included)."""
    return float(book["cash"]) + sum(int(u) * float(px.get(s) or 0.0) for s, u in book["units"].items())


def weights_of(book: Mapping, px: Mapping[str, float]) -> dict[str, float]:
    v = mark(book, px)
    out = {s: (int(book["units"].get(s, 0)) * float(px.get(s) or 0.0) / v if v > 0 else 0.0) for s in BOOK_SYMS}
    out["cash"] = float(book["cash"]) / v if v > 0 else 1.0
    return out


def _round_obj(o, nd: int = 6):
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: _round_obj(v, nd) for k, v in o.items()}
    if isinstance(o, list):
        return [_round_obj(v, nd) for v in o]
    return o


# ---------------------------------------------------------------- gold-silver ratio (T4)
def gsr_z(gold: Mapping[str, float], silver: Mapping[str, float], window: int, min_obs: int) -> float | None:
    """z-score of the last log(gold / silver) against the last `window` common dates (the last point is included). gold and silver are
    {iso date: close}. None with fewer than `min_obs` common dates, a non-positive close or a flat series. High z = silver cheap."""
    dates = sorted(set(gold) & set(silver))[-int(window):]
    if len(dates) < max(int(min_obs), 2):
        return None
    xs = []
    for d in dates:
        g, s = _px_ok(gold[d]), _px_ok(silver[d])
        if g is None or s is None:
            return None
        xs.append(math.log(g / s))
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / len(xs)
    sd = math.sqrt(var)
    if not (sd > 1e-9):
        return None
    return (xs[-1] - mean) / sd


def gsr_tilt(z: float | None, cfg: Mapping) -> float:
    """Points of the book to move from GOLDBEES to SILVERBEES (negative: the other way), at most +-gsr_cap (never more than 10)."""
    if z is None or not math.isfinite(z):
        return 0.0
    return _clip(float(cfg["gsr_k"]) * z, -float(cfg["gsr_cap"]), float(cfg["gsr_cap"]))


@dataclass(frozen=True)
class DayInputs:
    day: str                                              # ISO date of the check
    prices: Mapping[str, float]                           # GOLDBEES, SILVERBEES, HINDCOPPER live prices (all required)
    nifty: float | None                                   # NIFTYBEES price, None = not available today
    infos: Mapping[str, tuple | None]                     # {sym: (trend on, sigma60)} from trend.compute; None = no data
    real_cash: float                                      # the real portfolio's cash + LIQUIDBEES
    real_units: Mapping[str, int]
    gsr_z: float | None
    st: Mapping                                           # params["strategy"]
    acfg: Mapping                                         # allocation.settings(params)
    cov: Mapping | None = None                            # annualised EWMA covariance of the three symbols (v3 mode's volatility guard)
    capital_start: float = 100_000.0                      # capital.start: "underwater" (the lower vol cap) means value below it


def _fi_weights(infos: Mapping, inp: DayInputs, ineligible=()) -> dict[str, float]:
    known = {s: (bool(i[0]), i[1]) for s, i in infos.items() if s in SYMS and i is not None}
    w, _detail = allocation.default_d5fi(known, inp.st, inp.acfg, tuple(ineligible))
    return {s: float(w.get(s, 0.0)) for s in SYMS}


def _variant_default(tid: str, inp: DayInputs, cfg: Mapping, reconfirm) -> tuple[dict | None, str, dict]:
    """The D5-FI default of a rule twin with its one change applied (T3 no silver sleeve, T4 the gold-silver tilt). Weights None = no usable
    trend data (hold). Returns (weights over SYMS, note, infos used)."""
    infos = dict(inp.infos)
    if tid == "T3":                                       # the silver trend sleeve is removed: always OFF, no vol weight of its own
        s = infos.get(SILVER)
        infos[SILVER] = (False, s[1] if s else None)
    w = _fi_weights(infos, inp, reconfirm)
    if sum(w.values()) <= EPS:
        return None, "no trend data, held", infos
    note = ""
    caps = inp.acfg["ai_caps"]
    if tid == "T3":
        g = w[GOLD]
        sil = min(float(cfg["silver_sink_share"]) * g, float(caps[SILVER]))
        w[SILVER], w[GOLD] = sil, g - sil
        note = "silver %.0f%% of sink" % (100 * cfg["silver_sink_share"])
    elif tid == "T4":
        tau = gsr_tilt(inp.gsr_z, cfg)
        on_s = bool(infos.get(SILVER) and infos[SILVER][0])
        cap_s = float(caps[SILVER] if on_s else inp.acfg["off_trend_caps"][SILVER])
        if tau > 0:
            move = max(0.0, min(tau, w[GOLD], cap_s - w[SILVER]))
        else:
            move = -min(-tau, w[SILVER])
        w[GOLD] -= move
        w[SILVER] += move
        note = "tilt %+.3f z=%s" % (move, "n/a" if inp.gsr_z is None else "%.2f" % inp.gsr_z)
    return w, note, infos


def target_weights(tid: str, inp: DayInputs, cfg: Mapping, level: int, reconfirm, halted: bool, book: Mapping | None = None,
                   px: Mapping | None = None) -> tuple[dict | None, str, Any]:
    """(weights over BOOK_SYMS, note, allocation result or None) for a rule twin. weights None = no usable trend data: hold, do not trade.
    Mode v3 (needs `book` and `px`) runs allocation.clamp without any AI; mode simple (or no book) applies the plain brake only."""
    w, note, infos = _variant_default(tid, inp, cfg, reconfirm)
    if w is None:
        return None, note, None
    res = None
    if cfg.get("t1_mode") == "v3" and book is not None and px is not None:
        v = mark(book, px)
        cur = {s: (int(book["units"].get(s, 0)) * px[s] / v if v > 0 else 0.0) for s in SYMS}
        if tid == "T2" and px.get(NIFTY) and v > 0:        # T2's sink is gold + NIFTYBEES: the pipeline sees the whole sink as "gold"
            cur[GOLD] += int(book["units"].get(NIFTY, 0)) * px[NIFTY] / v
        idle = max(0.0, 1.0 - sum(cur.values()))
        ramp = idle > allocation.RAMP_MIN_IDLE and not halted and int(level) < 2
        tcfg = dict(inp.acfg, max_turnover_day=inp.acfg["ramp_turnover_day"], max_turnover_week=inp.acfg["ramp_turnover_week"]) if ramp else inp.acfg
        day_left, week_left = allocation.turnover_left(book.get("ledger"), date.fromisoformat(inp.day), tcfg, market.prev_trading_day)
        res = allocation.clamp(
            default=w, advice=None, confidence=0.0, current=cur, on={s: bool(i[0]) for s, i in infos.items() if i is not None}, cfg=inp.acfg,
            cov=inp.cov, ineligible=set(reconfirm or []), brake_level=int(level), brake_mult=trend.brake_mult(1, inp.st), halted=halted,
            day_left=day_left, week_left=week_left, underwater=v < float(inp.capital_start),
            frozen={s: cur[s] for s in SYMS if infos.get(s) is None},
            sigmas={s: (infos[s][1] if infos.get(s) else None) for s in SYMS}, cov_source="live" if inp.cov else "fallback")
        w = {s: float(res.final[s]) for s in SYMS}
        tags = (["ramp-in"] if ramp else []) + (["vol guard"] if any(str(c.get("rule", "")).startswith("vol") for c in res.clamps) else [])
    else:
        w = allocation._brake(w, SYMS, level, halted, inp.acfg.get("brake_mode", "rotate_to_gold"), trend.brake_mult(1, inp.st))
        w = {s: float(w.get(s, 0.0)) for s in SYMS}
        tags = []
    if level or halted:
        tags.append("latched" if halted else "brake L%d" % level)
    if tags:
        note = (note + " " if note else "") + " ".join(tags)
    out = dict(w)
    out[NIFTY] = 0.0
    if tid == "T2":
        n = out[GOLD] * float(cfg["nifty_share"])
        out[GOLD] -= n
        out[NIFTY] = n
    return out, note, res


def _rebalance(book: dict, targets: Mapping[str, float], px: Mapping[str, float], cfg: Mapping, inp: DayInputs, res=None, day: str = "") -> float:
    """The engine's band rules (trend.decide): a move to or from zero always trades; otherwise only a gap above the band; a new
    entry needs the minimum size. Sells first, then buys with the sink last, then leftover cash goes to the sink (simple mode: all of it;
    v3 mode: the engine's dust sweep, at most dust_day / dust_week of the book into GOLDBEES within its ceiling, whatever the target).
    Returns the dust swept as a share of the book (0 in simple mode)."""
    thr = float(inp.acfg["rebalance_threshold"])
    min_entry = float(inp.st.get("min_entry_weight", 0.01))
    v = mark(book, px)
    if v <= 0:
        return 0.0
    cur = {s: int(book["units"].get(s, 0)) * px[s] / v for s in BOOK_SYMS if px.get(s)}
    for s in BOOK_SYMS:
        if s not in cur:
            continue
        t, held = targets.get(s, 0.0), int(book["units"].get(s, 0))
        if held and t <= EPS:
            sell(book, s, held, px[s], cfg)
        elif held and cur[s] - t > thr:
            sell(book, s, max(1, int((cur[s] - t) * v / px[s])), px[s], cfg)
    v = mark(book, px)
    for s in (COPPER, SILVER, GOLD, NIFTY):
        if s not in cur:
            continue
        t, held = targets.get(s, 0.0), int(book["units"].get(s, 0))
        c = held * px[s] / v if v > 0 else 0.0
        if t <= EPS or t - c <= 0:
            continue
        if (held and t - c > thr) or (not held and t >= min_entry):
            buy(book, s, int((t - c) * v / px[s]), px[s], cfg)
    # leftover cash goes to the sink. Simple mode: all of it, beyond the cash the target itself keeps, into the sink leg furthest below its target.
    # v3 mode: exactly the engine's dust sweep (Engine.v3_sweep): up to dust_day / dust_week of the book into GOLDBEES whatever the target is
    # (so it also runs during the ramp-in), within the gold ceiling, never under a brake 2 or the account stop; no cash is "kept".
    if res is not None:
        if res.brake_level >= 2 or res.halted or inp.infos.get(GOLD) is None or not px.get(GOLD):
            return 0.0
        s = GOLD
        if targets.get(NIFTY, 0.0) > EPS and px.get(NIFTY):                    # T2: the sink is gold + NIFTYBEES, the sweep goes to the leg furthest below target
            s = max((GOLD, NIFTY), key=lambda x: targets.get(x, 0.0) - int(book["units"].get(x, 0)) * px[x] / v)
        spend = book["cash"]
        if s == GOLD:
            spend = min(spend, max(0.0, res.caps[GOLD] - int(book["units"].get(GOLD, 0)) * px[GOLD] / v) * v)
        dust = book.setdefault("dust", {})
        used_week = allocation.window_total(dust, date.fromisoformat(day), market.prev_trading_day)
        left = max(0.0, min(float(inp.acfg["dust_day"]) - float(dust.get(day, 0.0)), float(inp.acfg["dust_week"]) - used_week))
        spend = min(spend, left * v)
        before = book["traded"]
        buy(book, s, int(spend / fill_price(px[s], "BUY", cfg)), px[s], cfg)
        sw = (book["traded"] - before) / v
        if sw > 0:
            dust[day] = float(dust.get(day, 0.0)) + sw
            for k in sorted(dust)[:-10]:
                dust.pop(k, None)
        return sw
    keep = max(0.0, 1.0 - sum(targets.values())) * v
    legs = [s for s in (GOLD, NIFTY) if targets.get(s, 0.0) > EPS and s in cur]
    if not legs or book["cash"] - keep <= 0:
        return 0.0
    legs.sort(key=lambda s: targets[s] - int(book["units"].get(s, 0)) * px[s] / v, reverse=True)
    s = legs[0]
    buy(book, s, int((book["cash"] - keep) / fill_price(px[s], "BUY", cfg)), px[s], cfg)
    return 0.0


# ---------------------------------------------------------------- state
def new_state(day: str, created: str = "") -> dict:
    return {"schema": SCHEMA, "created": created or day, "start_day": day, "last_day": "", "days": 0, "px": {},
            "nifty": {"last_day": "", "missing_days": 0, "status": "pending"}, "books": {}, "notes": []}


def _new_book(tid: str, day: str, value: float, real_value: float) -> dict:
    return {"id": tid, "start_day": day, "start_value": value, "real_start_value": real_value, "cash": value, "units": {},
            "peak": value, "max_dd": 0.0, "days": 0, "days_green": 0, "value": value, "costs": 0.0, "traded": 0.0,
            "brake": None, "latched": False, "bought": False, "ledger": {}, "dust": {}, "entry_loss": None}


def _row(book: dict, day: str, px: Mapping, note: str) -> dict:
    w = weights_of(book, px)
    v = float(book["value"])
    return {"date": day, "twin": book["id"], "value": round(v, 2), "cash": round(float(book["cash"]), 2),
            "w_GOLDBEES": round(w[GOLD], 4), "w_SILVERBEES": round(w[SILVER], 4), "w_HINDCOPPER": round(w[COPPER], 4),
            "w_NIFTYBEES": round(w[NIFTY], 4), "w_cash": round(w["cash"], 4), "peak": round(float(book["peak"]), 2),
            "dd": round(v / float(book["peak"]) - 1.0 if book["peak"] else 0.0, 4), "days": int(book["days"]),
            "days_green": int(book["days_green"]), "green": 1 if v >= float(book["start_value"]) else 0,
            "note": " ".join(str(note).replace(",", ";").split())[:80]}


def _split_factor(last: float, n: float, suspect) -> int | None:
    """A price move is a real split (not a bad print) only when the SAME new level shows up on a second day, and the ratio is a clean
    integer of at least 2 (price divided). Returns the factor, else None."""
    if not isinstance(suspect, Mapping) or not isinstance(suspect.get("px"), (int, float)) or suspect["px"] <= 0:
        return None
    if abs(n / suspect["px"] - 1.0) > 0.05:
        return None
    r = last / n
    f = round(r)
    return int(f) if f >= 2 and abs(r - f) / f <= 0.03 else None


def _apply_split(s: dict, factor: int) -> None:
    for b in s["books"].values():
        u = int(b["units"].get(NIFTY, 0))
        if u:
            b["units"][NIFTY] = u * factor


def _nifty_update(s: dict, inp: DayInputs, cfg: Mapping) -> tuple[bool, str]:
    """Update the carried NIFTYBEES price. Returns (price usable today, status). A missing price carries the last one; a move beyond the
    jump limit is a bad print (logged, ignored, holdings NOT re-marked) unless the same new level repeats on a second day at a clean
    split ratio, or the owner set twins.split_override."""
    px, nst = s["px"], s["nifty"]
    ov = cfg.get("split_override")
    if ov and inp.day >= ov["date"] and nst.get("override_done") != ov["date"]:
        _apply_split(s, int(ov["factor"]))
        if px.get(NIFTY):
            px[NIFTY] = px[NIFTY] / int(ov["factor"])
        nst["override_done"] = ov["date"]
        nst.pop("suspect", None)
        log.warning("twins: NIFTYBEES split x%d applied from twins.split_override (%s)", ov["factor"], ov["date"])
    n, last = _px_ok(inp.nifty), _px_ok(px.get(NIFTY))
    if n is not None and last is not None and abs(n / last - 1.0) > float(cfg["nifty_jump"]) + 0.05 * int(nst.get("missing_days", 0)):
        f = _split_factor(last, n, nst.get("suspect"))
        if f:
            _apply_split(s, f)
            px[NIFTY], nst["last_day"], nst["missing_days"], nst["status"] = n, inp.day, 0, "split x%d" % f
            nst.pop("suspect", None)
            log.warning("twins: NIFTYBEES %.2f -> %.2f seen on two days: treated as a x%d split", last, n, f)
            return True, nst["status"]
        nst["suspect"] = {"day": inp.day, "px": n}
        nst["missing_days"] = int(nst.get("missing_days", 0)) + 1
        nst["status"] = "n/a (bad print %.2f vs %.2f)" % (n, last)
        log.warning("twins: NIFTYBEES print %.2f is %.0f%% away from the last price %.2f: ignored as a bad print (holdings keep the last price)",
                    n, 100 * abs(n / last - 1.0), last)
        return False, nst["status"]
    if n is not None:
        px[NIFTY], nst["last_day"], nst["missing_days"], nst["status"] = n, inp.day, 0, "ok"
        nst.pop("suspect", None)
        return True, "ok"
    nst["missing_days"] = int(nst.get("missing_days", 0)) + 1
    nst["status"] = "n/a" if NIFTY in px else "pending"
    return False, nst["status"]


def _finish(s: dict, day: str, st: Mapping) -> None:
    """The summary keys other readers use (`since`, `asof`, `twins`, `common_start`), the account-stop level and the stored day."""
    books = s["books"]
    s["since"], s["asof"], s["account_stop"] = s["start_day"], day, float(st.get("account_stop", 0) or 0)
    s["twins"] = {tid: {"value": round(b["value"], 2), "return_pct": round((b["value"] / b["start_value"] - 1.0) * 100, 3),
                        "max_dd_pct": round(b["max_dd"] * 100, 3), "days": int(b["days"]), "label": LABELS[tid]}
                  for tid, b in books.items()}
    latest = max(b["start_day"] for b in books.values())
    vals = {tid: b["value"] for tid, b in books.items()}
    cs = s.get("common_start")
    if not isinstance(cs, dict) or cs.get("date", "") < latest:
        cs = {"date": latest, "base": dict(vals), "peak": dict(vals), "max_dd_pct": {t: 0.0 for t in vals}}
    else:
        for t, v in vals.items():
            cs["peak"][t] = max(cs["peak"].get(t, v), v)
            cs["max_dd_pct"][t] = min(cs["max_dd_pct"].get(t, 0.0), (v / cs["peak"][t] - 1.0) * 100)
    cs["return_pct"] = {t: (v / cs["base"][t] - 1.0) * 100 if cs["base"].get(t) else 0.0 for t, v in vals.items()}
    s["common_start"] = cs


def run_day(state: Mapping | None, inp: DayInputs, cfg: Mapping) -> tuple[dict, list[dict]]:
    """Advance every twin by one check day. Pure: returns (new state, snapshot rows); `state` is not modified. A day that is not
    after state["last_day"] changes nothing (so a repeated minute or a restart can never double-count)."""
    s = copy.deepcopy(dict(state)) if state else new_state(inp.day)
    if inp.day <= str(s.get("last_day") or ""):
        return s, []
    px = s["px"]
    for sym in SYMS:
        p = _px_ok(inp.prices.get(sym))
        if p is None:
            raise ValueError("no usable price for %s" % sym)
        px[sym] = p
    nifty_today, _nstatus = _nifty_update(s, inp, cfg)
    real_units = {k: int(v) for k, v in inp.real_units.items() if k in SYMS and int(v) > 0}
    real_val = float(inp.real_cash) + sum(u * px[k] for k, u in real_units.items())
    rows: list[dict] = []
    for tid in TWINS:
        book = s["books"].get(tid)
        if book is None:
            if tid in NIFTY_TWINS and NIFTY not in px:
                continue                                   # pending: starts on the first day a NIFTYBEES price exists
            book = s["books"][tid] = _new_book(tid, inp.day, real_val, real_val)
        note = ""
        if tid == "T0":
            book["cash"], book["units"] = float(inp.real_cash), dict(real_units)
        elif tid in ("T5", "T6"):
            if not book["bought"]:
                share = float(cfg["nifty_share"]) if tid == "T6" else 0.0
                v0 = mark(book, px)
                buy(book, GOLD, int(v0 * (1 - share) / fill_price(px[GOLD], "BUY", cfg)), px[GOLD], cfg)
                if tid == "T6":
                    buy(book, NIFTY, int(v0 * share / fill_price(px[NIFTY], "BUY", cfg)), px[NIFTY], cfg)
                book["bought"] = True
            elif tid == "T6" and not nifty_today:
                note = "nifty n/a, last price carried"
        else:
            value = mark(book, px)
            if not book["latched"] and value <= float(inp.st.get("account_stop", 0) or 0):
                book["latched"] = True
            known_on = {k: bool(i[0]) for k, i in inp.infos.items() if k in SYMS and i is not None}
            if tid == "T3":
                known_on[SILVER] = False
            br = trend.brake_update(book["brake"], value, known_on, inp.st, float(book["start_value"]), account_stop=book["latched"])
            book["brake"] = {k: v for k, v in br.items() if k != "changed"}
            if tid == "T2" and not nifty_today:
                note = "nifty n/a, no trades"
            else:
                tw, note, res = target_weights(tid, inp, cfg, int(br["level"]), br["reconfirm"], bool(book["latched"]), book, px)
                if tw is not None:
                    dust = _rebalance(book, tw, px, cfg, inp, res, inp.day)
                    if res is not None:                    # the turnover ledger: the discretionary one-way turnover this check planned
                        led = book.setdefault("ledger", {})
                        led[inp.day] = float(led.get(inp.day, 0.0)) + float(res.discretionary_turnover) + dust
                        for k in sorted(led)[:-10]:
                            led.pop(k, None)
        v = mark(book, px)
        if book.get("entry_loss") is None:
            book["entry_loss"] = float(book["start_value"]) - v        # what the first day cost (fees, slippage) at that day's prices
        book["value"] = v
        book["days"] += 1
        book["peak"] = max(float(book["peak"]), v)
        book["max_dd"] = min(float(book["max_dd"]), v / book["peak"] - 1.0 if book["peak"] > 0 else 0.0)
        book["days_green"] += 1 if v >= float(book["start_value"]) else 0
        s["books"][tid] = _round_obj(book)
        rows.append(_row(s["books"][tid], inp.day, px, note))
    s["last_day"], s["days"] = inp.day, int(s["days"]) + 1
    s["px"] = _round_obj(px)
    _finish(s, inp.day, inp.st)
    s = _round_obj(s)
    return s, rows


# ---------------------------------------------------------------- persistence (the only I/O besides the feed)
class TwinsError(Exception):
    """The state file is from a newer schema (left alone) or a write was refused."""


def _valid_state(d) -> bool:
    if not isinstance(d, dict) or not isinstance(d.get("books"), dict) or not isinstance(d.get("px"), dict):
        return False
    if not isinstance(d.get("nifty"), dict) or not isinstance(d.get("last_day", ""), str):
        return False
    for tid, b in d["books"].items():
        if tid not in TWINS or not isinstance(b, dict) or not isinstance(b.get("units"), dict):
            return False
        for k in ("cash", "start_value", "peak", "value", "max_dd", "days", "days_green", "costs", "traded"):
            v = b.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return False
        if b["cash"] < 0 or b["start_value"] <= 0 or any((not isinstance(u, int)) or u < 0 for u in b["units"].values()):
            return False
    return True


def inspect_state(path) -> tuple[dict, str]:
    """(state, problem) WITHOUT touching the file: ({}, "") when there is no file, ({}, reason) when it is oversized, unreadable, invalid or
    from another schema, else (state, ""). A NEWER schema raises TwinsError. The size is checked before the file is read."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {}, ""
    except OSError as e:
        return {}, "cannot stat: %s" % e
    if size > MAX_STATE_BYTES:
        return {}, "%d bytes is over the %d byte cap" % (size, MAX_STATE_BYTES)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {}, "unreadable (%s)" % type(e).__name__
    if isinstance(d, dict) and isinstance(d.get("schema"), int) and not isinstance(d.get("schema"), bool) and d["schema"] > SCHEMA:
        raise TwinsError("twins.json has schema %s, this code knows %s: left alone" % (d["schema"], SCHEMA))
    if not _valid_state(d) or d.get("schema") != SCHEMA:
        return {}, "not a valid twins state"
    return d, ""


def load_state(path, quarantine: bool = True) -> dict:
    """{} when there is no file. A file that is oversized (checked BEFORE reading), corrupt or invalid is moved to twins.json.bad (git-ignored)
    and the twins start afresh (they are only a shadow); with quarantine=False it is left where it is. A NEWER schema raises TwinsError and
    is left untouched."""
    path = Path(path)
    d, why = inspect_state(path)
    if why and quarantine:
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
        log.warning("twins.json is not usable (%s): moved to %s.bad, the twins start afresh", why, path.name)
    return d


def save_state(path, state: dict) -> bool:
    """Atomic write (temp file + rename), compact and sorted. Over MAX_STATE_BYTES the notes are dropped first; still too big: the
    write is refused (False) and the old file stays."""
    path = Path(path)
    st = dict(state)
    text = json.dumps(st, sort_keys=True, separators=(",", ":")) + "\n"
    if len(text.encode("utf-8")) > MAX_STATE_BYTES:
        st["notes"] = []
        text = json.dumps(st, sort_keys=True, separators=(",", ":")) + "\n"
        if len(text.encode("utf-8")) > MAX_STATE_BYTES:
            log.error("twins.json would be %d bytes (cap %d): not written", len(text.encode("utf-8")), MAX_STATE_BYTES)
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def csv_path(logs_dir, day: str) -> Path:
    return Path(logs_dir) / ("twins-%s.csv" % day[:7])


def append_rows(logs_dir, rows: list[dict]) -> int:
    """Append snapshot rows to logs/twins-YYYY-MM.csv (header on a new file). A (date, twin) pair already in the file is skipped, and a
    file at MAX_CSV_BYTES takes no more rows. Returns the rows written."""
    n = 0
    by_file: dict[Path, list[dict]] = {}
    for r in rows:
        by_file.setdefault(csv_path(logs_dir, r["date"]), []).append(r)
    for path, rs in by_file.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        have: set[tuple[str, str]] = set()
        size = 0
        if path.exists():
            size = path.stat().st_size
            with open(path, encoding="utf-8", newline="") as fh:
                have = {(x.get("date", ""), x.get("twin", "")) for x in csv.DictReader(fh)}
        if size >= MAX_CSV_BYTES:
            log.warning("%s is at its size cap: no more rows", path.name)
            continue
        with open(path, "a", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
            if size == 0:
                w.writeheader()
            for r in rs:
                if (r["date"], r["twin"]) not in have:
                    w.writerow({k: r.get(k, "") for k in CSV_COLUMNS})
                    n += 1
    return n


def read_rows(logs_dir, last_days: int | None = None, today: str | None = None) -> list[dict]:
    """All snapshot rows (oldest first), numbers parsed; with `last_days` only the rows of the latest N distinct dates."""
    out: list[dict] = []
    for path in sorted(Path(logs_dir).glob("twins-*.csv")):
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    try:
                        for k in ("value", "cash", "w_GOLDBEES", "w_SILVERBEES", "w_HINDCOPPER", "w_NIFTYBEES", "w_cash", "peak", "dd"):
                            r[k] = float(r[k])
                        for k in ("days", "days_green", "green"):
                            r[k] = int(float(r[k]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    out.append(r)
        except OSError:
            continue
    out.sort(key=lambda r: (r["date"], r["twin"]))
    if last_days:
        keep = sorted({r["date"] for r in out})[-int(last_days):]
        out = [r for r in out if r["date"] in keep]
    return out


# ---------------------------------------------------------------- summaries (read-only helpers for tools/twins.py)
def _by_twin(rows: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["twin"], []).append(r)
    return by


def summarize(rows: list[dict], days: int | None = None) -> dict[str, dict]:
    """Per twin over the latest `days` dates (all rows when None). The return is measured from the last value before the window (or the
    twin's first row when it started inside it); max drawdown and `green` (share of days at or above that BASE value, i.e. in profit
    since the window start) are measured inside the window. `vs_real` is the twin's return minus the real mirror T0's return over the
    SAME span (T0 measured from the twin's own base date, so a late-starting twin is compared fairly)."""
    dates = sorted({r["date"] for r in rows})
    if days:
        dates = dates[-int(days):]
    first = dates[0] if dates else ""
    by = _by_twin(rows)
    t0 = {r["date"]: r["value"] for r in by.get("T0", [])}
    out: dict[str, dict] = {}
    for tid, rs in by.items():
        inside = [r for r in rs if r["date"] in dates]
        if not inside:
            continue
        before = [r for r in rs if r["date"] < first]
        b = before[-1] if before else inside[0]
        base = b["value"]
        peak, mdd = base, 0.0
        for r in inside:
            peak = max(peak, r["value"])
            mdd = min(mdd, r["value"] / peak - 1.0)
        ret = inside[-1]["value"] / base - 1.0 if base > 0 else 0.0
        vs = None
        if tid != "T0" and t0.get(b["date"]) and inside[-1]["date"] in t0:
            vs = ret - (t0[inside[-1]["date"]] / t0[b["date"]] - 1.0)
        out[tid] = {"twin": tid, "rows": len(inside), "from": inside[0]["date"], "base_date": b["date"], "to": inside[-1]["date"],
                    "value": inside[-1]["value"], "ret": ret, "max_dd": mdd, "green": sum(1 for r in inside if r["value"] >= base) / len(inside),
                    "vs_real": vs, "w": {k: inside[-1][k] for k in ("w_GOLDBEES", "w_SILVERBEES", "w_HINDCOPPER", "w_NIFTYBEES", "w_cash")},
                    "note": inside[-1].get("note", "")}
    return out


def summarize_common(rows: list[dict]) -> dict:
    """Every twin measured from ONE date: the latest start date of any twin, from that day's close. Returns {"date", "twins": {id: {...}}}
    (empty twins when fewer than 2 dates are available since then)."""
    by = _by_twin(rows)
    if not by:
        return {"date": "", "twins": {}}
    start = max(rs[0]["date"] for rs in by.values())
    out: dict[str, dict] = {}
    for tid, rs in by.items():
        win = [r for r in rs if r["date"] >= start]
        if len(win) < 2:
            continue
        base, peak, mdd = win[0]["value"], win[0]["value"], 0.0
        for r in win:
            peak = max(peak, r["value"])
            mdd = min(mdd, r["value"] / peak - 1.0)
        out[tid] = {"value": win[-1]["value"], "ret": win[-1]["value"] / base - 1.0 if base > 0 else 0.0, "max_dd": mdd, "rows": len(win)}
    t0 = out.get("T0")
    for tid, d in out.items():
        d["vs_real"] = None if (t0 is None or tid == "T0") else d["ret"] - t0["ret"]
    return {"date": start, "twins": out}


def daily_returns(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{twin: {date: value / previous row's value - 1}}. A twin's first row has no return (its day-1 entry cost is shown separately)."""
    out: dict[str, dict[str, float]] = {}
    for tid, rs in _by_twin(rows).items():
        d: dict[str, float] = {}
        for a, b in zip(rs, rs[1:]):
            if a["value"] > 0:
                d[b["date"]] = b["value"] / a["value"] - 1.0
        out[tid] = d
    return out


def paired_stats(rets: Mapping[str, Mapping[str, float]], twin: str, ref: str, block: int = 5, n_boot: int = 1000, seed: int = 7) -> dict | None:
    """Daily paired difference (twin minus ref) on the dates both have: n, mean, std, t-stat, a moving-block bootstrap 95% interval of the
    mean (block length `block`, seeded, deterministic; None with fewer than 2 blocks of data) and the smallest mean difference a sample this size could separate from zero
    (about 2 standard errors) in percentage points a year. None when either series is missing."""
    a, b = rets.get(twin), rets.get(ref)
    if not a or not b or twin == ref:
        return None
    d = [a[k] - b[k] for k in sorted(set(a) & set(b))]
    n = len(d)
    out: dict[str, Any] = {"twin": twin, "ref": ref, "n": n, "mean": None, "std": None, "t": None, "ci": None, "mde_year_pp": None}
    if n < 2:
        return out
    mean = sum(d) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in d) / (n - 1))
    out.update(mean=mean, std=sd, t=(mean / (sd / math.sqrt(n)) if sd > 1e-12 else None), mde_year_pp=2 * sd / math.sqrt(n) * 252 * 100)
    bl = max(1, int(block))
    if n < 2 * bl:                                       # fewer than two blocks: every resample is the same data, so no interval is honest
        return out
    rng = random.Random(seed)
    means = []
    for _ in range(int(n_boot)):
        sample: list[float] = []
        while len(sample) < n:
            i = rng.randrange(0, n - bl + 1)
            sample.extend(d[i:i + bl])
        means.append(sum(sample[:n]) / n)
    means.sort()
    out["ci"] = (means[int(0.025 * (len(means) - 1))], means[int(round(0.975 * (len(means) - 1)))])
    return out


def cost_table(state: Mapping) -> dict[str, dict]:
    """Gross vs net of fees per twin since its own start, from the state: net return, gross return (value plus the fees paid), fees,
    traded value as a multiple of the start value, and the first day's entry loss (fees + slippage at that day's prices). Slippage is
    inside the net and gross numbers alike (it is in the fill price); only fees are added back."""
    out = {}
    for tid, b in (state.get("books") or {}).items():
        sv = float(b["start_value"])
        out[tid] = {"net": b["value"] / sv - 1.0, "gross": (b["value"] + float(b.get("costs", 0.0))) / sv - 1.0, "fees": float(b.get("costs", 0.0)),
                    "traded_x": float(b.get("traded", 0.0)) / sv, "entry_loss": float(b.get("entry_loss") or 0.0), "start_value": sv}
    return out


# ---------------------------------------------------------------- the engine's entry point
def _close_map(bars, day: str, live_px: float) -> dict[str, float] | None:
    if bars is None:
        return None
    dates, closes = list(getattr(bars, "dates", None) or []), list(getattr(bars, "close", None) or [])
    if not dates or len(dates) != len(closes):
        return None
    m = {str(k)[:10]: v for k, v in zip(dates, closes)}
    m[day] = live_px
    return m


def call_with_timeout(fn: Callable, arg, timeout: float):
    """fn(arg) in a daemon worker thread, waited for at most `timeout` seconds (TimeoutError after that: the thread is left to finish or hang
    on its own and can never block the engine). Exceptions of fn are re-raised."""
    box: dict[str, Any] = {}

    def run():
        try:
            box["v"] = fn(arg)
        except BaseException as e:                    # noqa: BLE001 - handed to the caller
            box["e"] = e

    t = threading.Thread(target=run, name="twins-feed", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("no answer within %.1f s" % timeout)
    if "e" in box:
        raise box["e"]
    return box.get("v")


_TRIES: dict[str, dict] = {}        # in memory: per state file, today's attempts and the NIFTYBEES answer (the feed is asked ONCE a day)


def daily_hook(*, params: Mapping, state_dir, logs_dir, now, prices: Mapping[str, float], fresh: Callable[[str], bool], hist: Mapping, pf,
               checks_complete: bool, nifty_feed: Callable[[str], float | None] | None = None) -> list[dict] | None:
    """Once a day, from the check time: update every twin. Reads the engine's objects, writes only state/twins.json and
    logs/twins-YYYY-MM.csv (the CSV rows FIRST, so a failed state write can never drop a day from the log: the rows are de-duplicated on
    a retry), and returns the rows written (None when nothing was due). The NIFTYBEES feed is asked once a day, in a worker thread, for at
    most `nifty_timeout_s`; a failed write is retried at most MAX_TRIES times a day with that same answer. Raises on bad input; the
    caller wraps the call.  `checks_complete` = every symbol's daily check is done today (T0 then mirrors the finished check); the window
    end (`strategy.check_until`) is the latest time it waits for that."""
    cfg = settings(params)
    st = params.get("strategy") or {}
    if not cfg["enabled"] or not trend.is_v2(params):
        return None
    from . import guard                                    # local: guard is only needed on the dormant path
    t = now.time()
    if t < guard.hhmm(st["check_time"]) or not (checks_complete or t >= guard.hhmm(st["check_until"])):
        return None
    if not all(fresh(s) and _px_ok(prices.get(s)) is not None for s in SYMS):
        return None
    day = now.date().isoformat()
    state_path = Path(state_dir) / STATE_NAME
    key = str(state_path)
    tries = _TRIES.get(key)
    if tries is None or tries["day"] != day:
        tries = _TRIES[key] = {"day": day, "n": 0, "nifty": None, "asked": False}
    if tries["n"] >= MAX_TRIES:
        return None
    tries["n"] += 1
    state = load_state(state_path)
    if state and str(state.get("last_day") or "") >= day:
        tries["n"] = MAX_TRIES
        return None
    infos: dict[str, tuple | None] = {}
    for s in SYMS:
        bars = hist.get(s)
        try:
            i = trend.compute(s, st["symbols"][s]["signal"], bars, prices[s], st) if bars is not None else None
        except Exception:                                  # noqa: BLE001 - one bad series must not stop the other twins
            log.exception("twins: trend state failed for %s", s)
            i = None
        infos[s] = (bool(i.on), i.sigma) if i is not None else None
    g, sv = _close_map(hist.get(GOLD), day, prices[GOLD]), _close_map(hist.get(SILVER), day, prices[SILVER])
    z = gsr_z(g, sv, cfg["gsr_window"], cfg["gsr_min_obs"]) if g is not None and sv is not None else None
    cov = None
    try:
        series = {s: _close_map(hist.get(s), day, prices[s]) for s in SYMS}
        if all(series.values()):
            cov = allocation.ewma_cov(series, int(st.get("vol_span", 60)))
    except Exception:                                      # noqa: BLE001 - the vol guard then uses its conservative stand-in
        log.exception("twins: covariance failed")
    if not tries["asked"]:
        tries["asked"] = True
        if nifty_feed is not None:
            try:
                tries["nifty"] = _px_ok(call_with_timeout(nifty_feed, cfg["nifty_ticker"], cfg["nifty_timeout_s"]))
            except Exception as e:                         # noqa: BLE001 - the feed is optional (a TimeoutError lands here too)
                log.warning("twins: NIFTYBEES price unavailable (%s: %s)", type(e).__name__, e)
    inp = DayInputs(day=day, prices={s: float(prices[s]) for s in SYMS}, nifty=tries["nifty"], infos=infos,
                    real_cash=float(pf.cash) + float(getattr(pf, "liquidbees", 0.0) or 0.0),
                    real_units={s: int(p.qty) for s, p in pf.positions.items() if s in SYMS}, gsr_z=z, st=st,
                    acfg=allocation.settings(params), cov=cov, capital_start=float((params.get("capital") or {}).get("start", 100_000)))
    new, rows = run_day(state or new_state(day, now.isoformat(timespec="seconds")), inp, cfg)
    if rows:
        append_rows(logs_dir, rows)
        if not save_state(state_path, new):
            return None
    tries["n"] = MAX_TRIES
    return rows


def after_tick(pf, mkt, params: Mapping, state_dir, logs_dir, nifty_feed: Callable[[str], float | None] | None = None) -> list[dict] | None:
    """The call `run_live.Live.tick` makes AFTER the portfolio is saved (never inside the engine's tick): derives what the hook needs from the
    portfolio and the market snapshot the tick just used. Read-only on both. Returns the rows written, or None when nothing was due."""
    if settings(params)["enabled"] is not True:
        return None
    now = mkt.now.astimezone(market.IST) if getattr(mkt.now, "tzinfo", None) else mkt.now
    max_age = float((params.get("entry") or {}).get("max_price_age_min", 15))

    def fresh(sym: str) -> bool:
        ts = mkt.price_ts.get(sym)
        return sym in mkt.prices and ((now - ts).total_seconds() / 60 if ts else 0.0) <= max_age

    done = ((getattr(pf, "runtime", None) or {}).get("v2") or {}).get("done") or {}
    complete = done.get("date") == now.date().isoformat() and len(done.get("syms") or {}) >= len(params.get("instruments") or SYMS)
    return daily_hook(params=params, state_dir=state_dir, logs_dir=logs_dir, now=now, prices=mkt.prices, fresh=fresh, hist=mkt.hist, pf=pf,
                      checks_complete=complete, nifty_feed=nifty_feed)
