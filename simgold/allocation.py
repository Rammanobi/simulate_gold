"""Strategy v3 "desk allocation" (engine part, DORMANT): the pure rules. Paper trading only.

A fully invested portfolio of GOLDBEES, SILVERBEES and HINDCOPPER. The engine holds a deterministic default (D5-FI) that
runs every day with or without Claude; a routine may PROPOSE a mix (state/allocation.json) and the engine only TILTS its
own default toward it, then clamps the result to hard limits. Nothing in a proposal can add exposure beyond the limits
below, name another symbol, use leverage or touch a brake or the account stop.

Everything here is a plain function of its inputs (no clock, no network). The only I/O is `load_proposal` (reads one file).

  settings()          the `allocation:` block of config/params.yaml with defaults and bounds
  validate_proposal() schema check of state/allocation.json (the tool and the engine use the same filter). Weights are SHARES of
                      the invested book summing to 1.00 (+-0.02, normalised); there is no cash field; the engine scales to its
                      invested level
  sanitize_allocation()  the PUBLIC filter for the routine tools (tools/guidance.py allocation set|explain|show|hold-default): returns the
                      normalised object to write, or the plain reasons it is refused. Same schema rules as the engine uses; with
                      for_write=True it also applies the write-time rules (date window, FINAL cutoff, memsafe, numbers in rationale)
  resolve_advice()    today's proposal, or yesterday's FINAL at half strength for `carry_days`, or None (the default runs)
  default_d5fi()      D5-FI: the v2 trend rules (simgold/trend.py target_weights_ex) with the invested floor raised to
                      `invested_max`, all leftover weight to GOLDBEES, no cash weight
  clamp()             the pipeline, every step logged with before/after in `AllocResult.clamps`:
                        tilt (default moved toward the advice, strength k = ai_trust x clip((conf - 0.30) / 0.50, 0, 1),
                        at most max_tilt per metal) -> caps (ai_caps; off_trend_caps while the trend is OFF, halved when
                        DEFENSIVE / cautious thesis; results window = 0) -> fill to the invested level (sink GOLDBEES,
                        then other symbols with room) -> portfolio-volatility guard (weight moves to GOLDBEES, never to
                        cash) -> brakes (level 1 -x0.5: rotate to GOLDBEES sink, or cash with brake_mode cash; level 2 and
                        the account stop: all cash) -> turnover limiter (day / rolling week; risk sells are never limited)
                        -> rebalance band.
  The brakes are computed BEFORE the limiter so that the limiter can tell a risk sell (exempt) from a discretionary move;
  this is the same thing as the design's "brakes after the limiter, never limited".

A cap never pushes a weight BELOW the engine's own default: the cap of a symbol is max(configured cap, default weight), so
the caps bound what the AI can add and ai_trust 0 always reproduces the default (up to the guards, which are all logged).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from . import trend

SYMS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")
SINK = "GOLDBEES"                                   # lowest volatility: every leftover weight goes here
METAL = {"GOLDBEES": "GOLD", "SILVERBEES": "SILVER", "HINDCOPPER": "COPPER"}
PHASES = ("off", "shadow", "live_default", "live")
DEFENSIVE_STATES = frozenset({"DEFENSIVE", "DEFENSIVE_SHOCK"})
EPS = 1e-9
FUTURE_SLACK = timedelta(minutes=5)
MAX_FILE_BYTES = 32_768
FINAL_CUTOFF = time(15, 5)                          # a FINAL for today is refused by the tool after 15:05 IST (the check is 15:10)
MAX_ADVANCE_DAYS = 5                                # for_date may be at most this many calendar days ahead (weekend / holiday)
MAX_WRITTEN_AGE_H = 48.0                            # a proposal written longer ago than this is ignored (Saturdays and Sundays do not count)
RAMP_MIN_IDLE = 0.10                                # idle cash (beyond the risk-sell credit) above this share of the book = ramp-in
FALLBACK_VOL = {"GOLDBEES": 0.20, "SILVERBEES": 0.40, "HINDCOPPER": 0.40}    # per-asset annual vol when nothing else is known

DEFAULTS: dict[str, Any] = {
    "shadow": True,                 # True: the v2 (D5) decision trades; the v3 target is computed and LOGGED next to it
    "phase": "shadow",              # shadow (AI logged only, D5-FI trades) | live (the tilt trades); only read when shadow is False
    "invested_target": 0.98,        # MINIMUM invested share (advice sums must be within 5 points of it)
    "invested_max": 1.00,           # the engine aims for this: leftover cash is reinvested in whole units at every check
    "ai_trust": 0.0,
    "ai_caps": {"GOLDBEES": 1.00, "SILVERBEES": 0.50, "HINDCOPPER": 0.25},
    "off_trend_caps": {"GOLDBEES": 1.00, "SILVERBEES": 0.20, "HINDCOPPER": 0.10},
    "max_tilt": 0.30,
    "max_turnover_day": 0.20,
    "max_turnover_week": 0.40,
    "rebalance_threshold": 0.03,
    "ramp_turnover_day": 0.20,      # turnover limits used ONLY while the book is ramping in (idle cash above RAMP_MIN_IDLE); the owner may raise them
    "ramp_turnover_week": 0.40,
    "dust_day": 0.03,               # exempt "dust sweep" of leftover cash into GOLDBEES: at most this much a day ...
    "dust_week": 0.06,              # ... and this much over the rolling 5 sessions (booked in the ledger, bounded)
    "cov_max_age_days": 10,         # a stored last-good covariance is used for the vol guard for this many calendar days
    "fallback_rho": 1.0,            # correlation assumed when the guard has only per-asset volatilities (1.0 = the conservative bound)
    "max_port_vol": 0.30,
    "underwater_port_vol": 0.24,
    "brake_mode": "rotate_to_gold",  # level 1: rotate to the GOLDBEES sink (no cash); cash = the older multiply-to-cash behaviour
    "protect_gold_min": 0.30,
    "carry_days": 1,
    "provisional_conf_mult": 0.8,
    "desks": {"fanout": "compact", "quorum": 2},
}

# NOTE: no "cash" key. Weights are SHARES of the invested book (they sum to 1.00 across the three symbols); the engine scales them
# to its invested level. Cash is never a choice and a proposal that names it is refused.
ALLOWED_KEYS = frozenset({"for_date", "written", "created", "expiry", "expires", "by", "source", "phase", "weights",
                          "confidence", "horizon_days", "scenarios", "rationale", "invalidate", "desks", "default_at_write",
                          "prices_at_update", "budget", "note"})
_RE_INVALIDATE = re.compile(r"^close_below:(GOLDBEES|SILVERBEES|HINDCOPPER):(\d+(?:\.\d+)?)$")
_DIRS = {"up", "flat", "down"}


# ---------------------------------------------------------------- small helpers
def _num(x) -> float | None:
    """A finite float, or None (bools, strings, NaN and inf are not numbers here)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        v = float(x)
    except OverflowError:       # an integer too large for a float (e.g. 10**400) is not a number here
        return None
    return v if math.isfinite(v) else None


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _w(x) -> float:
    """A usable weight: finite, never negative, never above 1; anything else is 0."""
    v = _num(x)
    return 0.0 if v is None else _clip(v, 0.0, 1.0)


def _dt(s, tz=None) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo or tz is None else d.replace(tzinfo=tz)


def _d(s) -> date | None:
    if not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _bound(raw: Mapping | None, key: str, default: float, lo: float, hi: float) -> float:
    v = _num((raw or {}).get(key))
    return default if v is None else _clip(v, lo, hi)


def _caps(raw: Mapping | None, key: str) -> dict[str, float]:
    got = (raw or {}).get(key)
    got = got if isinstance(got, Mapping) else {}
    return {s: _bound(got, s, DEFAULTS[key][s], 0.0, 1.0) for s in SYMS}


