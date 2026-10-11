"""The decision engine shared by backtest and live mode.

score = 0.55·technical + 0.15·macro + 0.30·news      (each in [-1, 1])
target weight = max_weight × clip(score) × min(1, target_vol / realised_vol)
then portfolio-level risk caps (cash floor, drawdown breaker, monthly profit lock).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from . import config
from .broker import Portfolio

ENTRY_SCORE = 0.15   # need a clear positive score to open a position
EXIT_SCORE = 0.0     # once held, hold until the score turns negative (avoids churn)
FULL_SIZE_SCORE = 0.40  # conviction ramp: full max_weight once score ≥ 0.40


def _d(ts) -> str:
    return str(ts)[:10]


def risk_state(pf: Portfolio, equity: float, today: str) -> dict:
    r = config.RISK
    month = today[:7]
    if pf.month != month:
        pf.month, pf.month_start_equity = month, equity
    pf.peak_equity = max(pf.peak_equity, equity)
    dd = equity / pf.peak_equity - 1
    mtd = equity / pf.month_start_equity - 1
    cap, notes = 1 - r.min_cash, []
    if pf.flat_until and today < pf.flat_until:
        cap, notes = 0.0, [f"hard drawdown breaker: flat until {pf.flat_until}"]
    elif dd <= -r.dd_hard:
        pf.flat_until = (datetime.fromisoformat(today) + timedelta(days=5)).date().isoformat()
        pf.peak_equity = equity  # restart the drawdown clock after the cool-off
        cap, notes = 0.0, [f"drawdown {dd:.1%} ≤ −{r.dd_hard:.0%}: go flat for 5 days"]
    elif dd <= -r.dd_soft:
        cap *= 0.5
        notes.append(f"drawdown {dd:.1%}: exposure halved")
    if mtd >= r.month_lock_gain:
        cap = min(cap, 0.30)
        notes.append(f"month-to-date {mtd:+.1%} ≥ target: locking profits, gross ≤30%")
    elif mtd <= r.month_cut_loss:
        cap = min(cap, 0.50)
        notes.append(f"month-to-date {mtd:+.1%}: defensive, gross ≤50%")
    return {"equity": equity, "drawdown": dd, "mtd": mtd, "gross_cap": cap, "notes": notes}


def decide(pf: Portfolio, snap: dict[str, dict], ts, news: dict[str, float] | None = None,
           cooldown_min: int = 0) -> tuple[list[dict], dict]:
    """snap[asset] = {price, tech, macro, atr, vol, rsi, trend, mom}. Returns (orders, report)."""
    today = _d(ts)
    prices = {a: s["price"] for a, s in snap.items()}
    equity = pf.equity(prices)
    risk = risk_state(pf, equity, today)
    w = config.WEIGHTS
    rcfg = config.RISK
    cur = pf.weights(prices)
    per_asset, targets = {}, {}

    for a, s in snap.items():
        spec = config.ASSETS[a]
        n = news.get(a) if news else None
        if n is None:
            score = (w.technical * s["tech"] + w.macro * s["macro"]) / (w.technical + w.macro)
        else:
            score = w.technical * s["tech"] + w.macro * s["macro"] + w.news * n
        held = pf.qty(a) > 0
        if held:
            pos = pf.positions[a]
            pos.high_water = max(pos.high_water, s["price"])
        vol_scalar = min(1.0, rcfg.target_vol / s["vol"]) if s["vol"] and s["vol"] > 0 else 0.5
        threshold = EXIT_SCORE if held else ENTRY_SCORE
        tgt = spec["max_weight"] * min(1.0, max(0.0, score) / FULL_SIZE_SCORE) * vol_scalar if score > threshold else 0.0
        why = []
        if held and s["atr"] and s["price"] < pf.positions[a].high_water - rcfg.atr_stop_mult * s["atr"]:
            tgt = 0.0
            pf.stop_until[a] = (datetime.fromisoformat(today) + timedelta(days=rcfg.stop_cooldown_days)).date().isoformat()
            why.append(f"trailing stop hit (high {pf.positions[a].high_water:.2f} − {rcfg.atr_stop_mult}×ATR)")
        elif pf.stop_until.get(a, "") > today:
            tgt = 0.0
            why.append(f"stop cooldown until {pf.stop_until[a]}")
        targets[a] = tgt
        per_asset[a] = {
            "price": round(s["price"], 2), "score": round(score, 3), "tech": round(s["tech"], 3),
            "macro": round(s["macro"], 3), "news": None if n is None else round(n, 3),
            "rsi": round(s["rsi"], 1), "vol": round(s["vol"], 3) if s["vol"] else None,
            "current_w": round(cur.get(a, 0), 3), "notes": why,
        }

    gross = sum(targets.values())
    scale = min(1.0, risk["gross_cap"] / gross) if gross > 0 else 1.0
    orders = []
    for a, tgt in targets.items():
        tgt *= scale
        per_asset[a]["target_w"] = round(tgt, 3)
        diff = tgt - cur.get(a, 0)
        full_exit = tgt == 0 and pf.qty(a) > 0
        if abs(diff) < rcfg.rebalance_band and not full_exit:
            continue
        if cooldown_min and a in pf.last_trade_at and not per_asset[a]["notes"]:
            last = datetime.fromisoformat(pf.last_trade_at[a])
            if (ts - last).total_seconds() < cooldown_min * 60:
                continue
        price = prices[a]
        if full_exit:
            qty, side = pf.qty(a), "SELL"
        else:
            qty, side = int(abs(diff) * equity // price), ("BUY" if diff > 0 else "SELL")
        if qty <= 0:
            continue
        reason = "; ".join(per_asset[a]["notes"] + risk["notes"]) or (
            f"score {per_asset[a]['score']:+.2f} → target {tgt:.0%} (now {cur.get(a, 0):.0%})")
        orders.append({"asset": a, "side": side, "qty": qty, "price": price, "reason": reason,
                       "protective": bool(per_asset[a]["notes"] or (side == "SELL" and risk["notes"]))})
    orders.sort(key=lambda o: o["side"] != "SELL")  # sells first to free cash
    report = {"ts": str(ts), "risk": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in risk.items()},
              "assets": per_asset, "orders": orders}
    return orders, report


def apply_review(orders: list[dict], review: dict | None) -> list[dict]:
    """Claude may only scale proposed orders down; protective sells always go through."""
    if not review:
        return orders
    scales = {d["asset"]: (max(0.0, min(1.0, d["scale"])), d["reason"]) for d in review.get("decisions", [])}
    out = []
    for o in orders:
        s, why = scales.get(o["asset"], (1.0, ""))
        if o["protective"]:
            s = 1.0
        q = math.floor(o["qty"] * s)
        if q > 0:
            out.append({**o, "qty": q, "reason": o["reason"] + (f" | review×{s:.2f}: {why}" if why else "")})
    return out
