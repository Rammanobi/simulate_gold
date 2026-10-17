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


def _good(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def clean_bars(close, high, low, dates=None) -> Bars:
    """Drop rows with a bad close; a high below the close (or a low above it) is data noise and is pulled onto the close."""
    c, h, l, d = [], [], [], []
    for i, cv in enumerate(close):
        if not _good(cv):
            continue
        hv = high[i] if i < len(high) and _good(high[i]) else cv
        lv = low[i] if i < len(low) and _good(low[i]) else cv
        c.append(float(cv))
        h.append(float(max(hv, cv)))
        l.append(float(min(lv, cv)))
        d.append(str(dates[i]) if dates is not None and i < len(dates) else "")
    return Bars(c, h, l, d)


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


# ---------------------------------------------------------------- target weights
def raw_weight(on: bool, sigma: float | None, cap: float, vol_target: float) -> float:
    """w = on * min(cap, vol_target / sigma). No usable volatility means no position."""
    if not on or sigma is None or not math.isfinite(sigma) or sigma <= 0:
        return 0.0
    return min(cap, vol_target / sigma)


def target_weights(infos: dict, st: dict, brake_mult: float = 1.0, ineligible=()) -> tuple[dict, float]:
    """infos: {sym: (on, sigma)}. Returns ({sym: target weight}, scale) where scale is the factor that brought the sum
    of raw weights down to 100 percent (1.0 when it already fit; there is no leverage). The brake multiplier is applied
    after the scaling. Symbols in `ineligible` (waiting to re-confirm after the deep drawdown brake) get 0."""
    raw = {s: (0.0 if s in ineligible else raw_weight(on, sg, st["symbols"][s]["cap"], st["vol_target"]))
           for s, (on, sg) in infos.items()}
    total = sum(raw.values())
    scale = 1.0 / total if total > 1.0 else 1.0
    return {s: w * scale * brake_mult for s, w in raw.items()}, scale


# ---------------------------------------------------------------- drawdown brake
def brake_mult(level: int, st: dict) -> float:
    return {0: 1.0, 1: float(st["brake"]["level1_mult"]), 2: 0.0}[level]


def brake_update(state: dict | None, value: float, on: dict, st: dict, start_capital: float = 100000.0) -> dict:
    """The drawdown brake, evaluated once per daily check on total portfolio value.

    level 0: full targets.  level 1 (value 15 percent under its peak): all targets x 0.5, released above -13 percent.
    level 2 (25 percent under): targets 0 until value is back above -15 percent AND each symbol that was ON when the
    brake latched has turned OFF and ON again (the trend signal re-confirms).
    state: {"peak", "level", "reconfirm": [syms]} or None. Returns the new state plus "dd" and "changed"."""
    b = st["brake"]
    s = dict(state or {})
    peak = max(float(s.get("peak") or 0.0), float(start_capital), value)
    level = int(s.get("level") or 0)
    reconfirm = [x for x in (s.get("reconfirm") or [])]
    dd = value / peak - 1.0
    old = level
    if level == 2:
        if dd > -b["level2_release_dd"]:
            level = 0
    elif dd <= -b["level2_dd"]:
        level = 2
        reconfirm = sorted(k for k, v in on.items() if v)       # these must turn OFF and ON again
    elif level == 1:
        if dd > -b["level1_release_dd"]:
            level = 0
    elif dd <= -b["level1_dd"]:
        level = 1
    reconfirm = [k for k in reconfirm if on.get(k, True)]       # a symbol that is OFF has reset its confirmation
    return {"peak": peak, "level": level, "reconfirm": reconfirm, "dd": dd, "changed": level != old}


# ---------------------------------------------------------------- one symbol, one check
@dataclass
class Decision:
    action: str          # BUY | SELL | HOLD | SKIP
    kind: str            # entry | add | exit | trim | hold | wait
    to_w: float          # the weight to end at (for BUY/SELL)
    why: str             # plain reason fragment, without the formula numbers


def decide(*, on: bool, target: float, cur: float, flat: bool, rsi_val: float | None, bias: str, st: dict,
           brake_level: int = 0, eligible: bool = True) -> Decision:
    """What to do with one symbol at the daily check.

    target/cur are portfolio weights (0..1). `flat` means no shares are held. Rebalances only when the gap is more than
    `rebalance_threshold` (5 points); a move to or from zero always trades (entries need target >= min_entry_weight).
    New buys and adds also need: bias not "block"; entries not overbought (RSI(2) <= overbought);
    adds need an RSI(2) dip below dip_rsi unless the bias is "favour"."""
    thr, e = st["rebalance_threshold"], st["entry"]
    if target <= 1e-9:
        if flat:
            return Decision("HOLD", "hold", 0.0, "no position and the target is 0")
        if not eligible:
            why = "waiting to re-confirm the trend after the deep drawdown brake"
        elif brake_level == 2:
            why = "the deep drawdown brake (-25%) is on"
        elif not on:
            why = "the trend is OFF"
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
    if flat:
        if r is not None and r > e["overbought"]:
            return Decision("SKIP", "wait", target, f"RSI(2) {r:.1f} is overbought (above {e['overbought']:g}), so the entry waits")
        return Decision("BUY", "entry", target, "trend ON and no position")
    if bias == "favour":
        return Decision("BUY", "add", target, "bias is favour, so it may add without waiting for a dip")
    if r is not None and r < e["dip_rsi"]:
        return Decision("BUY", "add", target, f"RSI(2) {r:.1f} < {e['dip_rsi']:g} dip")
    rs = "n/a" if r is None else f"{r:.1f}"
    return Decision("HOLD", "wait", target, f"below target, waiting for a dip (RSI(2) {rs} is not under {e['dip_rsi']:g})")
