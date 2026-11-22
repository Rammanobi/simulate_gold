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

Rules shared by T1 to T4: the drawdown brake (trend.brake_update, -15% rotates silver/copper to GOLDBEES, -25% goes to cash), the
latched Rs 75,000 account stop, the 3-point rebalance band, a 1% minimum entry, whole units, and a sweep of leftover cash into the
sink. Not modelled on purpose: the turnover limiter and the AI (a twin jumps to its target on its first check), the intraday stops
and the event rules. "Sink" means the GOLDBEES weight of the D5-FI default (sleeve and floor together), as in the research backtest.

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
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from . import allocation, broker, config, trend

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
    }


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


# ---------------------------------------------------------------- targets
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
    extra: Mapping = field(default_factory=dict)


def _fi_weights(infos: Mapping, inp: DayInputs, ineligible=()) -> dict[str, float]:
    known = {s: (bool(i[0]), i[1]) for s, i in infos.items() if s in SYMS and i is not None}
    w, _detail = allocation.default_d5fi(known, inp.st, inp.acfg, tuple(ineligible))
    return {s: float(w.get(s, 0.0)) for s in SYMS}


def target_weights(tid: str, inp: DayInputs, cfg: Mapping, level: int, reconfirm, halted: bool) -> tuple[dict | None, str]:
    """(weights over BOOK_SYMS, note) for a rule twin. weights None = no usable trend data: hold, do not trade."""
    infos = dict(inp.infos)
    if tid == "T3":                                       # the silver trend sleeve is removed: always OFF, no vol weight of its own
        s = infos.get(SILVER)
        infos[SILVER] = (False, s[1] if s else None)
    w = _fi_weights(infos, inp, reconfirm if level >= 2 else ())
    if sum(w.values()) <= EPS:
        return None, "no trend data, held"
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
    mode = inp.acfg.get("brake_mode", "rotate_to_gold")
    w = allocation._brake(w, SYMS, level, halted, mode, trend.brake_mult(1, inp.st))
    if level or halted:
        note = (note + " " if note else "") + ("latched" if halted else "brake L%d" % level)
    out = {s: float(w.get(s, 0.0)) for s in SYMS}
    out[NIFTY] = 0.0
    if tid == "T2":
        n = out[GOLD] * float(cfg["nifty_share"])
        out[GOLD] -= n
        out[NIFTY] = n
    return out, note


def _rebalance(book: dict, targets: Mapping[str, float], px: Mapping[str, float], cfg: Mapping, inp: DayInputs) -> None:
    """The engine's band rules (trend.decide): a move to or from zero always trades; otherwise only a gap above the band; a new
    entry needs the minimum size. Sells first, then buys with the sink last, then the leftover cash goes to the sink."""
    thr = float(inp.acfg["rebalance_threshold"])
    min_entry = float(inp.st.get("min_entry_weight", 0.01))
    v = mark(book, px)
    if v <= 0:
        return
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
    # leftover cash (whole-unit rounding, band) beyond the cash the target itself keeps: into the sink leg that is furthest below target
    keep = max(0.0, 1.0 - sum(targets.values())) * v
    legs = [s for s in (GOLD, NIFTY) if targets.get(s, 0.0) > EPS and s in cur]
    if legs and book["cash"] - keep > 0:
        legs.sort(key=lambda s: targets[s] - int(book["units"].get(s, 0)) * px[s] / v, reverse=True)
        s = legs[0]
        buy(book, s, int((book["cash"] - keep) / fill_price(px[s], "BUY", cfg)), px[s], cfg)


# ---------------------------------------------------------------- state
def new_state(day: str, created: str = "") -> dict:
    return {"schema": SCHEMA, "created": created or day, "start_day": day, "last_day": "", "days": 0, "px": {},
            "nifty": {"last_day": "", "missing_days": 0, "status": "pending"}, "books": {}, "notes": []}