def settings(p: Mapping | None = None) -> dict:
    """The `allocation:` block of the params (full params dict or the block itself) with defaults and bounds applied.
    Garbage values fall back to the default; the result always has every key."""
    raw = p.get("allocation") if isinstance(p, Mapping) and isinstance(p.get("allocation"), Mapping) else (
        p if isinstance(p, Mapping) and "invested_target" in p else None)
    # ONE switch, one source of truth: allocation.phase. When it is present it alone decides (the legacy `shadow` key is ignored):
    #   off / shadow  the engine's own rules trade (v3 only logs, and only under strategy.mode v3_alloc; off does not even log)
    #   live_default  D5-FI trades fully invested, the AI advice is only logged (trust 0)
    #   live          the AI tilt trades too, at ai_trust
    # An unknown phase value is read as shadow (safe). Only when phase is ABSENT does the legacy `shadow: false` (with strategy.mode
    # v3_alloc) mean "v3 trades", read as live_default.
    has_phase = raw is not None and raw.get("phase") is not None
    ph = str(raw["phase"]).strip().lower() if has_phase else ""
    if has_phase:
        ph = ph if ph in PHASES else "shadow"
        trading = ph in ("live_default", "live")
    else:
        trading = (raw or {}).get("shadow", True) is False
        ph = "live_default" if trading else "shadow"
    out = {
        "shadow": not trading,                                              # anything but an explicit switch stays shadow
        "phase": ph,
        "invested_target": _bound(raw, "invested_target", DEFAULTS["invested_target"], 0.0, 1.0),
        "ai_trust": _bound(raw, "ai_trust", DEFAULTS["ai_trust"], 0.0, 1.0),
        "ai_caps": _caps(raw, "ai_caps"),
        "off_trend_caps": _caps(raw, "off_trend_caps"),
        "max_tilt": _bound(raw, "max_tilt", DEFAULTS["max_tilt"], 0.0, 1.0),
        "max_turnover_day": _bound(raw, "max_turnover_day", DEFAULTS["max_turnover_day"], 0.0, 1.0),
        "max_turnover_week": _bound(raw, "max_turnover_week", DEFAULTS["max_turnover_week"], 0.0, 5.0),
        "rebalance_threshold": _bound(raw, "rebalance_threshold", DEFAULTS["rebalance_threshold"], 0.0, 0.5),
        "max_port_vol": _bound(raw, "max_port_vol", DEFAULTS["max_port_vol"], 0.01, 5.0),
        "brake_mode": "cash" if str((raw or {}).get("brake_mode", "")).lower() == "cash" else "rotate_to_gold",
        "protect_gold_min": _bound(raw, "protect_gold_min", DEFAULTS["protect_gold_min"], 0.0, 1.0),
        "carry_days": int(_bound(raw, "carry_days", DEFAULTS["carry_days"], 0, 5)),
        "provisional_conf_mult": _bound(raw, "provisional_conf_mult", DEFAULTS["provisional_conf_mult"], 0.0, 1.0),
        "desks": dict(DEFAULTS["desks"], **((raw or {}).get("desks") if isinstance((raw or {}).get("desks"), Mapping) else {})),
    }
    for k, hi in (("ramp_turnover_day", 1.0), ("ramp_turnover_week", 5.0), ("dust_day", 0.5), ("dust_week", 1.0), ("fallback_rho", 1.0)):
        out[k] = _bound(raw, k, DEFAULTS[k], 0.0, hi)
    out["cov_max_age_days"] = int(_bound(raw, "cov_max_age_days", DEFAULTS["cov_max_age_days"], 0, 60))
    out["invested_max"] = _bound(raw, "invested_max", DEFAULTS["invested_max"], out["invested_target"], 1.0)
    uw = (raw or {}).get("underwater_port_vol", DEFAULTS["underwater_port_vol"])
    out["underwater_port_vol"] = None if uw is None else _bound(raw, "underwater_port_vol", DEFAULTS["underwater_port_vol"], 0.01, 5.0)
    return out


# ---------------------------------------------------------------- proposal schema (state/allocation.json)
@dataclass
class Advice:
    weights: dict[str, float] | None     # None = "hold-default" (an explicit no-view)
    confidence: float                    # EFFECTIVE confidence (provisional and carry multipliers already applied)
    raw_confidence: float
    phase: str                           # "provisional" | "final"
    source: str                          # "final" | "provisional" | "carry" | "hold_default"
    for_date: str
    written: str
    by: str
    invalidate: list[tuple[str, float]] = field(default_factory=list)
    expiry: str | None = None


def validate_proposal(obj: Any, cfg: Mapping | None = None) -> tuple[dict | None, str | None]:
    """Structural check of an allocation proposal. Returns (normalised dict, None) or (None, reason). Never raises.
    No clock here (see resolve_advice). Whitelist of keys: size / qty / order / leverage / extra symbols cannot even be expressed."""
    try:
        return _validate(obj, cfg or settings(None))
    except Exception:       # noqa: BLE001 - garbage in a hand-edited file (huge integers, odd types) is a refusal, never an exception
        return None, "could not be evaluated (unexpected value types)"


def _validate(obj: Any, cfg: Mapping) -> tuple[dict | None, str | None]:
    if not isinstance(obj, dict):
        return None, "not a JSON object"
    try:
        if len(json.dumps(obj)) > MAX_FILE_BYTES:
            return None, "file too large"
    except (TypeError, ValueError):
        return None, "not serialisable"
    bad = sorted(str(k) for k in obj if k not in ALLOWED_KEYS)
    if bad:
        return None, f"unknown key(s): {', '.join(bad)[:80]}"
    fd = _d(obj.get("for_date"))
    if fd is None:
        return None, "for_date is not an ISO date"
    wr = obj.get("written", obj.get("created"))
    if _dt(wr) is None:
        return None, "written/created is not an ISO timestamp"
    exp = obj.get("expiry", obj.get("expires"))
    if exp is not None and _dt(exp) is None:
        return None, "expiry is not an ISO timestamp"
    by = obj.get("by", obj.get("source", ""))
    if not isinstance(by, str) or len(by) > 60:
        return None, "source/by is not a short string"
    phase = obj.get("phase", "final")
    if phase not in ("provisional", "final"):
        return None, "phase must be provisional or final"
    conf = _num(obj.get("confidence"))
    if conf is None or not 0.0 <= conf <= 1.0:
        return None, "confidence is not a number in [0, 1]"
    hd = obj.get("horizon_days")
    if hd is not None and (_num(hd) is None or not 1 <= _num(hd) <= 30):
        return None, "horizon_days outside 1..30"
    if "cash" in obj:
        return None, "unknown key(s): cash (weights are shares of the invested book, cash is not a choice)"
    weights = obj.get("weights")
    wnorm: dict[str, float] | str
    if weights == "default":
        wnorm = "default"
    elif isinstance(weights, dict):
        extra = sorted(str(k) for k in weights if k not in SYMS)
        if extra:
            return None, f"weights name a symbol outside the three: {', '.join(extra)[:60]}"
        if set(weights) != set(SYMS):
            return None, "weights must name all three symbols"
        wnorm = {}
        for s in SYMS:
            v = _num(weights[s])
            if v is None or v < 0.0 or v > 1.0:
                return None, f"weight of {s} is not a number in [0, 1]"
            wnorm[s] = float(v)
        tot = sum(wnorm.values())
        if tot > 1.0 + 0.02:
            return None, f"weights sum to {tot:.3f}: shares must sum to 1.00 (more than 100 percent is leverage)"
        if tot < 1.0 - 0.02:
            return None, f"weights sum to {tot:.3f}: shares must sum to 1.00 (a shortfall would be cash, which is not a choice)"
        wnorm = {s: v / tot for s, v in wnorm.items()}            # exactly 1.00; the engine scales it to the invested level
    else:
        return None, "weights is neither an object nor \"default\""
    sc = obj.get("scenarios")
    if wnorm != "default" or sc is not None:
        if not isinstance(sc, list) or not 2 <= len(sc) <= 4:
            return None, "scenarios must be a list of 2 to 4"
        tp = 0.0
        for x in sc:
            if not isinstance(x, dict) or not isinstance(x.get("name"), str) or len(x["name"]) > 60:
                return None, "scenario without a short name"
            pv = _num(x.get("p"))
            if pv is None or not 0.0 <= pv <= 1.0:
                return None, "scenario p is not in [0, 1]"
            tp += pv
            for m in ("GOLD", "SILVER", "COPPER"):
                if m in x and not (isinstance(x[m], str) and x[m] in _DIRS):
                    return None, f"scenario direction for {m} is not up, flat or down"
            if "why" in x and (not isinstance(x["why"], str) or len(x["why"]) > 240):
                return None, "scenario why is too long"
        if abs(tp - 1.0) > 0.02:
            return None, f"scenario probabilities sum to {tp:.2f}"
    rat = obj.get("rationale")
    if rat is not None:
        if not isinstance(rat, dict) or any(k not in ("GOLD", "SILVER", "COPPER") or not isinstance(v, str) or len(v) > 240
                                            for k, v in rat.items()):
            return None, "rationale must map GOLD/SILVER/COPPER to short strings"
    inv = obj.get("invalidate", [])
    if not isinstance(inv, list) or len(inv) > 8 or any(not isinstance(x, str) or not _RE_INVALIDATE.match(x) for x in inv):
        return None, "invalidate must be up to 8 entries of the form close_below:SYMBOL:price"
    for k in ("desks", "default_at_write", "prices_at_update"):
        if k in obj and not isinstance(obj[k], dict):
            return None, f"{k} must be an object"
    out = {"for_date": fd.isoformat(), "written": str(wr), "by": by, "phase": phase, "weights": wnorm, "confidence": float(conf),
           "invalidate": list(inv), "expiry": str(exp) if exp is not None else None}
    return out, None


