"""India price gap: GOLDBEES / SILVERBEES against the world price in rupees. Read-only, dormant, advice for the owner only.

PAPER TRADING ONLY. Nothing here trades, sizes, blocks or changes a rule. The result goes to state/pricegap.json, a LOW alert
category `price_gap` (alerts.py: exempt from safe mode, never emailed on its own) and an optional chip row in the report.
Everything is off until `pricegap.enabled: true` in config/params.yaml, and every failure is swallowed (log only).

What is computed (once per day from 15:10 IST, from Yahoo daily closes that data.daily_history already fetches):

    world INR per gram = COMEX USD per troy ounce x USDINR / 31.1034768          (GC=F gold, SI=F silver, INR=X)
    ETF unit content   = launch grams x (1 - TER) ** years since listing            (ESTIMATE, see below)
    implied unit price = content x world INR per gram x (1 + duty_pct / 100)       ("India parity")
    premium_pct        = ETF price / implied unit price - 1                         (+ = the ETF is dearer than parity)
    world_premium_pct  = the same without the duty term (premium against the bare world price)

`duty_pct` (default 6) is India's statutory import duty on gold and silver since the 23 Jul 2024 budget (5% basic + 1% AIDC):
domestic metal, and so the ETFs that track it, normally trades at about world x 1.06, so +6% over the bare world price is the
NORMAL state and only the excess over it is a "gap". Set `pricegap.duty_pct: 0` to read the bare world premium. Whether the duty
changed again in May 2026 (when the ratio jumped 5-8%) is NOT verified; the cause of the current gap is unknown.

Constants (every one an ESTIMATE; override with `pricegap.gold_g_per_unit` / `silver_g_per_unit` for a fixed gram figure):
  GOLDBEES   launch 0.01 g per unit (the AMC product note: "approximately 0.01 gram of gold per unit", listed 8 Mar 2007).
             The entitlement shrinks by the expense ratio every year. Quoted TER today 0.79-0.81% (sites differ), higher
             (about 1%) in the early years, so TER_AVG = 1.0% a year -> about 0.00820 g per unit in Nov 2026.
  SILVERBEES launch 1.0 g per unit (the AMC does not publish the figure in anything reachable here; price at the 4 Feb 2022
             listing was close to one gram of silver, ratio 1.12 incl. the then 10%+ duty), TER 0.56% (quoted 0.51-0.58%)
             -> about 0.973 g in Nov 2026.
  Cross-check against the data (Yahoo, 2024-08 .. 2026-04, after the July 2024 duty cut): ETF price / bare world INR price was
  0.00870 per gold unit and 1.03 per silver unit, i.e. +6% over these constants, the statutory duty. That agreement is partly
  by construction (the gold TER_AVG sits inside its plausible 0.79-1.0% range and was chosen so it lines up), so the ABSOLUTE
  level is good to about +-3 points; the trend, the 20-day median and the z-score do not depend on it.
  Timing noise: GC=F / SI=F settle on the US clock, the NSE closes 15:30 IST, so a daily pair can be 1-2% off on a big
  US-session day. The 15:10 reading uses the live (partial) bar of every ticker, which lines up in time; older rows do not.
  The 20-day median damps this.

Window statistics: median20 = median of the last `median_window` (20) premiums including today; z = (today - mean) / sd over the
`z_window` (60) sessions BEFORE today (sd floored at 0.25 points so a quiet history cannot blow z up). Fewer than `min_obs` (10)
sessions -> no median / z and no alert.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("pricegap")
IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent

STATE_NAME = "pricegap.json"
MAX_BYTES = 8192                    # the state file is size-capped; history is dropped first
OZ_G = 31.1034768                   # grams in a troy ounce (exact)
FX_TICKER = "INR=X"                 # USD/INR (Yahoo); config.py uses USDINR=X, the same series, INR=X has the longer history
ALERT_COMPONENT, ALERT_STEP, ALERT_CATEGORY = "engine", "price-gap", "price_gap"
SYMS = ("GOLDBEES", "SILVERBEES")

# launch_g: grams of metal per ETF unit at listing; ter: average yearly expense-ratio decay of that entitlement. ESTIMATES.
PRODUCTS = {
    "GOLDBEES": {"metal": "GOLD", "etf": "GOLDBEES.NS", "fut": "GC=F", "listed": "2007-03-08", "launch_g": 0.01, "ter": 0.010,
                 "cfg_g": "gold_g_per_unit"},
    "SILVERBEES": {"metal": "SILVER", "etf": "SILVERBEES.NS", "fut": "SI=F", "listed": "2022-02-04", "launch_g": 1.0, "ter": 0.0056,
                   "cfg_g": "silver_g_per_unit"},
}

DEFAULTS = {
    "enabled": False,               # dormant until the owner switches it on
    "warn_pct": 8.0,                # LOW alert when the premium over India parity is at or above this many percent
    "duty_pct": 6.0,                # statutory import duty included in "India parity" (0 = bare world price)
    "median_window": 20, "z_window": 60, "min_obs": 10, "sd_floor_pts": 0.25,
    "history_period": "6mo", "check_time": "15:10", "history_keep": 20,
    "gold_g_per_unit": None, "silver_g_per_unit": None,        # a number overrides the TER-decay estimate
}


# ------------------------------------------------------------------ config
def settings(params: dict | None = None) -> dict:
    """The `pricegap:` block with defaults and sane bounds. Never raises."""
    cfg = dict(DEFAULTS)
    try:
        if params is None:
            from . import config
            params = config.load_params()
        blk = (params or {}).get("pricegap") if isinstance(params, dict) else None
        if isinstance(blk, dict):
            cfg.update({k: v for k, v in blk.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001 - defaults are fine
        pass
    cfg["enabled"] = cfg.get("enabled") is True

    def num(k, lo, hi):
        v = cfg.get(k)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi
        cfg[k] = float(v) if ok else float(DEFAULTS[k])

    num("warn_pct", 0.0, 100.0)
    num("duty_pct", 0.0, 50.0)
    num("sd_floor_pts", 0.01, 10.0)
    for k, lo, hi in (("median_window", 3, 250), ("z_window", 5, 500), ("min_obs", 3, 250), ("history_keep", 0, 60)):
        v = cfg.get(k)
        cfg[k] = int(v) if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else int(DEFAULTS[k])
    for k in ("gold_g_per_unit", "silver_g_per_unit"):
        v = cfg.get(k)
        cfg[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else None
    if not isinstance(cfg.get("history_period"), str) or cfg["history_period"] not in ("3mo", "6mo", "1y", "2y"):
        cfg["history_period"] = DEFAULTS["history_period"]
    ct = str(cfg.get("check_time") or "")
    cfg["check_time"] = ct if len(ct) == 5 and ct[2] == ":" and ct[:2].isdigit() and ct[3:].isdigit() else DEFAULTS["check_time"]
    return cfg


# ------------------------------------------------------------------ the maths (pure)
def content_g(sym: str, on: date, cfg: dict | None = None) -> float:
    """Estimated grams of metal behind one ETF unit on a date: launch grams x (1 - TER) ** years (or the configured fixed figure)."""
    cfg = cfg or DEFAULTS
    p = PRODUCTS[sym]
    fixed = cfg.get(p["cfg_g"])
    if isinstance(fixed, (int, float)) and not isinstance(fixed, bool) and fixed > 0:
        return float(fixed)
    years = max(0.0, (on - date.fromisoformat(p["listed"])).days / 365.25)
    return p["launch_g"] * (1.0 - p["ter"]) ** years


def world_inr_per_g(usd_per_oz: float, usdinr: float) -> float:
    """COMEX USD per troy ounce x USDINR -> rupees per gram."""
    return usd_per_oz * usdinr / OZ_G


def implied_unit_price(sym: str, usd_per_oz: float, usdinr: float, on: date, cfg: dict | None = None, duty: bool = True) -> float:
    """What one ETF unit 'should' cost: content x world INR per gram x (1 + duty). duty=False gives the bare world price."""
    cfg = cfg or DEFAULTS
    d = float(cfg.get("duty_pct", DEFAULTS["duty_pct"])) if duty else 0.0
    return content_g(sym, on, cfg) * world_inr_per_g(usd_per_oz, usdinr) * (1.0 + d / 100.0)


def premium_pct(sym: str, etf: float, usd_per_oz: float, usdinr: float, on: date, cfg: dict | None = None, duty: bool = True) -> float:
    """(ETF price / implied unit price - 1) x 100. Raises ValueError on a non-positive or non-finite input."""
    for v in (etf, usd_per_oz, usdinr):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"bad input {v!r}")
    return (etf / implied_unit_price(sym, usd_per_oz, usdinr, on, cfg, duty) - 1.0) * 100.0


def window_stats(values: list[float], cfg: dict | None = None) -> dict:
    """Today's premium (the last value), its median over the last `median_window` values and its z against the `z_window`
    values before today. median/z are None below `min_obs` observations."""
    cfg = cfg or DEFAULTS
    vals = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    out = {"n": len(vals), "premium_pct": None, "median20_pct": None, "z": None}
    if not vals:
        return out
    out["premium_pct"] = vals[-1]
    if len(vals) < cfg["min_obs"]:
        return out
    out["median20_pct"] = statistics.median(vals[-cfg["median_window"]:])
    prior = vals[-1 - cfg["z_window"]:-1]
    if len(prior) >= max(3, cfg["min_obs"] - 1):
        sd = statistics.pstdev(prior)
        out["z"] = (vals[-1] - statistics.fmean(prior)) / max(sd, cfg["sd_floor_pts"])
    return out


def series_from_rows(sym: str, rows: list[dict], cfg: dict | None = None, duty: bool = True) -> list[tuple[str, float]]:
    """rows = [{"date": "YYYY-MM-DD", "etf": x, "fut": y, "fx": z}] oldest first -> [(date, premium_pct)], bad rows skipped."""
    out = []
    for r in rows:
        try:
            d = date.fromisoformat(str(r["date"])[:10])
            out.append((d.isoformat(), premium_pct(sym, r["etf"], r["fut"], r["fx"], d, cfg, duty)))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    return out


def rows_from_frame(frame, sym: str) -> list[dict]:
    """A close-price DataFrame (index = dates, columns = Yahoo tickers) -> rows for one symbol."""
    p = PRODUCTS[sym]
    cols = (p["etf"], p["fut"], FX_TICKER)
    if any(c not in frame for c in cols):
        return []
    sub = frame[list(cols)].dropna()
    out: list[dict] = []
    for ix, (a, b, c) in zip(sub.index, sub.itertuples(index=False, name=None)):
        d = str(ix)[:10]
        if date.fromisoformat(d).weekday() >= 5:
            continue                    # data.daily_history forward-fills: a weekend row is only a stale copy
        if out and out[-1]["etf"] == float(a) and out[-1]["fut"] == float(b):
            continue                    # a holiday on either exchange forward-filled the previous bar: not a new observation
        out.append({"date": d, "etf": float(a), "fut": float(b), "fx": float(c)})
    return out


def compute(frame, cfg: dict | None = None, now: datetime | None = None) -> dict | None:
    """Build the state object from a close-price frame. None when no symbol has data."""
    cfg = cfg or settings()
    now = now or datetime.now(IST)
    gaps, hist, world = {}, {}, {}
    for sym in SYMS:
        rows = rows_from_frame(frame, sym)
        ser = series_from_rows(sym, rows, cfg)
        if not ser:
            continue
        st = window_stats([v for _, v in ser], cfg)
        last = rows[-1]
        d = date.fromisoformat(ser[-1][0])
        wser = series_from_rows(sym, rows[-1:], cfg, duty=False)
        gaps[sym] = {"date": ser[-1][0], "price": round(last["etf"], 4), "implied": round(implied_unit_price(sym, last["fut"], last["fx"], d, cfg), 4),
                     "premium_pct": round(st["premium_pct"], 2), "world_premium_pct": round(wser[-1][1], 2) if wser else None,
                     "median20_pct": None if st["median20_pct"] is None else round(st["median20_pct"], 2),
                     "z": None if st["z"] is None else round(st["z"], 2), "n": st["n"],
                     "content_g": round(content_g(sym, d, cfg), 6), "warn": bool(st["median20_pct"] is not None and st["premium_pct"] >= cfg["warn_pct"])}
        world[PRODUCTS[sym]["metal"].lower() + "_inr_g"] = round(world_inr_per_g(last["fut"], last["fx"]), 2)
        world["usdinr"] = round(last["fx"], 4)
        hist[sym] = [round(v, 2) for _, v in ser[-cfg["history_keep"]:]]
    if not gaps:
        return None
    return {"version": 1, "date": now.date().isoformat(), "computed": now.isoformat(timespec="seconds"), "estimate": True,
            "warn_pct": cfg["warn_pct"], "duty_pct": cfg["duty_pct"], "world": world, "gaps": gaps,
            "warn": [s for s, g in gaps.items() if g["warn"]], "history": hist}


# ------------------------------------------------------------------ state file
def state_path(state_dir) -> Path:
    return Path(state_dir) / STATE_NAME


def _atomic_write(path: Path, text: str) -> None:
    from .alerts import atomic_write
    atomic_write(path, text)


def write(state_dir, obj: dict) -> bool:
    """Atomic, size-capped write of state/pricegap.json. False when it would not fit even without the history."""
    text = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    if len(text.encode("utf-8")) > MAX_BYTES:
        text = json.dumps({k: v for k, v in obj.items() if k != "history"}, separators=(",", ":"), sort_keys=True)
    if len(text.encode("utf-8")) > MAX_BYTES:
        return False
    _atomic_write(state_path(state_dir), text + "\n")
    return True


def _f(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def load(state_dir) -> dict | None:
    """state/pricegap.json, shape-checked: {date, warn_pct, duty_pct, gaps{SYM: {...numbers}}}. None when absent or unusable. Never raises."""
    try:
        raw = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("gaps"), dict):
            return None
        date.fromisoformat(str(raw.get("date"))[:10])
        gaps = {}
        for sym in SYMS:
            g = raw["gaps"].get(sym)
            if not isinstance(g, dict) or _f(g.get("premium_pct")) is None:
                continue
            gaps[sym] = {"premium_pct": _f(g["premium_pct"]), "world_premium_pct": _f(g.get("world_premium_pct")),
                         "median20_pct": _f(g.get("median20_pct")), "z": _f(g.get("z")), "n": int(_f(g.get("n")) or 0),
                         "price": _f(g.get("price")), "implied": _f(g.get("implied")), "warn": g.get("warn") is True,
                         "date": str(g.get("date") or raw["date"])[:10]}
        if not gaps:
            return None
        return {"date": str(raw["date"])[:10], "warn_pct": _f(raw.get("warn_pct")) or DEFAULTS["warn_pct"],
                "duty_pct": _f(raw.get("duty_pct")), "gaps": gaps,
                "world": raw["world"] if isinstance(raw.get("world"), dict) else {}}
    except Exception:  # noqa: BLE001 - a bad file is "no data"
        return None


# ------------------------------------------------------------------ alert (LOW, exempt from safe mode, never blocks anything)
def alert_detail(obj: dict) -> str:
    bits = []
    for sym in obj.get("warn") or []:
        g = obj["gaps"][sym]
        z = f", z {g['z']:+.1f}" if g.get("z") is not None else ""
        bits.append(f"{sym} {g['premium_pct']:+.1f}% over India parity (20-day median {g['median20_pct']:+.1f}%{z})")
    return ("India price gap at or above the %.0f%% band: " % obj.get("warn_pct", 0)) + "; ".join(bits) + \
           ". Information only: nothing is blocked or changed. Constants are estimates; cause not verified."


def sync_alert(state_dir, obj: dict) -> None:
    """Raise (or refresh) the LOW `price_gap` alert while a symbol is over the band, resolve it when none is."""
    from . import alerts
    if obj.get("warn"):
        alerts.raise_alert(state_dir, ALERT_COMPONENT, ALERT_STEP, ALERT_CATEGORY, detail=alert_detail(obj), wake=False)
    else:
        fp = alerts.fingerprint(ALERT_COMPONENT, ALERT_STEP)
        e = alerts.load_state(state_dir)["alerts"].get(fp)
        if e and e.get("status") == "open":
            alerts.resolve(state_dir, fp)


# ------------------------------------------------------------------ the daily job (never raises)
def fetch_frame(cfg: dict):
    """Daily closes of both ETFs, both futures and USDINR (one download through data.daily_history)."""
    from . import data
    tickers = [PRODUCTS[s][k] for s in SYMS for k in ("etf", "fut")] + [FX_TICKER]
    return data.daily_history(tickers, period=cfg["history_period"])["close"]


def due(state_dir, cfg: dict, now: datetime) -> bool:
    """True from check_time on a weekday while today's state file does not exist yet."""
    if not cfg["enabled"] or now.weekday() >= 5:
        return False
    hh, mm = (int(x) for x in cfg["check_time"].split(":"))
    if (now.hour, now.minute) < (hh, mm) or (now.hour, now.minute) > (15, 45):
        return False
    try:
        cur = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
        if isinstance(cur, dict) and str(cur.get("date"))[:10] == now.date().isoformat():
            return False
    except (OSError, ValueError):
        pass
    return True


