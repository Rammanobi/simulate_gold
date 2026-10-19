"""Fast-lane trigger detection (rulebook 5.1) and the hand-off to the AI routine (5.3).

The engine never waits for the AI: a trigger blocks new buys straight away (engine.py does that).
This module only detects shocks, writes the request line and fires the routine, and it never raises.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import config, trend

log = logging.getLogger(__name__)
BETA = "experimental-cc-routine-2026-04-01"


def detect_price_shocks(sym: str, series: list[tuple[datetime, float]], prev_close: float | None,
                        now: datetime, from_close_done: bool, p: dict | None = None) -> list[dict]:
    """5.1: a move of shock_pct within 10 minutes, or from_close_pct since yesterday's close (once per day)."""
    fl = (p or config.load_params())["fast_lane"]
    out = []
    if not series:
        return out
    price = series[-1][1]
    cutoff = now - timedelta(minutes=fl["shock_window_min"])
    older = [pt for pt in series if pt[0] <= cutoff]
    if older and (cutoff - older[-1][0]) <= timedelta(minutes=5):
        ref = older[-1][1]
        mv = price / ref - 1
        if abs(mv) >= fl["shock_pct"][sym]:
            out.append({"kind": "10min", "symbol": sym, "move": mv,
                        "detail": f"{sym} moved {mv * 100:+.2f}% in {fl['shock_window_min']} minutes "
                                  f"(trigger is {fl['shock_pct'][sym] * 100:.1f}%)."})
    if prev_close and not from_close_done:
        mv = price / prev_close - 1
        if abs(mv) >= fl["from_close_pct"][sym]:
            out.append({"kind": "from_close", "symbol": sym, "move": mv,
                        "detail": f"{sym} is {mv * 100:+.2f}% from yesterday's close "
                                  f"(trigger is {fl['from_close_pct'][sym] * 100:.1f}%)."})
    return out


def detect_portfolio_trigger(value_hist: list[tuple[datetime, float]], value: float, floor: float, now: datetime,
                             p: dict | None = None) -> dict | None:
    """5.1: value fell by 40% or more of the cushion within 15 minutes (v2: by 1.5% of its value)."""
    pp = p or config.load_params()
    fl = pp["fast_lane"]
    win = [v for t, v in value_hist if now - t <= timedelta(minutes=fl["portfolio_drop_window_min"])]
    if not win:
        return None
    top = max(win)
    if trend.is_v2(pp):          # strategy v2: no cushion (the floor is the fixed account stop): a % drop of the value itself
        share = pp["strategy"]["fast_lane_portfolio_drop"]
        if top > 0 and (top - value) / top >= share:
            return {"kind": "portfolio", "symbol": "ALL", "move": -(top - value),
                    "detail": f"Portfolio value fell ₹{top - value:,.0f} ({(top - value) / top * 100:.1f}%) in "
                              f"{fl['portfolio_drop_window_min']} minutes (trigger is {share * 100:.1f}%)."}
        return None
    cushion_then = top - floor
    drop = top - value
    if cushion_then > 0 and drop >= fl["portfolio_drop_share_of_cushion"] * cushion_then:
        return {"kind": "portfolio", "symbol": "ALL", "move": -drop,
                "detail": f"Portfolio value fell ₹{drop:,.0f} in {fl['portfolio_drop_window_min']} minutes, "
                          f"which is {drop / cushion_then * 100:.0f}% of the cushion."}
    return None


def can_fire(runs: dict, now: datetime, p: dict | None = None, cap: int | None = None) -> tuple[bool, str]:
    """`cap` overrides fast_lane.max_runs_per_day (thrift mode passes its lower daily cap, see simgold/budget.py)."""
    fl = (p or config.load_params())["fast_lane"]
    cap = fl["max_runs_per_day"] if cap is None else cap
    if runs.get("date") == now.date().isoformat():
        if runs.get("n", 0) >= cap:
            return False, "daily limit of fast-lane runs used up"
        if runs.get("last_ts"):
            gap = (now - datetime.fromisoformat(runs["last_ts"])).total_seconds() / 60
            if gap < fl["min_minutes_between_runs"]:
                return False, f"previous fast-lane run was only {gap:.0f} min ago"
    return True, ""


def limit_used_up(runs: dict, now: datetime, p: dict | None = None, cap: int | None = None) -> bool:
    fl = (p or config.load_params())["fast_lane"]
    cap = fl["max_runs_per_day"] if cap is None else cap
    return runs.get("date") == now.date().isoformat() and runs.get("n", 0) >= cap


def append_request(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        log.exception("could not write fast-lane request")


def http_post(url: str, headers: dict, body: dict, timeout: float = 10) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 - URL comes from the owner's env
        return r.status


def fire(environ, summary: dict, post=http_post) -> tuple[bool, str]:
    """POST the summary to the routine. Returns (attempted, note). Never raises."""
    # a pasted secret often carries a trailing newline or space; that made urllib raise ValueError on 14 Oct 2026
    url, token = (environ.get("ROUTINE_FIRE_URL") or "").strip(), (environ.get("ROUTINE_FIRE_TOKEN") or "").strip()
    if not url or not token:
        return False, "ROUTINE_FIRE_URL / ROUTINE_FIRE_TOKEN not set"
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": BETA,
               "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    try:
        text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), default=str)[:3500]
        status = post(url, headers, {"text": text}, timeout=10)
        return True, f"HTTP {status}"
    except Exception as e:      # network, HTTP error, bad URL: the engine carries on
        log.warning("fast-lane POST failed: %s", e)
        return True, f"POST failed: {type(e).__name__}"