def age_hours(wr: datetime, now: datetime) -> float:
    """Hours from `wr` to `now`, not counting whole Saturdays and Sundays in between (Friday 14:50 to Monday 15:10 is 24 hours)."""
    if wr.tzinfo is None and now.tzinfo is not None:
        wr = wr.replace(tzinfo=now.tzinfo)
    h = (now - wr).total_seconds() / 3600.0
    d = wr.date() + timedelta(days=1)
    while d < now.date():
        if d.weekday() >= 5:
            h -= 24.0
        d += timedelta(days=1)
    return h


def default_tdays(d0: date, d1: date) -> int:
    """Weekdays in (d0, d1]: the default 'trading days between' (the engine passes the NSE calendar instead)."""
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


def resolve_advice(obj: Any, now: datetime, cfg: Mapping, tdays: Callable[[date, date], int] | None = None
                   ) -> tuple[Advice | None, list[dict]]:
    """The advice the engine may use at `now`, plus plain notes (clamp-record style) saying why not.
      - missing / invalid / from the future / expired / for a later day          -> (None, note): the default runs
      - for today: FINAL x1, PROVISIONAL x provisional_conf_mult (confidence)
      - for an earlier day: only a valid FINAL within `carry_days` trading days, carried at HALF confidence
      - `weights: "default"` is an explicit no-view (confidence 0)"""
    tdays = tdays or default_tdays
    if obj is None:
        return None, [{"rule": "no_proposal", "sym": "ALL", "why": "no allocation proposal"}]
    clean, why = validate_proposal(obj, cfg)
    if clean is None:
        return None, [{"rule": "invalid_proposal", "sym": "ALL", "why": why}]
    tz = now.tzinfo
    wr, today = _dt(clean["written"], tz), now.date()
    fd = date.fromisoformat(clean["for_date"])
    if wr is None or wr - now > FUTURE_SLACK:
        return None, [{"rule": "invalid_proposal", "sym": "ALL", "why": "written is in the future"}]
    if wr.date() > fd:
        return None, [{"rule": "invalid_proposal", "sym": "ALL", "why": "written after its for_date"}]
    if age_hours(wr, now) > MAX_WRITTEN_AGE_H:
        return None, [{"rule": "stale_proposal", "sym": "ALL",
                       "why": f"written {age_hours(wr, now):.0f} hours ago (more than {MAX_WRITTEN_AGE_H:.0f}): the default runs"}]
    ex = _dt(clean["expiry"], tz) if clean["expiry"] else None
    if ex is not None and ex < now:
        return None, [{"rule": "stale_proposal", "sym": "ALL", "why": f"expired at {clean['expiry']}"}]
    conf = clean["confidence"]
    weights = clean["weights"] if clean["weights"] != "default" else None
    inv = [(m.group(1), float(m.group(2))) for m in (_RE_INVALIDATE.match(x) for x in clean["invalidate"])]
    notes: list[dict] = []
    if fd > today:
        return None, [{"rule": "stale_proposal", "sym": "ALL", "why": f"for {clean['for_date']}, not today"}]
    if fd == today:
        source = clean["phase"]
        if clean["phase"] == "provisional":
            conf *= cfg["provisional_conf_mult"]
            notes.append({"rule": "provisional", "sym": "ALL", "why": f"provisional: confidence x {cfg['provisional_conf_mult']:g}"})
    else:
        age = tdays(fd, today)
        if clean["phase"] != "final" or age > cfg["carry_days"]:
            return None, [{"rule": "stale_proposal", "sym": "ALL", "why": f"{clean['phase']} proposal for {clean['for_date']} "
                                                                         f"({age} trading day(s) old): the default runs"}]
        source, conf = "carry", conf * 0.5
        notes.append({"rule": "carry_yesterday", "sym": "ALL", "why": f"FINAL of {clean['for_date']} carried at half strength"})
    if weights is None:
        source, conf = "hold_default", 0.0
    return Advice(weights, conf, clean["confidence"], clean["phase"], source, clean["for_date"], clean["written"], clean["by"],
                  inv, clean["expiry"]), notes


def load_proposal(path: Path | str, now: datetime, cfg: Mapping, tdays: Callable[[date, date], int] | None = None
                  ) -> tuple[Advice | None, list[dict]]:
    """Read state/allocation.json (the only I/O in this module) and resolve it. A missing file is a plain 'no proposal';
    an unreadable, oversized or malformed one is an `invalid_proposal` note. Never raises."""
    p = Path(path)
    try:
        if not p.exists():
            return resolve_advice(None, now, cfg, tdays)
        if p.stat().st_size > MAX_FILE_BYTES:
            return None, [{"rule": "invalid_proposal", "sym": "ALL", "why": "file too large"}]
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, [{"rule": "invalid_proposal", "sym": "ALL", "why": "unreadable file"}]
    try:
        return resolve_advice(obj, now, cfg, tdays)
    except Exception:       # noqa: BLE001 - a proposal problem is never an engine problem
        return None, [{"rule": "invalid_proposal", "sym": "ALL", "why": "could not be evaluated"}]


def invalidated_symbols(advice: Advice | None, prices: Mapping[str, float | None]) -> list[str]:
    """Symbols whose `close_below:SYM:level` condition is hit by the current (provisional close) price."""
    if not advice:
        return []
    out = []
    for sym, level in advice.invalidate:
        px = _num(prices.get(sym))
        if px is not None and px < level and sym not in out:
            out.append(sym)
    return out


_PASSTHROUGH = ("horizon_days", "scenarios", "rationale", "desks", "default_at_write", "prices_at_update", "budget", "note")


def sanitize_allocation(obj: Any, cfg: Mapping | None = None, now: datetime | None = None, *, for_write: bool = False
                        ) -> tuple[dict | None, list[str]]:
    """The one public filter for state/allocation.json. Pure: no I/O, no clock unless `now` is given. Returns
    (clean, []) with the object the tool may write (only whitelisted keys; weights are shares rounded to 4 places that sum to
    exactly 1.00, or the string "default"; confidence a float) or (None, [plain reasons]) when it is refused.

    The structural rules are `validate_proposal` (what the engine applies on every read): the three symbols only, no cash key, no
    size / qty / order / leverage key, each weight in [0, 1], shares summing to 1.00 +-0.02, confidence in [0, 1], 2 to 4 scenarios
    whose p sum to 1 +-0.02, invalidate entries of the form close_below:SYMBOL:price. With for_write=True and `now` (an IST-aware
    datetime) it also refuses: written in the future, for_date in the past or more than MAX_ADVANCE_DAYS ahead, a FINAL for today
    at or after 15:05, text that memsafe refuses (instructions, URLs, over 240 characters) and a rationale without a number.
    `weights: "default"` (hold-default) is valid with any confidence; it is always stored with confidence 0."""
    try:
        return _sanitize(obj, cfg or settings(None), now, for_write)
    except Exception:       # noqa: BLE001 - documented: this filter never raises
        return None, ["could not be evaluated (unexpected value types)"]


