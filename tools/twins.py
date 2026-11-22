#!/usr/bin/env python3
"""Shadow paper-twin comparison (READ-ONLY: it writes nothing and never touches the engine's files). Paper trading only.

  python tools/twins.py show                 # every twin since its own start (state/twins.json)
  python tools/twins.py summary [--days N]   # the latest N dated snapshots (logs/twins-YYYY-MM.csv), default 20

Columns: value (Rs), return (since start / over the window), max drawdown from the running peak, % of days green (value at or above the
twin's starting value), difference to the real book T0 over the same period (percentage points; "-" when not comparable), and the
current mix G/S/C/N/cash in percent. The twins never trade; see simgold/twins.py for the rules of each one.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold import twins


def _pct(x, signed: bool = False) -> str:
    return "-" if x is None else (f"{x * 100:+.1f}" if signed else f"{x * 100:.1f}")


def _mix(w: dict) -> str:
    return "/".join(f"{100 * w[k]:.0f}" for k in ("G", "S", "C", "N", "cash"))


def _table(rows: list[dict], title: str) -> str:
    head = ["id", "twin", "value", "ret%", "maxDD%", "green%", "vs real pp", "mix G/S/C/N/cash", "note"]
    body = [[r["id"], r["label"], f"{r['value']:,.0f}", _pct(r["ret"], True), _pct(r["max_dd"]), _pct(r["green"]),
             _pct(r["vs_real"], True), _mix(r["w"]), r["note"]] for r in rows]
    widths = [max(len(str(x[i])) for x in [head] + body) for i in range(len(head))]
    fmt = lambda r: "  ".join(str(c).ljust(widths[i]) if i in (0, 1, 7, 8) else str(c).rjust(widths[i]) for i, c in enumerate(r)).rstrip()  # noqa: E731
    return "\n".join([title, fmt(head)] + [fmt(b) for b in body])


def show(root: Path) -> str:
    state = {}
    try:
        state = twins.load_state(root / "state" / twins.STATE_NAME)
    except twins.TwinsError as e:
        return str(e)
    books = state.get("books") or {}
    if not books:
        return "No twin data yet (twins.enabled is false, or no 15:10 check has run since it was switched on)."
    px = state.get("px") or {}
    t0 = books.get("T0")
    out = []
    for tid in twins.TWINS:
        b = books.get(tid)
        if b is None:
            out.append({"id": tid, "label": twins.LABELS[tid], "value": 0.0, "ret": None, "max_dd": None, "green": None, "vs_real": None,
                        "w": {k: 0.0 for k in ("G", "S", "C", "N", "cash")}, "note": "n/a (waiting for a NIFTYBEES price)"})
            continue
        w = twins.weights_of(b, px)
        ret = b["value"] / b["start_value"] - 1.0
        vs = None
        if t0 and tid != "T0" and t0["real_start_value"] > 0:
            vs = ret - (t0["value"] / b["real_start_value"] - 1.0)
        note = "latched at the account stop" if b.get("latched") else ""
        out.append({"id": tid, "label": twins.LABELS[tid], "value": b["value"], "ret": ret, "max_dd": b["max_dd"],
                    "green": b["days_green"] / b["days"] if b["days"] else None, "vs_real": vs,
                    "w": {"G": w[twins.GOLD], "S": w[twins.SILVER], "C": w[twins.COPPER], "N": w[twins.NIFTY], "cash": w["cash"]},
                    "note": note})
    nstat = (state.get("nifty") or {}).get("status", "")
    title = (f"Shadow twins (paper, never trade) | {state.get('start_day', '?')} to {state.get('last_day', '?')} | {state.get('days', 0)} checks"
             f" | NIFTYBEES feed: {nstat or 'n/a'}")
    return _table(out, title)


def summary(root: Path, days: int) -> str:
    rows = twins.read_rows(root / "logs")
    if not rows:
        return "No twin snapshots yet (logs/twins-*.csv)."
    sm = twins.summarize(rows, days)
    out = []
    for tid in twins.TWINS:
        d = sm.get(tid)
        if d is None:
            continue
        out.append({"id": tid, "label": twins.LABELS[tid], "value": d["value"], "ret": d["ret"], "max_dd": d["max_dd"], "green": d["green"],
                    "vs_real": d["vs_real"], "note": d["note"], "w": {"G": d["w"]["w_GOLDBEES"], "S": d["w"]["w_SILVERBEES"],
                    "C": d["w"]["w_HINDCOPPER"], "N": d["w"]["w_NIFTYBEES"], "cash": d["w"]["w_cash"]}})
    first = min(d["from"] for d in sm.values())
    last = max(d["to"] for d in sm.values())
    return _table(out, f"Shadow twins (paper, never trade) | latest {days} dated snapshots: {first} to {last}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("show", help="every twin since its own start (with --days N: the same as summary)")
    sh.add_argument("--days", type=int, default=None)
    sm = sub.add_parser("summary", help="the latest N dated snapshots")
    sm.add_argument("--days", type=int, default=20)
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root)
    if a.cmd == "show" and a.days is None:
        print(show(root))
    else:
        print(summary(root, max(1, a.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
