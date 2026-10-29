"""Strategy v2 ("trend core", growth, AGGRESSIVE profile): the pure rules. Paper trading only.

Everything here is a plain function of its inputs (no clock, no files, no network), so each rule is unit-tested on
its own (tests/test_trend.py), the engine (simgold/engine.py) wires them into the daily 15:10 IST check, and the
backtest (simgold/backtest.py, `run_backtest.py --strategy v2`) replays the very same code on daily bars.

  frame()           per-day trend state, volatility and RSI(2) for one symbol (completed closes + a provisional close)
  describe()        the plain-English "why" of a trend state, with the formula numbers
  target_weights()  w_i = on_i * min(cap_i, vol_target / sigma_i), scaled down to a gross of 100 percent, times the brake
  brake_update()    the drawdown brake on total value (-15 percent: half size; -25 percent: flat until recovery)
  decide()          what to do for one symbol: enter, add on a dip, trim, exit, or nothing

Rules (docs/RULEBOOK.md, "Strategy v2"):
  GOLDBEES            ON when close > SMA200 (+ a small hysteresis band), OFF when close < SMA200 (- the band)
  SILVERBEES, HINDCOPPER   ON when close > highest high of the prior 55 days; OFF when
                      close < max(peak close since entry - 3 x ATR20, lowest low of the prior 20 days)
All numbers live in config/params.yaml under `strategy:`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MODE_V2 = "v2_trend"
MODE_V1 = "v1_scores"


def is_v2(p: dict | None) -> bool:
    """True when the params select the v2 trend core. Anything else (or no `strategy:` block) is the v1 score rules."""
    return bool(p) and (p.get("strategy") or {}).get("mode") == MODE_V2


# ---------------------------------------------------------------- inputs
@dataclass
class Bars:
    """Completed daily bars of one symbol, oldest first (no bar for today)."""
    close: list[float]
    high: list[float]
    low: list[float]
    dates: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)      # rows the sanity filter removed, for the log


def _good(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def clean_bars(close, high, low, dates=None, spike: float = 0.25, spike_revert: float = 0.5, hl_mult: float = 4.0) -> Bars:
    """Sanity filter for Yahoo daily bars.
      1. rows with a bad close are dropped; a row identical to the one before (c, h, l) is a forward-filled gap, dropped;
      2. a one-day spike is dropped: the close moved more than `spike` from the previous close AND the next close reverts
         more than `spike_revert` of that move (the last bar cannot be judged and is kept);
      3. a high under the close (or a low over it) is pulled onto the close, and highs/lows are clipped to
         close +/- hl_mult x the median true range (a bad low would otherwise inflate the ATR and lower the exit level)."""
    rows, dropped = [], []
    for i, cv in enumerate(close):
        d = str(dates[i]) if dates is not None and i < len(dates) else ""
        if not _good(cv):
            continue
        hv = high[i] if i < len(high) and _good(high[i]) else cv
        lv = low[i] if i < len(low) and _good(low[i]) else cv
        row = [float(cv), float(max(hv, cv)), float(min(lv, cv)), d]
        if rows and rows[-1][:3] == row[:3]:
            dropped.append(f"{d or i}: duplicate of the previous bar (forward-filled gap)")
            continue
        rows.append(row)
    keep = list(rows)
    changed = True
    while changed and len(keep) > 2:
        changed = False
        for i in range(1, len(keep) - 1):
            prev, cur, nxt = keep[i - 1][0], keep[i][0], keep[i + 1][0]
            mv = cur - prev
            if abs(mv / prev) > spike and (cur - nxt) * mv > 0 and abs(cur - nxt) > spike_revert * abs(mv):
                dropped.append(f"{keep[i][3] or i}: close {cur:.2f} is {mv / prev * 100:+.0f}% from {prev:.2f} and reverts next day (bad print)")
                del keep[i]
                changed = True
                break
    if len(keep) > 2:
        c = np.array([r[0] for r in keep])
        pc = np.concatenate([[c[0]], c[:-1]])
        h = np.array([r[1] for r in keep])
        l = np.array([r[2] for r in keep])
        tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
        med = float(np.median(tr))
        if med > 0:
            for i, r in enumerate(keep):
                hi, lo = min(r[1], r[0] + hl_mult * med), max(r[2], r[0] - hl_mult * med)
                if hi != r[1] or lo != r[2]:
                    dropped.append(f"{r[3] or i}: high/low clipped to close +/- {hl_mult:g} x median range {med:.2f}")
                r[1], r[2] = hi, lo
    return Bars([r[0] for r in keep], [r[1] for r in keep], [r[2] for r in keep], [r[3] for r in keep], dropped)


# ---------------------------------------------------------------- indicators
def rsi(close: pd.Series, n: int = 2) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    flat = dn == 0                                   # no down move at all: 100 if it only rose, 50 if it did not move
    out[flat] = np.where(up[flat] > 0, 100.0, 50.0)
    return out.where(d.notna() | out.notna())


def atr(close: pd.Series, high: pd.Series, low: pd.Series, n: int = 20) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def annual_vol(close: pd.Series, span: int = 60, method: str = "ewma") -> pd.Series:
    """Annualised close-to-close volatility of daily returns: EWMA with span `span` (the research's sigma60), or a plain
    rolling standard deviation when method is "simple"."""
    r = close.pct_change()
    s = r.rolling(span).std() if method == "simple" else r.ewm(span=span, adjust=False).std()
    return s * math.sqrt(252)


def _hysteresis_states(c: np.ndarray, sma: np.ndarray, band: float) -> np.ndarray:
    """1 when close > SMA x (1 + band), 0 when close < SMA x (1 - band), otherwise the previous state. 0 until the SMA exists."""
    out = np.zeros(len(c))
    state = 0.0
    for i in range(len(c)):
        if math.isnan(c[i]) or math.isnan(sma[i]):
            state = 0.0
        elif c[i] > sma[i] * (1 + band):
            state = 1.0
        elif c[i] < sma[i] * (1 - band):
            state = 0.0
        out[i] = state
    return out


def _donchian_states(c, hi, lo, at, k_atr: float):
    """Research rule (V2 Donchian): enter when close > prior N-day high; while in, peak = highest close since entry and
    exit when close < max(peak - k x ATR, prior M-day low). Returns (on, peak, stop_level) arrays; peak and stop are NaN
    while out. On the exit bar they still hold the values that caused the exit."""
    n = len(c)
    on, peak_out, stop_out = np.zeros(n), np.full(n, np.nan), np.full(n, np.nan)
    inpos, peak = False, 0.0
    for i in range(n):
        if math.isnan(c[i]) or math.isnan(hi[i]):
            continue
        if not inpos:
            if c[i] > hi[i]:
                inpos, peak = True, c[i]
                peak_out[i] = peak
                stop_out[i] = max(peak - k_atr * at[i], lo[i]) if not (math.isnan(at[i]) or math.isnan(lo[i])) else np.nan
        else:
            peak = max(peak, c[i])
            stop = max(peak - k_atr * at[i], lo[i])
            peak_out[i], stop_out[i] = peak, stop
            if c[i] < stop:
                inpos = False
        on[i] = 1.0 if inpos else 0.0
    return on, peak_out, stop_out


def frame(close, high, low, kind: str, st: dict) -> pd.DataFrame:
    """Per-day trend table for one symbol (every row uses only data up to and including that day's close).

    close, high, low: equal-length sequences or Series, oldest first; the LAST element may be a provisional close
    (today's live price). kind: "sma_trend" or "donchian". Columns: close, on (0/1), sigma, rsi, and
      sma_trend: sma, upper (SMA x (1+band)), lower (SMA x (1-band))
      donchian:  hh (prior-N-day high), ll (prior-M-day low), atr, peak (peak close since entry), stop (exit level)"""
    idx = close.index if isinstance(close, pd.Series) else pd.RangeIndex(len(close))
    c = pd.Series(np.asarray(close, dtype=float), index=idx)
    h = pd.Series(np.asarray(high, dtype=float), index=idx)
    l = pd.Series(np.asarray(low, dtype=float), index=idx)
    df = pd.DataFrame({"close": c})
    df["sigma"] = annual_vol(c, st["vol_span"], st.get("vol_method", "ewma"))
    df["rsi"] = rsi(c, st["entry"]["rsi_days"])
    if kind == "sma_trend":
        n, b = st["sma_days"], st["sma_hysteresis"]
        sma = c.rolling(n).mean()
        df["sma"], df["upper"], df["lower"] = sma, sma * (1 + b), sma * (1 - b)
        df["on"] = _hysteresis_states(c.values, sma.values, b)
    elif kind == "donchian":
        hh = h.rolling(st["donchian_entry_days"]).max().shift(1)
        ll = l.rolling(st["donchian_exit_days"]).min().shift(1)
        at = atr(c, h, l, st["atr_days"])
        on, peak, stop = _donchian_states(c.values, hh.values, ll.values, at.values, st["atr_mult"])
        df["hh"], df["ll"], df["atr"], df["on"], df["peak"], df["stop"] = hh, ll, at, on, peak, stop
    else:
        raise ValueError(f"unknown trend signal {kind!r}")
    return df


@dataclass
class TrendInfo:
    sym: str
    kind: str
    on: bool
    was_on: bool               # the state at the previous completed close (before today's provisional bar)
    close: float
    sigma: float | None
    rsi: float | None
    exit_level: float | None   # where the trend would turn OFF (informational; shown as the position's "stop")
    row: dict                  # the raw last row of frame(), for the reason texts
    held_off: str | None = None   # set by the engine: stopped out intraday and not yet re-armed, so the state is forced OFF (reason text)


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def info_from_frame(sym: str, kind: str, df: pd.DataFrame) -> TrendInfo:
    last = df.iloc[-1]
    prev_on = bool(df["on"].iloc[-2]) if len(df) > 1 else False
    row = {k: _f(v) for k, v in last.items()}
    level = row.get("lower") if kind == "sma_trend" else row.get("stop")
    return TrendInfo(sym, kind, bool(last["on"]), prev_on, float(last["close"]), row["sigma"], row["rsi"],
                     level if bool(last["on"]) else None, row)


def compute(sym: str, kind: str, bars: Bars, live: float, st: dict, min_bars: int | None = None) -> TrendInfo | None:
    """Trend state of `sym` from completed daily bars plus today's live price as the provisional close.
    None when there is not enough history (the SMA or the Donchian window and the volatility need data)."""
    need = min_bars or (st["sma_days"] if kind == "sma_trend" else st["donchian_entry_days"]) + 1
    if not _good(live) or len(bars.close) < need:
        return None
    df = frame(bars.close + [live], bars.high + [live], bars.low + [live], kind, st)
    return info_from_frame(sym, kind, df)


# ---------------------------------------------------------------- plain-English reasons
def _px(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def describe(i: TrendInfo, st: dict) -> tuple[str, str]:
    """(state, detail) with the formula numbers: ("ON", "close 121.40 > SMA200 118.90"). Callers build
    'GOLDBEES trend ON (close 121.40 > SMA200 118.90)' or 'Trend ON: close 121.40 > SMA200 118.90'."""
    r, c = i.row, i.close
    state = "ON" if i.on else "OFF"
    if i.held_off:
        return state, i.held_off
    if i.kind == "sma_trend":
        n, b = st["sma_days"], st["sma_hysteresis"]
        sma = r.get("sma")
        name = f"SMA{n}"
        if sma is None:
            return state, f"not enough history for the {name}"
        if i.on and c > sma * (1 + b):
            return state, f"close {_px(c)} > {name} {_px(sma)}"
        if not i.on and c < sma * (1 - b):
            return state, f"close {_px(c)} < {name} {_px(sma)}"
        return state, (f"close {_px(c)} is within the {b * 100:.1f}% band around {name} {_px(sma)}, "
                       f"so the previous state {state} is kept")
    hh, ll, a, peak, stop = r.get("hh"), r.get("ll"), r.get("atr"), r.get("peak"), r.get("stop")
    ne, nx, k = st["donchian_entry_days"], st["donchian_exit_days"], st["atr_mult"]
    if hh is None:
        return state, f"not enough history for the {ne}-day high"
    if i.on:
        if not i.was_on:
            return state, f"close {_px(c)} > {ne}-day high {_px(hh)}, a new breakout"
        return state, (f"close {_px(c)} is above the exit level {_px(stop)} = max(peak close {_px(peak)} - {k:g} x "
                       f"ATR{st['atr_days']} {_px(a)}, {nx}-day low {_px(ll)})")
    if i.was_on and stop is not None:
        return state, (f"close {_px(c)} < exit level {_px(stop)} = max(peak close {_px(peak)} - {k:g} x "
                       f"ATR{st['atr_days']} {_px(a)}, {nx}-day low {_px(ll)})")
    return state, f"close {_px(c)} is not above the {ne}-day high {_px(hh)}"


# ---------------------------------------------------------------- stop-loss package D5-S (strategy.stops)
def next_exit_parts(close, high, low, st: dict) -> dict | None:
    """The trend exit level for the NEXT session, from completed bars only (pure; used for the intraday stop of
    strategy.stops.intraday_exit). level = max(peak close since entry - atr_mult x ATR, lowest low of the last
    donchian_exit_days completed bars), only while the Donchian state is ON at the last completed bar.
    Returns {"level", "peak", "atr", "low"} or None (state OFF, or not enough history)."""
    n = len(close)
    if n < st["donchian_entry_days"] + 1:
        return None
    df = frame(close, high, low, "donchian", st)
    last = df.iloc[-1]
    if not bool(last["on"]):
        return None
    peak, at = _f(last["peak"]), _f(last["atr"])
    lo = _f(min(float(x) for x in list(low)[-int(st["donchian_exit_days"]):]))
    if peak is None or at is None or lo is None:
        return None
    return {"level": max(peak - st["atr_mult"] * at, lo), "peak": peak, "atr": at, "low": lo}


def next_exit_level(close, high, low, st: dict) -> float | None:
    """Pure: the level for the next session (see next_exit_parts), or None while the trend is OFF."""
    parts = next_exit_parts(close, high, low, st)
    return parts["level"] if parts else None


def next_exit_levels(df: pd.DataFrame, low, st: dict) -> pd.Series:
    """Vector form for the backtest: for every row of a Donchian frame() the level for the session AFTER that row
    (NaN while the state is OFF at that row)."""
    lows = pd.Series(np.asarray(low, dtype=float), index=df.index)
    ll = lows.rolling(int(st["donchian_exit_days"])).min()
    lvl = np.maximum(df["peak"] - st["atr_mult"] * df["atr"], ll)
    return lvl.where(df["on"] > 0)


def floor_weight(w_detail, weights: dict, st: dict, brake_level: int = 0) -> float:
    """Weight of the gold floor: max(0, min_invested - sum of the other (satellite) weights); 0 at brake level 2 or when the
    floor is off. weights: {sym: weight} (targets or current). w_detail is the target_weights_ex detail (kept for the call
    signature; the floor is a pure function of the satellite weights)."""
    mi = float(st.get("min_invested") or 0.0)
    if mi <= 0 or int(brake_level) >= 2:
        return 0.0
    g = st.get("floor_symbol", "GOLDBEES")
    return max(0.0, mi - sum(float(v) for s, v in weights.items() if s != g))


# ---------------------------------------------------------------- target weights
def raw_weight(on: bool, sigma: float | None, cap: float, vol_target: float) -> float:
    """w = on * min(cap, vol_target / sigma). No usable volatility means no position."""
    if not on or sigma is None or not math.isfinite(sigma) or sigma <= 0:
        return 0.0
    return min(cap, vol_target / sigma)


def target_weights_ex(infos: dict, st: dict, brake_mult: float = 1.0, ineligible=()) -> tuple[dict, float, dict]:
    """infos: {sym: (on, sigma)}. Returns ({sym: target weight}, scale, detail).

    1. v2 weights: raw_i = on_i * min(cap_i, vol_target / sigma_i), scaled down together when they sum to more than 100 percent
       (`scale`; no leverage). Symbols in `ineligible` (waiting to re-confirm after the deep brake) get 0.
    2. stop moves to gold (strategy.stop_to_gold): each SILVERBEES/HINDCOPPER that is OFF hands its vol weight
       min(cap, vol_target / sigma) to the floor symbol (GOLDBEES), but only while gold's own trend is ON when
       stop_to_gold_requires_gold_trend is set.
    3. invested floor (strategy.min_invested): the floor symbol is raised so that the metals total is at least min_invested of the
       portfolio, even when its own trend is OFF. If the total is then over 100 percent, everything is scaled down together.
    4. brake multiplier (applied last): level 1 (0 < m < 1) multiplies every weight but the floor symbol never goes below
       min(its weight, min_invested); level 2 (m = 0) is flat, the floor included.
    detail: {"floor": bool, "metals_before": float, "to_gold": float, "to_gold_from": [syms], "gold_trend": bool}"""
    syms = list(infos)
    raw = {s: (0.0 if s in ineligible else raw_weight(on, sg, st["symbols"][s]["cap"], st["vol_target"]))
           for s, (on, sg) in infos.items()}
    total = sum(raw.values())
    scale = 1.0 / total if total > 1.0 else 1.0
    w = {s: v * scale for s, v in raw.items()}
    g = st.get("floor_symbol", "GOLDBEES")
    detail = {"floor": False, "metals_before": sum(w.values()), "to_gold": 0.0, "to_gold_from": [], "gold_trend": bool(infos.get(g, (False,))[0])}
    mi = float(st.get("min_invested") or 0.0)
    if g in infos and (st.get("stop_to_gold") or mi > 0):
        if st.get("stop_to_gold") and (infos[g][0] or not st.get("stop_to_gold_requires_gold_trend", True)):
            for s, (on, sg) in infos.items():
                if s != g and not on:
                    room = min(st["symbols"][s]["cap"], st["vol_target"] / sg) if (sg and math.isfinite(sg) and sg > 0) else 0.0
                    if room > 0:
                        w[g] += room
                        detail["to_gold"] += room
                        detail["to_gold_from"].append(s)
        detail["metals_before"] = sum(w.values())
        if mi > 0:
            need = mi - sum(v for s, v in w.items() if s != g)
            if need > w[g] + 1e-12:
                w[g] = max(need, 0.0)
                detail["floor"] = True
        tot = sum(w.values())
        if tot > 1.0:
            w = {s: v / tot for s, v in w.items()}
    if brake_mult >= 1.0:
        return w, scale, detail
    out = {s: v * brake_mult for s, v in w.items()}
    if brake_mult > 0 and g in w and mi > 0:
        out[g] = max(out[g], min(w[g], mi))
    return out, scale, detail


def target_weights(infos: dict, st: dict, brake_mult: float = 1.0, ineligible=()) -> tuple[dict, float]:
    w, scale, _ = target_weights_ex(infos, st, brake_mult, ineligible)
    return w, scale


# ---------------------------------------------------------------- drawdown brake
def brake_mult(level: int, st: dict) -> float:
    return {0: 1.0, 1: float(st["brake"]["level1_mult"]), 2: 0.0}[level]


def brake_update(state: dict | None, value: float, on: dict, st: dict, start_capital: float = 100000.0,
                 account_stop: bool = False) -> dict:
    """The drawdown brake. Call it ONCE per trading day (it counts days) on total portfolio value.

    level 0: full targets.
    level 1 (value 15 percent under its peak): all targets x 0.5; released above -13 percent. A RE-BASED level 1 (see below)
            is released only after `level1_min_days` trading days and value more than `rebase_release_gain` above the re-base.
    level 2 (25 percent under): targets 0. Released in one of two ways:
            a) recovery: value back above -15 percent -> level 1 if still under -13 percent, else level 0;
            b) time: at least `level2_min_days` trading days at level 2, at least one symbol is ON and has re-confirmed
               (turned OFF and ON again since the latch), and the account stop is not in force -> level 1 (half size) with
               the peak re-based to the current value.
    state: {"peak", "level", "reconfirm": [syms], "days", "rebase_value"} or None.
    Returns the new state plus "dd" and "changed"."""
    b = st["brake"]
    s = dict(state or {})
    peak = max(float(s.get("peak") or start_capital), value)       # starts at the start capital; only a re-base lowers it
    level = int(s.get("level") or 0)
    days = int(s.get("days") or 0) + 1
    rebase = s.get("rebase_value")
    reconfirm = list(s.get("reconfirm") or [])
    reconfirm = [k for k in reconfirm if on.get(k, True)]       # a symbol that is OFF has reset its confirmation
    dd = value / peak - 1.0
    old = level

    def enter(lv, rb=None):
        nonlocal level, days, rebase
        level, days, rebase = lv, 0, rb

    if level == 2:
        if dd > -b["level2_release_dd"]:
            enter(1 if dd <= -b["level1_release_dd"] else 0)
        elif (days >= b["level2_min_days"] and not account_stop
              and any(v and k not in reconfirm for k, v in on.items())):
            peak = value
            enter(1, value)
    elif dd <= -b["level2_dd"]:
        enter(2)
        reconfirm = sorted(k for k, v in on.items() if v)       # these must turn OFF and ON again
    elif level == 1:
        if rebase:
            if days >= b["level1_min_days"] and value > rebase * (1 + b["rebase_release_gain"]):
                enter(0)
        elif dd > -b["level1_release_dd"]:
            enter(0)
    elif dd <= -b["level1_dd"]:
        enter(1)
    return {"peak": peak, "level": level, "reconfirm": reconfirm, "days": days, "rebase_value": rebase,
            "dd": value / peak - 1.0, "changed": level != old}


# ---------------------------------------------------------------- one symbol, one check
@dataclass
class Decision:
    action: str          # BUY | SELL | HOLD | SKIP
    kind: str            # entry | add | exit | trim | hold | wait
    to_w: float          # the weight to end at (for BUY/SELL)
    why: str             # plain reason fragment, without the formula numbers


def decide(*, on: bool, target: float, cur: float, flat: bool, rsi_val: float | None, bias: str, st: dict,
           brake_level: int = 0, eligible: bool = True, sigma_ok: bool = True) -> Decision:
    """What to do with one symbol at the daily check.

    target/cur are portfolio weights (0..1). `flat` means no shares are held. Rebalances only when the gap is more than
    `rebalance_threshold` (5 points); a move to or from zero always trades (entries need target >= min_entry_weight).
    New buys and adds also need: bias not "block"; entries not overbought (RSI(2) <= overbought);
    adds need an RSI(2) dip below dip_rsi (bias "favour" loosens it to favour_dip_rsi, it never removes it)."""
    thr, e = st["rebalance_threshold"], st["entry"]
    # the suffix must match the real trend state: a floor buy happens with the trend OFF
    no_pos = "trend ON and no position" if on else "no position; this is the gold floor buy (the trend is OFF)"
    if target <= 1e-9:
        if flat:
            return Decision("HOLD", "hold", 0.0, "no position and the target is 0")
        if not eligible:
            why = "waiting to re-confirm the trend after the deep drawdown brake"
        elif brake_level == 2:
            why = "the deep drawdown brake (-25%) is on"
        elif not on:
            why = "the trend is OFF"
        elif not sigma_ok:
            why = "volatility is not available, so no size can be set"
        else:
            why = "the target is 0"
        return Decision("SELL", "exit", 0.0, why)
    if cur - target > thr:
        return Decision("SELL", "trim", target, f"the position is {(cur - target) * 100:.0f} points over its target")
    if target - cur <= thr and not flat:
        return Decision("HOLD", "hold", target, "within the 5-point rebalance band")
    if flat and target < st["min_entry_weight"]:
        return Decision("HOLD", "hold", target, f"target {target * 100:.1f}% is below the minimum entry size")
    if bias == "block":
        return Decision("SKIP", "wait", target, "Claude's bias for this metal is block, so no new buys or adds")
    r = rsi_val
    if not e.get("rsi_timing", True):                  # D5 "stay invested": entries and adds go straight to the target
        return Decision("BUY", "entry" if flat else "add", target, (no_pos if flat else "below target"))
    if flat:
        if r is not None and r > e["overbought"]:
            return Decision("SKIP", "wait", target, f"RSI(2) {r:.1f} is overbought (above {e['overbought']:g}), so the entry waits")
        return Decision("BUY", "entry", target, no_pos)
    lim = e["favour_dip_rsi"] if bias == "favour" else e["dip_rsi"]       # favour only loosens the dip, it never removes it
    if r is not None and r < lim:
        return Decision("BUY", "add", target, f"RSI(2) {r:.1f} < {lim:g} dip" + (" (bias favour loosens it)" if bias == "favour" else ""))
    rs = "n/a" if r is None else f"{r:.1f}"
    return Decision("HOLD", "wait", target, f"below target, waiting for a dip (RSI(2) {rs} is not under {lim:g})")