def _sanitize(obj: Any, cfg: Mapping, now: datetime | None, for_write: bool) -> tuple[dict | None, list[str]]:
    clean, why = validate_proposal(obj, cfg)
    if clean is None:
        return None, [why or "invalid proposal"]
    out = {"for_date": clean["for_date"], "written": clean["written"], "by": clean["by"], "phase": clean["phase"]}
    wts = clean["weights"]
    if wts == "default":
        out["weights"], out["confidence"] = "default", 0.0
    else:
        r = {s: round(wts[s], 4) for s in SYMS}
        r[SINK] = round(r[SINK] + (1.0 - sum(r.values())), 4)       # exactly 1.00 after rounding (the sink absorbs the 4th decimal)
        out["weights"], out["confidence"] = r, round(clean["confidence"], 4)
    out["invalidate"] = list(clean["invalidate"])
    if clean["expiry"] is not None:
        out["expiry"] = clean["expiry"]
    for k in _PASSTHROUGH:
        if k in obj:
            out[k] = obj[k]
    reasons: list[str] = []
    if for_write:
        reasons += _write_rules(out, now)
    return (None, reasons) if reasons else (out, [])


def _write_rules(out: dict, now: datetime | None) -> list[str]:
    """Extra refusals when a routine WRITES a proposal (the engine never applies these: it only uses the structural rules)."""
    why: list[str] = []
    if now is not None:
        wr, fd, today = _dt(out["written"], now.tzinfo), date.fromisoformat(out["for_date"]), now.date()
        if wr is None or wr - now > FUTURE_SLACK:
            why.append("written is in the future")
        elif age_hours(wr, now) > MAX_WRITTEN_AGE_H:
            why.append(f"written more than {MAX_WRITTEN_AGE_H:.0f} hours ago")
        if fd < today:
            why.append(f"for_date {out['for_date']} is in the past")
        elif (fd - today).days > MAX_ADVANCE_DAYS:
            why.append(f"for_date {out['for_date']} is more than {MAX_ADVANCE_DAYS} days ahead")
        if out["phase"] == "final" and fd == today and now.timetz().replace(tzinfo=None) >= FINAL_CUTOFF:
            why.append("a FINAL proposal for today is refused after 15:05 IST (the check runs at 15:10)")
    try:
        from . import memsafe
    except ImportError:         # pragma: no cover
        return why
    texts = [("by", out["by"])] + ([("note", out["note"])] if "note" in out else [])
    for i, sc in enumerate(out.get("scenarios") or []):
        texts += [(f"scenario {i + 1} name", sc.get("name"))] + ([(f"scenario {i + 1} why", sc["why"])] if "why" in sc else [])
    for m, txt in (out.get("rationale") or {}).items():
        texts.append((f"rationale {m}", txt))
        if not re.search(r"\d", txt):
            why.append(f"rationale {m} needs a number (a level, a percentage or a date)")
    for label, txt in texts:
        try:
            memsafe.check_text(txt, "candidate")
        except memsafe.Refused as e:
            why.append(f"{label}: {e}")
    return why


# ---------------------------------------------------------------- default D5-FI
def default_d5fi(infos: Mapping[str, tuple[bool, float | None]], st: Mapping, cfg: Mapping,
                    ineligible: Collection[str] = ()) -> tuple[dict[str, float], dict]:
    """D5-FI: trend.target_weights_ex (v2 trend + vol sizing, stop moves to gold, no brake) with min_invested raised to
    `invested_max`, so the residual goes to GOLDBEES. infos: {sym: (trend on, sigma)}; a symbol without data is 0.
    Returns ({sym: weight} for all three symbols, trend detail). The brake is NOT applied here (clamp() does it)."""
    known = {s: infos[s] for s in SYMS if s in infos}
    st2 = dict(st, min_invested=cfg["invested_max"], floor_symbol=SINK)
    # owner decision D3: the v3 caps (gold 1.00, silver 0.50, copper 0.25) bind the DEFAULT too, not only AI proposals. A stricter
    # strategy cap wins. strategy.symbols is copied: the v2 caps under strategy.mode v2_trend are never touched.
    syms_cfg = {s: dict(v) for s, v in (st.get("symbols") or {}).items()}
    for s in SYMS:
        if s in syms_cfg:
            syms_cfg[s]["cap"] = min(float(syms_cfg[s].get("cap", 1.0)), float(cfg["ai_caps"][s]))
    st2["symbols"] = syms_cfg
    if SINK not in known:       # no gold data: no floor can be computed
        return {s: 0.0 for s in SYMS}, {}
    w, _scale, detail = trend.target_weights_ex(known, st2, 1.0, tuple(ineligible))
    out = {s: _w(w.get(s)) for s in SYMS}
    tot = sum(out.values())
    if tot > cfg["invested_max"] + EPS:
        out = {s: v * cfg["invested_max"] / tot for s, v in out.items()}
    return out, detail


default_targets = default_d5fi          # the name the first engine integration used; kept as an alias


# ---------------------------------------------------------------- portfolio volatility
def ewma_cov(series: Mapping[str, Mapping[str, float]], span: int = 60, min_obs: int = 20) -> dict[str, dict[str, float]] | None:
    """Annualised EWMA covariance of daily returns. series: {sym: {iso date: close}}; only dates every symbol has are used.
    None when there are fewer than min_obs returns or any close is bad."""
    syms = [s for s in SYMS if s in series]
    if not syms:
        return None
    dates = sorted(set.intersection(*(set(series[s]) for s in syms)))
    if len(dates) < min_obs + 1:
        return None
    rets = []
    for s in syms:
        c = [series[s][d] for d in dates]
        if any(_num(x) is None or x <= 0 for x in c):
            return None
        rets.append([c[i] / c[i - 1] - 1.0 for i in range(1, len(c))])
    n = len(rets[0])
    a = 2.0 / (span + 1.0)
    wt = [(1 - a) ** (n - 1 - i) for i in range(n)]
    tw = sum(wt)
    wt = [x / tw for x in wt]
    mean = [sum(w * r for w, r in zip(wt, rs)) for rs in rets]
    cov = {}
    for i, si in enumerate(syms):
        cov[si] = {}
        for j, sj in enumerate(syms):
            cov[si][sj] = 252.0 * sum(w * (ri - mean[i]) * (rj - mean[j]) for w, ri, rj in zip(wt, rets[i], rets[j]))
    return cov


def port_vol(w: Mapping[str, float], cov: Mapping[str, Mapping[str, float]] | None) -> float | None:
    """Annualised volatility of the weights, or None when the covariance is unusable."""
    if not cov:
        return None
    var = 0.0
    for a in SYMS:
        for b in SYMS:
            wa, wb = w.get(a, 0.0), w.get(b, 0.0)
            if wa == 0.0 or wb == 0.0:
                continue
            c = _num((cov.get(a) or {}).get(b))
            if c is None:
                return None
            var += wa * wb * c
    return math.sqrt(max(var, 0.0))


def _sigma(cov, s) -> float:
    v = _num((cov.get(s) or {}).get(s)) if cov else None
    return math.sqrt(v) if v is not None and v > 0 else 0.0


def fallback_cov(sigmas: Mapping[str, float] | None, rho: float = 1.0) -> dict[str, dict[str, float]]:
    """A stand-in covariance from per-asset annual volatilities (a missing or bad one falls back to FALLBACK_VOL) and one
    correlation `rho` (1.0 = the conservative upper bound: volatilities simply add)."""
    sg = {}
    for s in SYMS:
        v = _num((sigmas or {}).get(s))
        sg[s] = v if v is not None and v > 0 else FALLBACK_VOL[s]
    r = _clip(_num(rho) if _num(rho) is not None else 1.0, 0.0, 1.0)
    return {a: {b: sg[a] * sg[b] * (1.0 if a == b else r) for b in SYMS} for a in SYMS}