def _new_book(tid: str, day: str, value: float, real_value: float) -> dict:
    return {"id": tid, "start_day": day, "start_value": value, "real_start_value": real_value, "cash": value, "units": {},
            "peak": value, "max_dd": 0.0, "days": 0, "days_green": 0, "value": value, "costs": 0.0, "traded": 0.0,
            "brake": None, "latched": False, "bought": False}


def _row(book: dict, day: str, px: Mapping, note: str) -> dict:
    w = weights_of(book, px)
    v = float(book["value"])
    return {"date": day, "twin": book["id"], "value": round(v, 2), "cash": round(float(book["cash"]), 2),
            "w_GOLDBEES": round(w[GOLD], 4), "w_SILVERBEES": round(w[SILVER], 4), "w_HINDCOPPER": round(w[COPPER], 4),
            "w_NIFTYBEES": round(w[NIFTY], 4), "w_cash": round(w["cash"], 4), "peak": round(float(book["peak"]), 2),
            "dd": round(v / float(book["peak"]) - 1.0 if book["peak"] else 0.0, 4), "days": int(book["days"]),
            "days_green": int(book["days_green"]), "green": 1 if v >= float(book["start_value"]) else 0,
            "note": " ".join(str(note).replace(",", ";").split())[:80]}


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
    nstat = s["nifty"]
    n, last = _px_ok(inp.nifty), _px_ok(px.get(NIFTY))
    if n is not None and last is not None and abs(n / last - 1.0) > float(cfg["nifty_jump"]) + 0.05 * int(nstat.get("missing_days", 0)):
        n = None                                           # a bad print: ignored like a missing price
        nstat["status"] = "n/a (bad print)"
    if n is not None:
        px[NIFTY], nstat["last_day"], nstat["missing_days"], nstat["status"] = n, inp.day, 0, "ok"
    else:
        nstat["missing_days"] = int(nstat.get("missing_days", 0)) + 1
        if nstat.get("status") != "n/a (bad print)":
            nstat["status"] = "n/a" if NIFTY in px else "pending"
    nifty_today = n is not None
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
                tw, note = target_weights(tid, inp, cfg, int(br["level"]), br["reconfirm"], bool(book["latched"]))
                if tw is not None:
                    _rebalance(book, tw, px, cfg, inp)
        v = mark(book, px)
        book["value"] = v
        book["days"] += 1
        book["peak"] = max(float(book["peak"]), v)
        book["max_dd"] = min(float(book["max_dd"]), v / book["peak"] - 1.0 if book["peak"] > 0 else 0.0)
        book["days_green"] += 1 if v >= float(book["start_value"]) else 0
        s["books"][tid] = _round_obj(book)
        rows.append(_row(s["books"][tid], inp.day, px, note))
    s["last_day"], s["days"] = inp.day, int(s["days"]) + 1
    s["px"] = _round_obj(px)
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


