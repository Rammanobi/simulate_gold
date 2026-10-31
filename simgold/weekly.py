"""Weekly "rules vs live" line: what the same v2 rules would have done this ISO week without AI blocks, pauses or outages.

    This week: live account +X% · rules alone (backtest of the same rules on the same days, no AI blocks/pauses) +Y%
    · GOLDBEES buy-and-hold +Z%

The rules run (simgold/backtest.py run_v2) starts from the live book at the previous week's close (positions rebuilt from the
trade list, the rest is cash) and replays the week's daily bars, so the gap to the live account is what blocks, pauses and
outages cost (or saved). Paper money only; read-only. Everything here is best effort: any missing input -> None (the line is
simply left out) and nothing ever raises into a report.
"""
from __future__ import annotations

import copy
import csv
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from . import config
from .market import is_trading_day

log = logging.getLogger(__name__)
HISTORY_DAYS = 800          # calendar days of bars before the week, for the 200-day / 12-month signals


def week_bounds(d: date) -> tuple[date, date]:
    """Monday and Sunday of the ISO week of d."""
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=6)


def is_last_trading_day_of_week(d: date) -> bool:
    if not is_trading_day(d):
        return False
    _, sun = week_bounds(d)
    return not any(is_trading_day(d + timedelta(days=i)) for i in range(1, (sun - d).days + 1))


def fetch_frames(p: dict, start: str):
    """Daily {"close","high","low"} bars for the three symbols (Yahoo). Tests replace this; it may raise."""
    from .backtest import yahoo_frames
    return yahoo_frames(p, start)


def _equity(root: Path) -> list[tuple[date, float]]:
    out: dict[date, float] = {}
    try:
        for row in csv.reader((root / "state" / "equity.csv").read_text(encoding="utf-8").splitlines()):
            try:
                out[date.fromisoformat(row[0].strip()[:10])] = float(row[1])
            except (ValueError, IndexError):
                continue
    except OSError:
        pass
    return sorted(out.items())


def _trades(root: Path) -> tuple[list[dict], dict]:
    from .summary import merge_trades, read_archive
    try:
        pf = json.loads((root / "state" / "portfolio.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pf = {}
    pf = pf if isinstance(pf, dict) else {}
    return merge_trades(read_archive(root / "logs" / "trades-archive.jsonl"), pf.get("trades")), pf


def _qty_at(trades: list[dict], pf: dict, syms, base: date) -> dict[str, int] | None:
    """Units held at the close of `base`, replayed from the trade list. None when the list does not add up to today's book."""
    now: dict[str, int] = {s: 0 for s in syms}
    at: dict[str, int] = {s: 0 for s in syms}
    for t in trades:
        a, q = t.get("asset"), t.get("qty")
        if a not in now or not isinstance(q, (int, float)) or t.get("side") not in ("BUY", "SELL"):
            continue
        sign = 1 if t["side"] == "BUY" else -1
        now[a] += sign * int(q)
        try:
            if date.fromisoformat(str(t.get("ts"))[:10]) <= base:
                at[a] += sign * int(q)
        except ValueError:
            return None
    held = {s: int(((pf.get("positions") or {}).get(s) or {}).get("qty") or 0) for s in syms}
    if now != held or any(v < 0 for v in at.values()):
        return None
    return at


def rules_vs_live(root, d: date, frames=None, params: dict | None = None) -> dict | None:
    """Percent change over the ISO week of d (up to d): live account, the rules alone from the same opening book, and
    GOLDBEES buy-and-hold. None whenever any piece of data is missing."""
    try:
        import pandas as pd
        from .backtest import run_v2
        root = Path(root)
        p = copy.deepcopy(params if params is not None else config.load_params())
        if (p.get("strategy") or {}).get("mode") != "v2_trend":
            return None
        mon, _ = week_bounds(d)
        eq = _equity(root)
        prior = [x for x in eq if x[0] < mon]
        cur = [x for x in eq if mon <= x[0] <= d]
        if not prior or not cur:
            return None
        (b_d, b_v), (e_d, e_v) = prior[-1], cur[-1]
        if b_v <= 0 or e_d <= b_d:
            return None
        if frames is None:
            frames = fetch_frames(p, (b_d - timedelta(days=HISTORY_DAYS)).isoformat())
        syms = list(p["strategy"]["symbols"])
        close = frames["close"]
        bts, ets = pd.Timestamp(b_d), pd.Timestamp(e_d)
        if bts not in close.index or ets not in close.index or any(s not in close for s in syms):
            return None
        if close.loc[[bts, ets], syms].isna().any().any():
            return None
        trades, pf = _trades(root)
        qty = _qty_at(trades, pf, syms, b_d)
        if qty is None:
            return None
        hold = {s: qty[s] * float(close.at[bts, s]) for s in syms}
        cash = b_v - sum(hold.values())
        if cash < -0.01 * b_v:
            return None
        if config.idle_is_cash(p):
            p["capital"]["liquid_yield_annual"] = 0.0           # idle money earns nothing since 22 Oct 2026
        res = run_v2(frames, p, bts, ets, init={"cash": max(cash, 0.0), "hold": hold})
        eqs = res["equity"]
        if ets not in eqs.index or bts not in eqs.index:
            return None
        gold = float(close.at[ets, "GOLDBEES"]) / float(close.at[bts, "GOLDBEES"]) - 1
        out = {"week": f"{mon.isocalendar()[0]}-W{mon.isocalendar()[1]:02d}", "from": b_d.isoformat(), "to": e_d.isoformat(),
               "live_pct": (e_v / b_v - 1) * 100, "rules_pct": (float(eqs[ets]) / float(eqs[bts]) - 1) * 100,
               "goldbees_pct": gold * 100}
        return out if all(v == v and abs(v) != float("inf") for k, v in out.items() if k.endswith("_pct")) else None
    except Exception as e:      # noqa: BLE001 - an extra line must never break a report or a scorecard
        log.warning("weekly rules-vs-live skipped: %s", e)
        return None


def line(r: dict | None) -> str | None:
    if not r:
        return None
    return (f"This week: live account {r['live_pct']:+.2f}% · rules alone (backtest of the same rules on the same days, "
            f"no AI blocks/pauses) {r['rules_pct']:+.2f}% · GOLDBEES buy-and-hold {r['goldbees_pct']:+.2f}%")


def week_line(root, d: date, frames=None, params: dict | None = None) -> str | None:
    return line(rules_vs_live(root, d, frames, params))