def vol_guard(w: Mapping[str, float], cov, limit: float, gold_room: float, movable: Collection[str] = SYMS,
              fixed: Mapping[str, float] | None = None) -> dict[str, float]:
    """Best effort: while the portfolio volatility is over `limit`, move weight from the highest-volatility non-gold symbol
    to GOLDBEES (never to cash, never above `gold_room` of extra gold), only when the move lowers the volatility."""
    w = dict(w)
    fx = {s: _w((fixed or {}).get(s)) for s in SYMS}      # weights that cannot move (no data this check) still count in the volatility
    pv = lambda x: port_vol({s: x.get(s, 0.0) + fx[s] for s in SYMS}, cov)      # noqa: E731
    v0 = pv(w)
    if v0 is None or v0 <= limit + 1e-12 or SINK not in movable:
        return w
    order = sorted((s for s in SYMS if s != SINK and s in movable and w[s] > EPS and _sigma(cov, s) > _sigma(cov, SINK)),
                   key=lambda s: -_sigma(cov, s))
    room = gold_room
    for s in order:
        cur = pv(w)
        if cur is None or cur <= limit + 1e-12 or room <= EPS:
            break
        full = min(w[s], room)

        def moved(x, s=s):
            m = dict(w)
            m[s] -= x
            m[SINK] += x
            return m
        vf = pv(moved(full))
        if vf is None or vf >= cur - 1e-15:
            continue
        if vf > limit:
            w, room = moved(full), room - full
            continue
        lo, hi = 0.0, full
        for _ in range(60):
            mid = (lo + hi) / 2
            vm = pv(moved(mid))
            if vm is not None and vm > limit:
                lo = mid
            else:
                hi = mid
        w = moved(hi)
        break
    return w


def vol_cut(w: Mapping[str, float], cov, limit: float, movable: Collection[str], fixed: Mapping[str, float] | None = None
            ) -> dict[str, float]:
    """Reduce the highest-volatility movable symbols (weight goes to cash) until the portfolio volatility is at or under `limit`,
    or until none of them is left. Only ever lowers weights, so the volatility can only fall."""
    w = dict(w)
    fx = {s: _w((fixed or {}).get(s)) for s in SYMS}
    pv = lambda x: port_vol({s: x.get(s, 0.0) + fx[s] for s in SYMS}, cov)      # noqa: E731
    for s in sorted((x for x in movable if x in w and w[x] > EPS), key=lambda x: -_sigma(cov, x)):
        cur = pv(w)
        if cur is None or cur <= limit + 1e-12:
            break
        zero = dict(w, **{s: 0.0})
        vz = pv(zero)
        if vz is None or vz >= cur - 1e-15:           # cutting this one would not lower the volatility (a hedge): leave it
            continue
        if vz > limit:
            w = zero
            continue
        lo, hi = 0.0, 1.0                       # fraction of the weight kept: hi is under the limit, lo is over
        lo_w = w[s]
        for _ in range(60):
            mid = (lo + hi) / 2
            vm = pv(dict(w, **{s: lo_w * mid}))
            if vm is not None and vm > limit:
                hi = mid
            else:
                lo = mid
        cand = dict(w, **{s: lo_w * lo})
        vc = pv(cand)
        if vc is not None and vc < cur:
            w = cand
        break
    return w


# ---------------------------------------------------------------- turnover ledger
def _back_trading_days(d: date, n: int, prev_td: Callable[[date], date] | None) -> date:
    for _ in range(n):
        if prev_td:
            d = prev_td(d)
        else:
            d -= timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
    return d


def window_total(ledger: Mapping[str, float] | None, today: date, prev_td: Callable[[date], date] | None = None) -> float:
    """Sum of a {iso date: amount} ledger over today and the 4 sessions before it (bad entries are ignored)."""
    cutoff = _back_trading_days(today, 4, prev_td)
    tot = 0.0
    for k, v in (ledger or {}).items():
        d, x = _d(k), _num(v)
        if d is not None and x is not None and x >= 0 and cutoff <= d <= today:
            tot += x
    return tot


def turnover_left(ledger: Mapping[str, float] | None, today: date, cfg: Mapping,
                  prev_td: Callable[[date], date] | None = None) -> tuple[float, float]:
    """(left today, left this rolling 5-session window) of one-way turnover. ledger: {iso date: discretionary one-way
    turnover}. The window is today and the 4 sessions before it."""
    led = {}
    for k, v in (ledger or {}).items():
        d, x = _d(k), _num(v)
        if d is not None and x is not None and x >= 0:
            led[d] = x
    cutoff = _back_trading_days(today, 4, prev_td)
    week = sum(v for d, v in led.items() if cutoff <= d <= today)
    return (max(0.0, cfg["max_turnover_day"] - led.get(today, 0.0)), max(0.0, cfg["max_turnover_week"] - week))


def effective_credit(stored, cash_weight, level: int = 0, halted: bool = False) -> float:
    """The re-invest credit that may be used now: what is stored (a share of the book from today's risk sells), never more than the
    cash actually held, and zero at brake level 2 and under the account stop. Pure; garbage in means 0."""
    if halted or (_num(level) or 0) >= 2:
        return 0.0
    c, cash = _num(stored), _num(cash_weight)
    if c is None or cash is None:
        return 0.0
    return max(0.0, min(c, cash, 1.0))


# ---------------------------------------------------------------- the clamp pipeline
@dataclass
class AllocResult:
    default: dict[str, float]
    proposal: dict[str, float] | None      # the advice weights as read (None = no usable advice)
    confidence: float                      # effective confidence used
    k: float                               # tilt strength
    advice_source: str                     # "final" | "provisional" | "carry" | "hold_default" | "none"
    stages: dict[str, dict[str, float]]    # after_tilt, after_caps, after_fill, after_vol, after_brake, after_turnover, final
    final: dict[str, float]
    caps: dict[str, float]                 # the per-symbol ceilings used (max(cap, default))
    clamps: list[dict]                     # [{rule, sym, from, to, why?}] in execution order
    baseline: dict[str, float]             # risk baseline: the default after zeroing, fill, vol guard and brake (no AI)
    exempt_sell: float                     # sum of sells never limited (risk sells), one-way weight
    discretionary_turnover: float          # one-way turnover (the larger of buys and sells) the limiter counted; the ledger adds it
    turnover_budget: float                 # min(day left, week left) when the limiter ran
    brake_level: int
    halted: bool
    vol_before: float | None = None
    vol_after: float | None = None
    credit_used: float = 0.0               # the part of `credit` this result spent (exempt GOLDBEES buys); the caller subtracts it
    credit: float = 0.0                    # risk-sell proceeds (share of the book) that were available to exempt gold buys
    exempt_buy: float = 0.0                # buys the limiter did not count (funded by risk sells today or by the credit)
    cov_source: str = "live"               # live | last_good | fallback (the vol guard's input)

    def as_record(self) -> dict:
        r4 = lambda m: {s: round(float(m[s]), 4) for s in SYMS} if m is not None else None
        return {"default": r4(self.default), "proposal": r4(self.proposal), "confidence": round(self.confidence, 4),
                "k": round(self.k, 4), "advice": self.advice_source, "brake_level": self.brake_level, "halted": self.halted,
                **{k: r4(v) for k, v in self.stages.items()}, "final": r4(self.final), "caps": r4(self.caps),
                "turnover": {"discretionary": round(self.discretionary_turnover, 4), "exempt_sells": round(self.exempt_sell, 4),
                             "budget": None if not math.isfinite(self.turnover_budget) else round(self.turnover_budget, 4)},
                "vol": {"before": None if self.vol_before is None else round(self.vol_before, 4),
                        "after": None if self.vol_after is None else round(self.vol_after, 4), "cov": self.cov_source},
                "credit": {"available": round(self.credit, 4), "exempt_buy": round(self.exempt_buy, 4),
                           "used": round(self.credit_used, 4)},
                "clamps": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in c.items()} for c in self.clamps]}


def _budget(x) -> float:
    """A turnover allowance: a finite number (negative counts as 0) or +inf for 'no limit'; anything else is 0."""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            if x == math.inf:
                return math.inf
            if math.isfinite(x):
                return max(0.0, float(x))
        except OverflowError:       # an integer too large for a float: positive = no limit, negative = nothing
            return math.inf if x > 0 else 0.0
    return 0.0


