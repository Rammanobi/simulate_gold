"""Long-term trend regime, short-term state and the alignment table (paper trading, pure functions, no I/O).

LTM  = slow trend label per metal from daily closes (UP / FLAT / DOWN, 3-session confirmation).
STM  = what is happening now (UP / FLAT / DOWN / SHOCK_DOWN / SHOCK_UP).
align() compares the two (table in docs: memory design 3.1) and applies overrides that can only LOWER
the bias ceiling and the core multiplier. Anything unknown or stale collapses to the most cautious reading.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

LTM_LABELS = ("UP", "FLAT", "DOWN")
STM_STATES = ("UP", "FLAT", "DOWN", "SHOCK_DOWN", "SHOCK_UP")
BIAS_RANK = {"block": 0, "neutral": 1, "favour": 2}
RANK_BIAS = {v: k for k, v in BIAS_RANK.items()}

CONFIRM_SESSIONS = 3
MIN_BARS = 20
MIN_TESTS = 4
SHOCK_Z = 2.5
SHOCK_GAP_VOL = 1.5
Z5_MIN = 0.5
STALE_SESSIONS = 3

# (ltm, stm) -> (state, core multiplier, tactical, ceiling, default)
ALIGN_TABLE: dict[tuple[str, str], tuple[str, float, str, str, str]] = {
    ("UP", "UP"): ("ALIGNED_UP", 1.0, "breakout entries", "favour", "favour"),
    ("UP", "FLAT"): ("TREND_PAUSE", 1.0, "normal", "favour", "neutral"),
    ("UP", "DOWN"): ("PULLBACK", 1.0, "pullback-buy after reclaim (hold, no add)", "favour", "neutral"),
    ("UP", "SHOCK_DOWN"): ("SHOCK_IN_UPTREND", 0.75, "wait >= 1 session", "neutral", "block"),
    ("UP", "SHOCK_UP"): ("EXTENDED", 1.0, "no chase, trail stops", "neutral", "neutral"),
    ("FLAT", "UP"): ("EMERGING", 0.5, "normal, small", "neutral", "neutral"),
    ("FLAT", "FLAT"): ("RANGE", 0.5, "mean-reversion only", "neutral", "neutral"),
    ("FLAT", "DOWN"): ("WEAKENING", 0.25, "none new", "neutral", "neutral"),
    ("FLAT", "SHOCK_DOWN"): ("BREAKDOWN_RISK", 0.25, "none", "block", "block"),
    ("FLAT", "SHOCK_UP"): ("SPIKE", 0.5, "no chase", "neutral", "neutral"),
    ("DOWN", "UP"): ("COUNTER_TREND", 0.0, "small/none (half tactical size at most)", "neutral", "neutral"),
    ("DOWN", "FLAT"): ("DOWNTREND", 0.0, "none new", "neutral", "neutral"),
    ("DOWN", "DOWN"): ("DEFENSIVE", 0.0, "none", "block", "block"),
    ("DOWN", "SHOCK_DOWN"): ("DEFENSIVE_SHOCK", 0.0, "none; fast lane prefers tighten-stop/sell", "block", "block"),
    ("DOWN", "SHOCK_UP"): ("SHORT_SQUEEZE", 0.0, "none", "neutral", "neutral"),
}
# Used when LTM is unknown/stale -> FLAT row; STM unknown -> FLAT column.
UNKNOWN_ALIGNMENT = "RANGE"


# ------------------------------------------------------------------ helpers
def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clean(close) -> pd.Series:
    s = pd.Series(close, dtype="float64") if not isinstance(close, pd.Series) else close.astype("float64")
    return s.replace([np.inf, -np.inf], np.nan).dropna()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _ret(s: pd.Series, n: int):
    if len(s) <= n:
        return None
    base = float(s.iloc[-1 - n])
    return float(s.iloc[-1]) / base - 1 if base else None


# ------------------------------------------------------------------ long-term regime
def vote_frame(close: pd.Series) -> pd.DataFrame:
    """Six yes/no tests per bar; NaN where the test cannot be evaluated yet (short history)."""
    c = close
    n = len(c)
    e50, e100, e200 = _ema(c, 50), _ema(c, 100), _ema(c, 200)
    f = pd.DataFrame(index=c.index, dtype="float64")

    def test(name, cond, valid):
        col = cond.astype("float64")
        col[~valid] = np.nan
        f[name] = col

    idx = pd.Series(np.arange(n), index=c.index)
    test("c_gt_e200", c > e200, idx >= 199)
    test("e50_gt_e200", e50 > e200, idx >= 199)
    test("r63", c.pct_change(63) > 0, idx >= 63)
    test("r126", c.pct_change(126) > 0, idx >= 126)
    test("r252", c.pct_change(252) > 0, idx >= 252)
    test("e100_rising", e100 > e100.shift(20), idx >= 119)
    return f


def raw_label_from_votes(yes: float, avail: int) -> str:
    if avail < MIN_TESTS:
        return "FLAT"
    ratio = yes / avail
    if ratio >= 5 / 6 - 1e-9:
        return "UP"
    if ratio <= 1 / 6 + 1e-9:
        return "DOWN"
    return "FLAT"


def raw_label_series(close) -> pd.Series:
    c = _clean(close)
    f = vote_frame(c)
    yes, avail = f.sum(axis=1, skipna=True), f.notna().sum(axis=1)
    return pd.Series([raw_label_from_votes(y, int(a)) for y, a in zip(yes, avail)], index=c.index)


def apply_hysteresis(raw: list[str], start: str | None = None, start_pending: tuple[str | None, int] = (None, 0),
                     confirm: int = CONFIRM_SESSIONS):
    """Walk raw labels; the label changes only after the same new raw label held `confirm` sessions in a row.
    Returns (labels, since_index_of_current_label, pending, pending_days)."""
    label = start or (raw[0] if raw else "FLAT")
    pend, days = start_pending
    labels, since = [], 0
    for i, r in enumerate(raw):
        if r == label:
            pend, days = None, 0
        else:
            if r == pend:
                days += 1
            else:
                pend, days = r, 1
            if days >= confirm:
                label, pend, days, since = r, None, 0, i
        labels.append(label)
    return labels, since, pend, days


def ltm_regime(close, high=None, low=None, prev_state: dict | None = None) -> dict:
    """Trend regime of one metal from COMPLETED daily bars. Never raises; short/odd data -> FLAT + stale."""
    c = _clean(close)
    n = len(c)
    out: dict = {"label": "FLAT", "raw_label": "FLAT", "votes": None, "tests": 0, "stale": False,
                 "since": None, "pending": None, "pending_days": 0, "asof": None,
                 "r21": None, "r63": None, "r126": None, "r252": None,
                 "vol20": None, "vol_pct": None, "vol_regime": "NORMAL", "bars": n}
    if n:
        try:
            out["asof"] = pd.Timestamp(c.index[-1]).date().isoformat()
        except (ValueError, TypeError):
            out["asof"] = None
    if n < MIN_BARS:
        out["stale"], out["reason"] = True, "fewer than 20 daily bars"
        return out
    for k, d in (("r21", 21), ("r63", 63), ("r126", 126), ("r252", 252)):
        v = _ret(c, d)
        out[k] = None if v is None else round(v, 4)
    f = vote_frame(c)
    last = f.iloc[-1]
    avail = int(last.notna().sum())
    yes = int(last.sum(skipna=True))
    out["votes"], out["tests"] = yes, avail
    if avail < MIN_TESTS:
        out["stale"], out["reason"] = True, "too little history for the trend tests"
    raw = raw_label_series(c).tolist()
    out["raw_label"] = raw[-1]
    start, pend, offset = None, (None, 0), 0
    ps = prev_state if isinstance(prev_state, dict) else {}
    if ps.get("label") in LTM_LABELS and ps.get("asof"):
        # continue the stored hysteresis state over the bars after its asof instead of replaying all history
        try:
            pos = c.index.get_loc(pd.Timestamp(ps["asof"]))
        except (KeyError, TypeError, ValueError):
            pos = None
        if isinstance(pos, int):
            start, pend, offset = ps["label"], (ps.get("pending"), int(ps.get("pending_days") or 0)), pos + 1
    labels, since_i, pending, pdays = apply_hysteresis(raw[offset:], start, pend)
    if offset and not labels:        # no new bar since the stored state
        labels, since_i, pending, pdays = [start], 0, pend[0], pend[1]
    out["label"] = labels[-1]
    out["pending"], out["pending_days"] = pending, pdays
    if offset and out["label"] == start and ps.get("since"):
        out["since"] = ps["since"]
    else:
        try:
            out["since"] = pd.Timestamp(c.index[offset + since_i]).date().isoformat()
        except (ValueError, TypeError, IndexError):
            out["since"] = None
    # volatility: annualised stdev of 20 daily returns and its percentile over the last 252 sessions
    rets = c.pct_change().dropna()
    if len(rets) >= 20:
        roll = rets.rolling(20).std().dropna() * math.sqrt(252)
        v = float(roll.iloc[-1])
        out["vol20"] = round(v, 4)
        window = roll.iloc[-252:]
        if len(window) >= 60:
            pct = float((window <= v).mean() * 100)
            out["vol_pct"] = round(pct, 1)
            out["vol_regime"] = "LOW" if pct < 30 else "HIGH" if pct > 80 else "NORMAL"
    if len(rets) >= 60:
        s60 = float(rets.iloc[-60:].std())
        if s60 > 0:
            out["vol_ratio"] = round(float(rets.iloc[-5:].std()) / s60, 2)
    return out


def driver_scores(closes: pd.DataFrame, drivers: dict, lookback: int = 63) -> dict:
    """Per-driver change over `lookback` sessions and the weighted sign-score in [-1, 1]."""
    rows, num, den = {}, 0.0, 0.0
    for tkr, w in (drivers or {}).items():
        if closes is None or tkr not in closes:
            continue
        s = _clean(closes[tkr])
        ch = _ret(s, lookback)
        if ch is None:
            continue
        sign = 1 if ch > 0 else -1 if ch < 0 else 0
        rows[tkr] = {"chg_pct": round(ch * 100, 2), "contrib": round(w * sign, 2)}
        num += w * sign
        den += abs(w)
    return {"drivers": rows, "driver_score": round(num / den, 3) if den else None}


# ------------------------------------------------------------------ short-term state
def stm_state(closes, live_px: float | None = None, open_gap: float | None = None) -> dict:
    """closes = completed daily closes (most recent last). live_px = today's price (appended as today).
    open_gap = today's open vs previous close as a FRACTION (optional). First match wins:
    SHOCK_DOWN/SHOCK_UP (z_day beyond +-2.5 in the last 2 sessions, or today's gap beyond 1.5 daily vol),
    UP (z5 >= 0.5 and close > EMA10), DOWN (mirror), else FLAT. Too little data -> FLAT."""
    c = _clean(closes)
    lp = _num(live_px)
    if lp and lp > 0:
        c = pd.concat([c, pd.Series([lp])], ignore_index=True)
    res: dict = {"state": "FLAT", "z5": None, "z_day": None, "m5_pct": None, "m10_pct": None,
                 "ema10_side": None, "last_shock": None, "sigma_d": None}
    if len(c) < 11:
        res["reason"] = "fewer than 11 closes"
        return res
    rets = c.pct_change().dropna().reset_index(drop=True)
    base = rets.iloc[:-2] if len(rets) >= 12 else rets
    base = base.iloc[-20:]
    sd = float(base.std()) if len(base) >= 5 else 0.0
    if not sd or not math.isfinite(sd):
        res["reason"] = "no volatility"
        return res
    res["sigma_d"] = round(sd, 5)
    z = (rets / sd)
    res["z_day"] = round(float(z.iloc[-1]), 2)
    m5, m10 = _ret(c, 5), _ret(c, 10)
    res["m5_pct"] = None if m5 is None else round(m5 * 100, 2)
    res["m10_pct"] = None if m10 is None else round(m10 * 100, 2)
    z5 = None if m5 is None else m5 / (sd * math.sqrt(5))
    res["z5"] = None if z5 is None else round(z5, 2)
    e10 = float(_ema(c, 10).iloc[-1])
    res["ema10_side"] = "above" if c.iloc[-1] > e10 else "below"
    last2 = z.iloc[-2:]
    gap = _num(open_gap)
    gz = None if gap is None else gap / sd
    if (last2 <= -SHOCK_Z).any() or (gz is not None and gz <= -SHOCK_GAP_VOL):
        res["state"] = "SHOCK_DOWN"
    elif (last2 >= SHOCK_Z).any() or (gz is not None and gz >= SHOCK_GAP_VOL):
        res["state"] = "SHOCK_UP"
    elif z5 is not None and z5 >= Z5_MIN and c.iloc[-1] > e10:
        res["state"] = "UP"
    elif z5 is not None and z5 <= -Z5_MIN and c.iloc[-1] < e10:
        res["state"] = "DOWN"
    big = z[z.abs() >= SHOCK_Z]
    if len(big):
        k = int(len(rets) - 1 - big.index[-1])
        res["last_shock"] = {"sessions_ago": k, "z": round(float(big.iloc[-1]), 2),
                             "dir": "down" if big.iloc[-1] < 0 else "up"}
    return res


# ------------------------------------------------------------------ alignment
def lower_bias(a: str, b: str) -> str:
    """The more cautious of two bias values (unknown values count as block)."""
    return RANK_BIAS[min(BIAS_RANK.get(a, 0), BIAS_RANK.get(b, 0))]


def apply_ceiling(bias: str, ceiling: str) -> tuple[str, bool]:
    """Bias cannot exceed the ceiling. Returns (bias, downgraded). Only ever lowers."""
    b = BIAS_RANK.get(bias)
    if b is None:
        return bias, False
    cap = BIAS_RANK.get(ceiling, 1)
    if b > cap:
        return RANK_BIAS[cap], True
    return bias, False


def align(ltm: str | None, stm: str | None, overrides: dict | None = None, metal: str = "") -> dict:
    """Alignment record. overrides (all optional, each can only lower ceiling/core):
    stale (bool) | event ("none"|"neutral"|"block") + event_note | thesis_cautious (bool) | month_end (bool) |
    cushion_low (bool) | vol_high (bool)."""
    ov = overrides or {}
    notes: list[str] = []
    ltm_u = ltm if ltm in LTM_LABELS else None
    stm_u = stm if stm in STM_STATES else None
    stale = bool(ov.get("stale")) or ltm_u is None
    if stale:
        notes.append("data stale/unknown: LTM treated as FLAT")
        ltm_eff = "FLAT"
    else:
        ltm_eff = ltm_u
    stm_eff = stm_u or "FLAT"
    state, core, tactical, ceiling, default = ALIGN_TABLE[(ltm_eff, stm_eff)]
    if stale and ltm_u is not None:
        # a stale reading is treated as FLAT, but never as LESS cautious than the stored label (e.g. a stale DOWN)
        _, c2, _, ceil2, def2 = ALIGN_TABLE[(ltm_u, stm_eff)]
        core, ceiling, default = min(core, c2), lower_bias(ceiling, ceil2), lower_bias(default, def2)
    base_core = core
    ceil_r = BIAS_RANK[ceiling]

    ev = ov.get("event") or "none"
    if ev in ("neutral", "block"):
        ceil_r = min(ceil_r, BIAS_RANK[ev])
        notes.append(f"event: {ov.get('event_note') or ev} (ceiling <= {ev})")
    if ov.get("thesis_cautious"):
        ceil_r = min(ceil_r, BIAS_RANK["neutral"])
        core *= 0.5
        notes.append("thesis cautious (ceiling <= neutral, core x0.5)")
    if ov.get("month_end"):
        ceil_r = min(ceil_r, BIAS_RANK["neutral"])
        notes.append("month-end window (ceiling <= neutral)")
    if ov.get("cushion_low"):
        ceil_r = min(ceil_r, BIAS_RANK["neutral"])
        core *= 0.5
        notes.append("cushion low (ceiling <= neutral, core x0.5)")
    if ov.get("vol_high"):
        core *= 0.75
        notes.append("vol HIGH (core x0.75)")
    ceiling = RANK_BIAS[ceil_r]
    default, _ = apply_ceiling(default, ceiling)
    return {"metal": metal, "ltm": ltm_u or "UNKNOWN", "stm": stm_u or "UNKNOWN", "state": state,
            "core_mult": round(min(core, base_core), 4), "tactical": tactical,
            "ceiling": ceiling, "default": default, "overrides": notes}


def format_line(a: dict) -> str:
    """One compact line (about 120 chars) for the top of every context view."""
    ov = "; ".join(a.get("overrides") or [])
    ov = (" | " + ov[:70]) if ov else ""
    return (f"{a.get('metal') or '?'}[{a['state']}] ltm={a['ltm']} stm={a['stm']} core x{a['core_mult']:g} "
            f"ceiling={a['ceiling']} default={a['default']}{ov}")
