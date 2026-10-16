#!/usr/bin/env python3
"""Decide whether a routine should do deep work or stop early (cheap, no AI).

  python tools/should_think.py [--force]
Exit 0 and print "THINK: <reasons>", or exit 3 and print "SKIP: <why>".
Thresholds live in config/params.yaml under `routines:`.
Think if ANY of: guidance.json missing or too old; an instrument moved more than move_factor x its fast-lane 10-minute
threshold since the last guidance; a new headline with urgency >= 3 since the last guidance; an event today or
tomorrow; an open position within 1% of its stop; cushion under 300; no guidance written yet today (first run of
the day); the alignment state (LTM x STM, memory/stm) of a metal changed since the last guidance; --force.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import brief, config, memstore


def alignment_changes(root: Path, now: datetime, p: dict) -> list[str]:
    """Reasons for each metal whose alignment state differs from the one stored by the last `guidance.py set/touch`.
    No stored alignment (older guidance.json) -> no reason, so the rollout does not cause a THINK storm."""
    old = brief.load_json(Path(root) / "state" / "guidance.json", {})
    old = old.get("alignment") if isinstance(old, dict) else None
    if not isinstance(old, dict) or not old:
        return []
    try:
        cur = memstore.current_alignment(root, now, p)
    except Exception:
        return []
    return [f"alignment state changed for {m}: {old[m]} -> {cur[m]['state']}" for m in memstore.METALS
            if m in old and old[m] != cur[m]["state"]]


def evaluate(root: Path, now: datetime, force: bool = False, params: dict | None = None) -> list[str]:
    """Return the list of reasons to think (empty list = skip)."""
    p = params or config.load_params()
    r = p["routines"]
    root = Path(root)
    why: list[str] = []
    if force:
        why.append("--force was passed")
    g = brief.guidance_view(root, now)
    upd = g["updated"]
    max_age = r["guidance_max_age_hours"] * 60
    if not g["exists"] or upd is None:
        why.append("guidance.json is missing or unreadable")
    else:
        if g["age_min"] > max_age:
            why.append(f"guidance is {brief.age_text(g['age_min'])} old (limit {r['guidance_max_age_hours']} h)")
        if upd.date() != now.date():
            why.append("first run of the day (no guidance written today)")

        snap = g["prices"]
        last = brief.load_json(root / "state" / "last_prices.json", {})
        last = last if isinstance(last, dict) else {}
        for sym, thr in p["fast_lane"]["shock_pct"].items():
            try:
                then, nowpx = float(snap[sym]), float(last[sym]["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if then > 0:
                mv = nowpx / then - 1
                limit = r["move_factor"] * thr
                if abs(mv) > limit:
                    why.append(f"{sym} moved {mv * 100:+.2f}% since the last guidance (limit {limit * 100:.2f}%)")

        hot = []
        for h in brief.news_items(root):
            try:
                ts = float(h.get("ts", 0))
            except (TypeError, ValueError):
                continue
            if ts > upd.timestamp():
                m = brief.match(h["title"])
                if (m.get("urgency") or 0) >= r["headline_urgency"]:
                    hot.append((m["urgency"], h["title"]))
        if hot:
            hot.sort(reverse=True)
            why.append(f"{len(hot)} new headline(s) with urgency >= {r['headline_urgency']} since the last guidance "
                       f"(top: u{hot[0][0]} {brief._cut(hot[0][1], 60)})")

        why += alignment_changes(root, now, p)

    ev = brief.upcoming_events(root, now.date(), r["event_lookahead_days"])
    for e in ev:
        when = "today" if e["date"] == now.date() else "tomorrow"
        why.append(f"event {e['type']} {when}")

    pv = brief.portfolio_view(root, p, now)
    for pos in pv["positions"]:
        if pos["stop"] and pos["last"] > 0 and (pos["last"] - pos["stop"]) / pos["last"] <= r["stop_proximity_pct"]:
            why.append(f"{pos['symbol']} is within {r['stop_proximity_pct'] * 100:.0f}% of its stop "
                       f"({pos['last']:.2f} vs {pos['stop']:.2f})")
    if pv["cushion"] < r["cushion_below"]:
        why.append(f"cushion {brief._inr(pv['cushion'])} is under {brief._inr(r['cushion_below'])}")
    return why


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="always think")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    a = ap.parse_args(argv)
    utf8_stdout()
    why = evaluate(Path(a.root), parse_now(a.now), a.force)
    if why:
        print("THINK: " + "; ".join(why))
        return 0
    print("SKIP: guidance is fresh, prices are quiet, no new urgent headlines, no event today or tomorrow, "
          "stops and cushion are comfortable")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