def tilt_strength(ai_trust: float, confidence: float) -> float:
    """k = ai_trust x clip((confidence - 0.30) / 0.50, 0, 1): confidence 0.3 or less means no tilt, 0.8 or more the full tilt."""
    t, c = _num(ai_trust), _num(confidence)
    if t is None or c is None:
        return 0.0
    return _clip(t, 0.0, 1.0) * _clip((c - 0.30) / 0.50, 0.0, 1.0)


def _fill(t: dict, cap: Mapping[str, float], target: float, syms: Sequence[str]) -> dict:
    """Move the total to `target`: a shortfall goes to GOLDBEES first, then pro rata to symbols with room; an excess is taken
    from GOLDBEES first, then pro rata. Never exceeds a cap when adding; whatever cannot be placed stays unallocated."""
    t = dict(t)
    tot = sum(t[s] for s in syms)
    if tot < target - EPS:
        need = target - tot
        if SINK in syms:
            add = min(need, max(0.0, cap[SINK] - t[SINK]))
            t[SINK] += add
            need -= add
        others = [s for s in syms if s != SINK]
        room = {s: max(0.0, cap[s] - t[s]) for s in others}
        R = sum(room.values())
        if need > EPS and R > EPS:
            put = min(need, R)
            for s in others:
                t[s] += put * room[s] / R
    elif tot > target + EPS:
        ex = tot - target
        if SINK in syms:
            cut = min(ex, t[SINK])
            t[SINK] -= cut
            ex -= cut
        others = [s for s in syms if s != SINK]
        R = sum(t[s] for s in others)
        if ex > EPS and R > EPS:
            take = min(ex, R)
            for s in others:
                t[s] -= take * t[s] / R
    return {s: max(0.0, v) for s, v in t.items()}


def _brake(w: Mapping[str, float], syms: Sequence[str], level: int, halted: bool, mode: str, mult: float
           ) -> dict[str, float]:
    """Level 1 (-15 percent): non-gold x mult, the freed weight rotates to GOLDBEES (mode rotate_to_gold, no cash) or every
    weight x mult (mode cash). Level 2 (-25 percent) and the account stop: everything to cash."""
    if halted or level >= 2:
        return {s: 0.0 for s in w}
    if level == 1:
        m = _clip(_num(mult) if _num(mult) is not None else 0.5, 0.0, 1.0)
        if mode == "trim" and SINK in syms:               # gold cannot be bought today: silver / copper are cut, gold is kept, the rest waits in cash
            return {s: (v if s == SINK else v * m) for s, v in w.items()}
        if mode == "cash" or mode == "trim" or SINK not in syms:
            return {s: v * m for s, v in w.items()}
        out = {s: (v if s == SINK else v * m) for s, v in w.items()}
        freed = sum(w[s] - out[s] for s in syms if s != SINK)
        out[SINK] = w[SINK] + freed          # a risk rotation, not an AI choice: the gold ai_cap does not apply, so no cash appears
        return out
    return dict(w)