def load_state(path) -> dict:
    """{} when there is no file. A corrupt or invalid file is moved to twins.json.bad and the twins start afresh (they are only a
    shadow); a file from a NEWER schema raises TwinsError and is left untouched."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = None
    if isinstance(d, dict) and isinstance(d.get("schema"), int) and not isinstance(d.get("schema"), bool) and d["schema"] > SCHEMA:
        raise TwinsError("twins.json has schema %s, this code knows %s: left alone" % (d["schema"], SCHEMA))
    if d is None or not _valid_state(d) or d.get("schema") != SCHEMA:
        try:
            os.replace(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
        log.warning("twins.json was not valid: moved to %s.bad, the twins start afresh", path.name)
        return {}
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


# ---------------------------------------------------------------- summary (read-only helper for tools/twins.py)
def summarize(rows: list[dict], days: int | None = None) -> dict[str, dict]:
    """Per twin over the latest `days` dates (all rows when None): return, max drawdown inside the window, share of green days, and the
    difference to the real mirror T0 over the same dates. The return is measured from the last value before the window (or its first
    row when the twin only started inside it)."""
    dates = sorted({r["date"] for r in rows})
    if days:
        dates = dates[-int(days):]
    first = dates[0] if dates else ""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["twin"], []).append(r)
    out: dict[str, dict] = {}
    for tid, rs in by.items():
        inside = [r for r in rs if r["date"] in dates]
        if not inside:
            continue
        before = [r for r in rs if r["date"] < first]
        base = before[-1]["value"] if before else inside[0]["value"]
        peak, mdd = base, 0.0
        for r in inside:
            peak = max(peak, r["value"])
            mdd = min(mdd, r["value"] / peak - 1.0)
        out[tid] = {"twin": tid, "rows": len(inside), "from": inside[0]["date"], "to": inside[-1]["date"], "value": inside[-1]["value"],
                    "ret": inside[-1]["value"] / base - 1.0 if base > 0 else 0.0, "max_dd": mdd,
                    "green": sum(r["green"] for r in inside) / len(inside),
                    "w": {k: inside[-1][k] for k in ("w_GOLDBEES", "w_SILVERBEES", "w_HINDCOPPER", "w_NIFTYBEES", "w_cash")},
                    "note": inside[-1].get("note", "")}
    real = out.get("T0")
    for tid, d in out.items():
        d["vs_real"] = None if (real is None or tid == "T0" or real["from"] > d["from"]) else d["ret"] - real["ret"]
    return out


# ---------------------------------------------------------------- the engine's entry point
def _close_map(bars, day: str, live_px: float) -> dict[str, float] | None:
    if bars is None:
        return None
    dates, closes = list(getattr(bars, "dates", None) or []), list(getattr(bars, "close", None) or [])
    if not dates or len(dates) != len(closes):
        return None
    m = dict(zip(dates, closes))
    m[day] = live_px
    return m


def daily_hook(*, params: Mapping, state_dir, logs_dir, now, prices: Mapping[str, float], fresh: Callable[[str], bool],
               hist: Mapping, pf, checks_complete: bool, nifty_feed: Callable[[str], float | None] | None = None) -> list[dict] | None:
    """Once a day, from the check time: update every twin. Reads the engine's objects, writes only state/twins.json and
    logs/twins-YYYY-MM.csv, and returns the rows written (None when nothing was due). Raises on bad input; the engine wraps the call.
    The caller passes `checks_complete` = every symbol's daily check is done today (T0 then mirrors the finished check); the
    window end (`strategy.check_until`) is the latest time it waits for that."""
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
    state = load_state(state_path)
    if state and str(state.get("last_day") or "") >= day:
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
    z = None
    g, sv = _close_map(hist.get(GOLD), day, prices[GOLD]), _close_map(hist.get(SILVER), day, prices[SILVER])
    if g is not None and sv is not None:
        z = gsr_z(g, sv, cfg["gsr_window"], cfg["gsr_min_obs"])
    npx = None
    if nifty_feed is not None:
        try:
            npx = _px_ok(nifty_feed(cfg["nifty_ticker"]))
        except Exception as e:                             # noqa: BLE001 - the feed is optional
            log.warning("twins: NIFTYBEES price unavailable (%s)", e)
    inp = DayInputs(day=day, prices={s: float(prices[s]) for s in SYMS}, nifty=npx, infos=infos,
                    real_cash=float(pf.cash) + float(getattr(pf, "liquidbees", 0.0) or 0.0),
                    real_units={s: int(p.qty) for s, p in pf.positions.items() if s in SYMS}, gsr_z=z, st=st,
                    acfg=allocation.settings(params))
    new, rows = run_day(state or new_state(day, now.isoformat(timespec="seconds")), inp, cfg)
    if rows:
        if not save_state(state_path, new):
            return None
        append_rows(logs_dir, rows)
    return rows