_attempts: dict[str, int] = {}


def run_daily(state_dir, params: dict | None = None, now: datetime | None = None, fetch=None) -> dict | None:
    """Compute and write state/pricegap.json once per day from the check time, then sync the LOW alert. Returns the object written or
    None. Swallows every exception (log only): it must never touch the tick."""
    try:
        cfg = settings(params)
        now = now or datetime.now(IST)
        if not due(state_dir, cfg, now):
            return None
        key = now.date().isoformat()
        if _attempts.get(key, 0) >= 3:                      # three tries a day, then give up until tomorrow
            return None
        _attempts[key] = _attempts.get(key, 0) + 1
        for k in [k for k in _attempts if k != key]:
            _attempts.pop(k, None)
        frame = (fetch or fetch_frame)(cfg)
        obj = compute(frame, cfg, now)
        if obj is None:
            log.warning("pricegap: no usable data")
            return None
        if not write(state_dir, obj):
            log.warning("pricegap: state file too large, not written")
            return None
        try:
            sync_alert(state_dir, obj)
        except Exception:  # noqa: BLE001
            log.exception("pricegap: alert sync failed")
        return obj
    except Exception:  # noqa: BLE001
        log.exception("pricegap: daily job failed (ignored)")
        return None


def run_daily_bounded(state_dir, params: dict | None = None, now: datetime | None = None, fetch=None, timeout: float = 30.0) -> None:
    """run_daily in a daemon thread joined for at most `timeout` seconds, so a hung download can never stall a tick."""
    try:
        if not settings(params)["enabled"]:
            return
        t = threading.Thread(target=run_daily, args=(state_dir, params, now, fetch), daemon=True, name="pricegap")
        t.start()
        t.join(timeout)
    except Exception:  # noqa: BLE001
        log.exception("pricegap: could not start the daily job")