def _clamp(*, default: Mapping[str, float], advice: Mapping[str, Any] | None, confidence: float, current: Mapping[str, float],
          on: Mapping[str, bool], cfg: Mapping, cov=None, halve: Collection[str] = (), zero: Collection[str] = (),
          ineligible: Collection[str] = (), invalidated: Collection[str] = (), brake_level: int = 0, brake_mult: float = 0.5,
          halted: bool = False, day_left: float = math.inf, week_left: float = math.inf, underwater: bool = False,
          frozen: Mapping[str, float] | None = None, notes: Sequence[Mapping] = (), advice_source: str | None = None,
          credit: float = 0.0, no_add: Collection[str] = (), sigmas: Mapping[str, float] | None = None,
          cov_source: str = "live") -> AllocResult:
    """The pipeline (see the module docstring). Pure and total: any garbage in `advice`, `default`, `current` is repaired
    (NaN / inf / negative / unknown symbols never reach the output) and every change is a line in `clamps`.

    default       D5-FI weights ({sym: w}); advice  raw proposal weights (any mapping) or None
    confidence    effective confidence (provisional / carry multipliers already applied)
    current       current portfolio weights;  on  trend state per symbol (a missing symbol counts as OFF)
    halve         symbols whose OFF-trend cap is halved (alignment DEFENSIVE / DEFENSIVE_SHOCK, or a cautious thesis)
    zero          symbols forced to 0 (HINDCOPPER results window)
    ineligible    symbols waiting to re-confirm after the deep brake: the AI cannot raise them above the default
    invalidated   symbols whose invalidation level was hit: the advice for them is dropped (they go to the default)
    frozen        {sym: weight} symbols with no data this check: held as they are, excluded from every step
    day_left / week_left  one-way turnover still available (turnover_left); risk sells never count against them
    credit        share of the book that is idle cash from today's (or an undeployed earlier) RISK sells: gold buys up to this amount
                  are not counted by the turnover limiter, so risk proceeds are never stranded in cash by the day / week budget
    no_add        symbols that cannot be bought today (stopped out today, routine bias block): their target never rises above the
                  current weight, and the weight they cannot take goes to the other symbols with room
    sigmas        per-asset annual volatilities for the fallback vol guard when no usable covariance exists
    cov_source    where `cov` came from: live, last_good (a stored earlier covariance) or None/fallback (see the vol guard)"""
    clamps: list[dict] = [{"from": None, "to": None, **n} for n in notes]
    frozen = {s: _w(frozen[s]) for s in SYMS if frozen and s in frozen}
    S = [s for s in SYMS if s not in frozen]
    fill_to = max(0.0, cfg["invested_max"] - sum(frozen.values()))
    zero, ineligible, invalidated, halve = set(zero), set(ineligible), set(invalidated), set(halve)

    def log(rule, sym, a, b, why=None):
        e = {"rule": rule, "sym": sym, "from": None if a is None else float(a), "to": None if b is None else float(b)}
        if why:
            e["why"] = why
        clamps.append(e)

    def snap(m):
        return {s: float(m[s]) if s in m else float(frozen.get(s, 0.0)) for s in SYMS}

    # ---- inputs, repaired
    d_in = {s: _w(default.get(s)) for s in S}
    for s in zero:
        if s in d_in and d_in[s] > EPS:
            log("results_window", s, d_in[s], 0.0, "default set to 0 in the results window")
            d_in[s] = 0.0
    tot = sum(d_in.values())
    d = dict(d_in)
    if tot > fill_to + EPS:
        d = {s: v * fill_to / tot for s, v in d_in.items()}
        log("default_scale", "ALL", tot, fill_to, "default total scaled to the invested level")
    c = {s: _w(current.get(s)) for s in S}
    ct = sum(c.values())
    if ct + sum(frozen.values()) > 1.0 + EPS:
        room = max(0.0, 1.0 - sum(frozen.values()))
        c = {s: (v * room / ct if ct > 0 else 0.0) for s, v in c.items()}
    onm = {s: bool(on.get(s, False)) for s in S}
    conf = _num(confidence) or 0.0
    k = tilt_strength(cfg["ai_trust"], conf) if advice is not None else 0.0

    # ---- ceilings: cap bounds the AI, never pushes below the default
    cap: dict[str, float] = {}
    for s in S:
        cp = cfg["ai_caps"][s]
        if not onm[s]:
            off = cfg["off_trend_caps"][s] * (0.5 if s in halve else 1.0)
            if off < cp:
                cp = off
        if s in zero:
            cp = 0.0
        elif s in ineligible:
            cp = 0.0
        cap[s] = max(cp, d[s])

    # ---- step 3: tilt
    t = dict(d)
    if advice is not None:
        # advice weights are shares of the invested book: scale them to the invested level (a no-op for shares that sum to 1.00
        # when invested_max is 1.00); a symbol with a bad value keeps its default
        good = {s: _clip(v, 0.0, 1.0) for s in S for v in [_num(advice.get(s)) if isinstance(advice, Mapping) else None] if v is not None}
        gsum = sum(good.values())
        scale = fill_to / gsum if gsum > EPS and len(good) == len(S) else 1.0
        for s in S:
            av = good[s] * scale if s in good else None
            if av is None:
                log("bad_advice_weight", s, d[s], d[s], "advice weight missing or not a number: default kept")
                continue
            if s in invalidated:
                log("invalidated", s, av, d[s], "invalidation level hit: advice dropped, back to the default")
                continue
            av = _clip(av, 0.0, 1.0)
            raw = av - d[s]
            lim = _clip(raw, -cfg["max_tilt"], cfg["max_tilt"])
            if lim != raw:
                log("max_tilt", s, av, d[s] + lim, f"advice {av:.3f} is more than {cfg['max_tilt']:g} from the default {d[s]:.3f}")
            t[s] = d[s] + k * lim
    after_tilt = snap(t)

    # ---- step 4: caps (and the results window)
    for s in S:
        if s in zero and t[s] > EPS:
            log("results_window", s, t[s], 0.0)
            t[s] = 0.0
        elif t[s] > cap[s] + EPS:
            off_bound = not onm[s] and cap[s] <= cfg["off_trend_caps"][s] * (0.5 if s in halve else 1.0) + EPS
            log("off_trend_cap" if off_bound else "cap", s, t[s], cap[s],
                "trend OFF cap" if off_bound else ("waiting to re-confirm the trend" if s in ineligible else "per-asset cap"))
            t[s] = cap[s]
    # symbols that cannot be bought today never rise above the weight already held (the engine's buy blockers would refuse the buy)
    bar = set(no_add) & set(S)
    for s in sorted(bar):
        if t[s] > c[s] + EPS:
            log("no_add", s, t[s], c[s], "not bought today (stopped out, or the routine bias is block): held at the current weight")
            t[s] = c[s]
    after_caps = snap(t)

    # ---- step 5: fill to the invested level (sink GOLDBEES, then others with room)
    capf = dict(cap)
    for s in zero:
        capf[s] = 0.0
    avoid = bar | (invalidated & set(S))            # first pass: the leftover goes to symbols that may still be bought, and never
    # lifts a symbol other than the GOLDBEES sink above its own default weight (no counter-trend silver / copper buys from a leftover)
    first = {s: (min(capf[s], t[s]) if s in avoid else (capf[s] if s == SINK else min(capf[s], max(d[s], t[s])))) for s in S}
    t_fill = _fill(t, first, fill_to, S)
    if sum(t_fill[s] for s in S) < fill_to - 1e-6 and (avoid - bar):
        t2 = _fill(t_fill, {s: (min(capf[s], t_fill[s]) if s in bar else capf[s]) for s in S}, fill_to, S)
        if sum(t2.values()) > sum(t_fill.values()) + EPS:
            log("fill_last_resort", SINK if SINK in avoid else "ALL", sum(t_fill.values()), sum(t2.values()),
                "no other symbol has room: the leftover goes back to a symbol whose advice was invalidated")
            t_fill = t2
    for s in S:
        if abs(t_fill[s] - t[s]) > EPS:
            log("fill", s, t[s], t_fill[s], "leftover weight to the sink" if s == SINK else "leftover weight to a symbol with room")
    t = t_fill
    after_fill = snap(t)

    # ---- step 6: portfolio-volatility guard
    vlimit = cfg["max_port_vol"]
    if underwater and cfg.get("underwater_port_vol") is not None:
        vlimit = min(vlimit, cfg["underwater_port_vol"])
    # the guard never switches off silently: with no usable covariance (a bad close, mismatched history, too few shared dates) it
    # runs on a conservative stand-in built from per-asset volatilities, and says so in the ALLOC record
    cov_eff, cvsrc = cov, (cov_source or "live") if cov else "fallback"
    if cov_eff is None or port_vol(snap({**t, **frozen}), cov_eff) is None:
        cov_eff, cvsrc = fallback_cov(sigmas, cfg.get("fallback_rho", 1.0)), "fallback"
    if cvsrc == "fallback":
        log("vol_guard_fallback", "ALL", None, None, "covariance unavailable: conservative guard from per-asset volatilities, "
                                                     f"correlation {cfg.get('fallback_rho', 1.0):g}")
    elif cvsrc != "live":
        log("vol_guard_stale_cov", "ALL", None, None, f"covariance unavailable today: the vol guard uses the {cvsrc} covariance")
    v_before = port_vol(snap({**t, **frozen}), cov_eff)
    t_vol = vol_guard({**t, **{s: 0.0 for s in SYMS if s not in t}}, cov_eff, vlimit, max(0.0, capf.get(SINK, 0.0) - t.get(SINK, 0.0)),
                      movable=[x for x in S if x not in bar], fixed=frozen)             # the guard never moves weight INTO a symbol that cannot be bought today
    t_vol = {s: t_vol[s] for s in S}
    for s in S:
        if abs(t_vol[s] - t[s]) > EPS:
            log("vol_guard", s, t[s], t_vol[s], f"portfolio volatility over {vlimit:.2f}: weight moved to GOLDBEES")
    t = t_vol
    # GOLDBEES cannot take weight today (not bought today, no room) and the limit is still broken: reduce the riskiest symbols instead
    # (the weight waits in cash, logged). The limit stays satisfiable whenever the sink alone fits under it.
    vnow = port_vol(snap({**t, **frozen}), cov_eff)
    sink_closed = SINK in bar or SINK in zero or SINK not in S or capf.get(SINK, 0.0) - t.get(SINK, 0.0) <= EPS
    if vnow is not None and vnow > vlimit + 1e-9 and sink_closed:
        t_cut = vol_cut(t, cov_eff, vlimit, [x for x in S if x != SINK], frozen)
        for s in S:
            if abs(t_cut[s] - t[s]) > EPS:
                log("vol_guard_no_sink", s, t[s], t_cut[s], f"portfolio volatility over {vlimit:.2f} and GOLDBEES cannot be bought today: "
                                                             "the riskiest symbols are reduced (the weight waits in cash)")
        t = t_cut
    after_vol = snap(t)
    v_after = port_vol(snap({**t, **frozen}), cov_eff)

    # ---- step 8 (computed before 7, see the docstring): brakes and the account stop
    bmode = cfg["brake_mode"] if (SINK in S and SINK not in zero) else "cash"      # no usable sink: the old multiply-to-cash
    if bmode != "cash" and SINK in bar and brake_level == 1 and not halted:
        bmode = "trim"                                  # rotating into a GOLDBEES that cannot be bought today would show a target the engine never trades
        log("brake_rotation_blocked", SINK, None, None, "drawdown brake level 1: GOLDBEES is not bought today (blocked), so silver / copper are "
                                                        "cut without rotating the weight into gold; it waits in cash")
    f_t = _brake(t, S, brake_level, halted, bmode, brake_mult)
    if halted or brake_level >= 2 or brake_level == 1:
        rule = "account_stop" if halted else ("brake_level2" if brake_level >= 2 else "brake_level1")
        for s in S:
            if abs(f_t[s] - t[s]) > EPS:
                log(rule, s, t[s], f_t[s], {"account_stop": "the account stop is in force: everything to cash",
                                            "brake_level2": "drawdown brake level 2: everything to cash",
                                            "brake_level1": ("drawdown brake level 1: silver/copper cut, weight rotated to GOLDBEES"
                                                             if bmode == "rotate_to_gold" else "drawdown brake level 1: weights cut")}[rule])
    after_brake = snap(f_t)
    # the risk baseline: what the default alone would hold after the same risk rules (no AI)
    base = _fill(d, capf, fill_to, S)
    base = vol_guard({**base, **{s: 0.0 for s in SYMS if s not in base}}, cov_eff, vlimit, max(0.0, capf.get(SINK, 0.0) - base.get(SINK, 0.0)),
                     movable=S, fixed=frozen)
    base = _brake({s: base[s] for s in S}, S, brake_level, halted, bmode, brake_mult)

    # ---- step 7: turnover limiter. Risk sells are exempt: every sell down to the risk baseline (trend exit, brake, results exit,
    # the default's own trims) of SILVERBEES / HINDCOPPER, and the GOLDBEES sells that go to cash (level 2, account stop, level 1 in
    # cash mode, results window). A GOLDBEES sell that only funds other buys is a rebalance and is limited. Buys funded by exempt
    # sales are exempt too (up to the proceeds), so risk proceeds never sit in cash.
    risk_gold = halted or brake_level >= 2 or (brake_level == 1 and bmode == "cash") or SINK in zero
    E, b = {}, {}
    for s in S:
        if c[s] > f_t[s] + EPS and (s != SINK or risk_gold):
            b[s] = min(c[s], max(f_t[s], base[s]))
            E[s] = c[s] - b[s]
        else:
            b[s], E[s] = c[s], 0.0
    sumE = sum(E.values())
    delta = {s: f_t[s] - b[s] for s in S}
    P = sum(v for v in delta.values() if v > 0)
    N = -sum(v for v in delta.values() if v < 0)
    cr = 0.0 if (halted or brake_level >= 2) else _budget(credit)               # may be inf (a huge number): bounded below
    X_E = min(P, sumE)
    e = {s: (delta[s] * X_E / P if delta[s] > 0 and P > EPS else 0.0) for s in S}
    # The credit (cash from today's risk sells) re-funds ONLY the GOLDBEES sink (owner decision). Every other buy, a metal back up to its
    # default included, goes through the normal turnover budget and is booked. It never funds an AI tilt.
    elig = {}
    for s in S:
        rem = delta[s] - e[s]
        if rem > EPS and s == SINK:
            elig[s] = rem
    tot_el = sum(elig.values())
    X_C = min(cr, tot_el) if tot_el > EPS else 0.0
    if X_C > EPS:
        for s, v in elig.items():
            e[s] += v * X_C / tot_el
    X = X_E + X_C
    credit_used = X_C
    cr = min(cr, 1.0)
    D = max(P - X, N)                                       # one-way: the larger of the discretionary buys and sells
    budget = max(0.0, min(_budget(day_left), _budget(week_left)))
    lam = 1.0
    if D > EPS and D > budget + EPS:
        lam = budget / D
    if lam >= 1.0:
        f = dict(f_t)
        used = D
    else:
        f = {s: b[s] + e[s] + lam * (delta[s] - e[s]) for s in S}
        used = lam * D
        for s in S:
            if abs(f[s] - f_t[s]) > EPS:
                log("turnover_limit", s, f_t[s], f[s], f"one-way turnover limited to {budget:.3f} (lambda {lam:.2f})")
    # exempt buys are funded by cash from risk sells; a limited discretionary SELL must never leave the book over-invested
    room_total = max(fill_to, sum(c.values())) + 1e-12
    over = sum(f.values()) - room_total
    if over > EPS:
        buys = {s: max(f[s] - c[s], 0.0) for s in S}
        nb = sum(buys.values())
        if nb > EPS:
            cut = min(over, nb)
            for s in S:
                if buys[s] > 0:
                    f[s] -= cut * buys[s] / nb
                    log("cash_limit", s, f[s] + cut * buys[s] / nb, f[s], "a limited sell leaves no cash for this buy")
    after_turnover = snap(f)

    # ---- step 9: rebalance band (small buys of a held symbol are skipped; to/from zero always trades)
    for s in S:
        if c[s] > EPS and f[s] > c[s] + EPS and f[s] - c[s] <= cfg["rebalance_threshold"] + EPS:
            log("rebalance_band", s, f[s], c[s], f"within {cfg['rebalance_threshold'] * 100:.0f} points of the current weight")
            f[s] = c[s]
    final = snap(f)
    src = advice_source if advice_source is not None else ("none" if advice is None else "final")
    return AllocResult(
        default=snap(d), proposal=({s: _w(advice.get(s)) for s in SYMS} if isinstance(advice, Mapping) else None),
        confidence=conf, k=k, advice_source=src,
        stages={"after_tilt": after_tilt, "after_caps": after_caps, "after_fill": after_fill, "after_vol": after_vol,
                "after_brake": after_brake, "after_turnover": after_turnover},
        final=final, caps=snap(cap), clamps=clamps, baseline=snap(base), exempt_sell=sumE, discretionary_turnover=used,
        turnover_budget=budget, brake_level=int(brake_level), halted=bool(halted), vol_before=v_before, vol_after=v_after,
        credit=cr, exempt_buy=X, credit_used=credit_used, cov_source=cvsrc)


def clamp(*, default: Mapping[str, float], advice: Mapping[str, Any] | None, confidence: float, current: Mapping[str, float],
          on: Mapping[str, bool], cfg: Mapping, **kw) -> AllocResult:
    """The pipeline (see _clamp for the arguments). Total, in three steps:
      1. the normal pipeline;
      2. if it raises, once more with the inputs most likely to be the cause replaced by safe ones: no covariance and no per-asset
         volatilities (the conservative stand-in runs), a valid brake multiplier, a plain integer brake level, no credit. The turnover
         limiter, caps, brakes and the account stop all still apply; a `clamp_retry` note says so;
      3. if that raises too, the engine's repaired default with the turnover limit applied to it (never an unlimited trade of the
         default), zero under the account stop or brake level 2, and a `clamp_failed` note."""
    try:
        return _clamp(default=default, advice=advice, confidence=confidence, current=current, on=on, cfg=cfg, **kw)
    except Exception as e:      # noqa: BLE001
        first = e
    try:
        safe = dict(kw)
        for k in ("cov", "sigmas"):
            safe.pop(k, None)
        safe.pop("cov_source", None)
        bl = _num(safe.get("brake_level"))
        safe["brake_level"] = int(bl) if bl is not None and bl < 10 ** 6 else 0
        bm = _num(safe.get("brake_mult"))
        safe["brake_mult"] = bm if bm is not None and 0.0 <= bm <= 1.0 else 0.5
        safe["credit"] = 0.0
        safe["notes"] = list(safe.get("notes") or []) + [{"rule": "clamp_retry", "sym": "ALL",
                                                          "why": f"the pipeline failed ({type(first).__name__}): re-run without covariance, "
                                                                 "credit or the original brake inputs; every limit still applies"}]
        return _clamp(default=default, advice=advice, confidence=confidence, current=current, on=on, cfg=cfg, **safe)
    except Exception:       # noqa: BLE001
        pass
    stop = bool(kw.get("halted")) or (_num(kw.get("brake_level")) or 0) >= 2
    top = _num(cfg.get("invested_max")) if isinstance(cfg, Mapping) else None
    top = 1.0 if top is None else _clip(top, 0.0, 1.0)
    d = {s: _w(default.get(s)) if isinstance(default, Mapping) else 0.0 for s in SYMS}
    tot = sum(d.values())
    if tot > top and tot > 0:
        d = {s: v * top / tot for s, v in d.items()}
    budget = 0.0
    if stop:
        d = {s: 0.0 for s in SYMS}
        final, used = dict(d), 0.0
    else:
        try:
            c = {s: _w(current.get(s)) for s in SYMS} if isinstance(current, Mapping) else {s: 0.0 for s in SYMS}
        except Exception:       # noqa: BLE001
            c = {s: 0.0 for s in SYMS}
        budget = max(0.0, min(_budget(kw.get("day_left", math.inf)), _budget(kw.get("week_left", math.inf))))
        buys = sum(max(d[s] - c[s], 0.0) for s in SYMS)
        sells = sum(max(c[s] - d[s], 0.0) for s in SYMS)
        D = max(buys, sells)
        lam = 1.0 if D <= EPS or D <= budget else budget / D          # the same one-way limit the pipeline uses
        final = {s: c[s] + lam * (d[s] - c[s]) for s in SYMS}
        used = lam * D
    z = {s: 0.0 for s in SYMS}
    stages = {k: dict(d) for k in ("after_tilt", "after_caps", "after_fill", "after_vol", "after_brake")}
    stages["after_turnover"] = dict(final)
    return AllocResult(default=dict(d), proposal=None, confidence=0.0, k=0.0, advice_source="none", stages=stages, final=dict(final),
                       caps=dict(z), clamps=[{"rule": "clamp_failed", "sym": "ALL", "from": None, "to": None,
                                              "why": f"the allocation pipeline failed ({type(first).__name__}): the default runs, "
                                                     "limited by the turnover budget"}],
                       baseline=dict(d), exempt_sell=0.0, discretionary_turnover=used, turnover_budget=budget,
                       brake_level=int(_num(kw.get("brake_level")) or 0), halted=bool(kw.get("halted")))
