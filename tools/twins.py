#!/usr/bin/env python3
"""Shadow paper-twin comparison (READ-ONLY: it writes nothing, moves nothing, and never touches the engine's files). Paper trading only.

  python tools/twins.py show                 # every twin since its own start (state/twins.json)
  python tools/twins.py summary [--days N]   # the latest N dated snapshots (logs/twins-YYYY-MM.csv), default 20, with the fairness tables

Columns: value (Rs), return, max drawdown from the running peak, % of days green (in `show`: value at or above the twin's own starting value;
in `summary`: at or above its value at the START OF THE WINDOW), the difference to the real book T0 over the same span (percentage points; T0 is
measured from the twin's own start, "-" when not comparable), the Rs 75,000 account stop as a % of that twin's start (the same rupee stop is a
different distance for each start value), and the current mix G/S/C/N/cash in percent.

`summary` adds: every twin from ONE common start date; gross vs net of fees with turnover and the first day's entry loss; and daily PAIRED
differences against T1 and T5 (mean, std, t-stat, block-bootstrap interval) with a power note. A handful of days cannot rank the twins.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold import config, twins


def _pct(x, signed: bool = False, nd: int = 1) -> str:
    return "-" if x is None else (f"{x * 100:+.{nd}f}" if signed else f"{x * 100:.{nd}f}")


def _mix(w: dict) -> str:
    return "/".join(f"{100 * w[k]:.0f}" for k in ("G", "S", "C", "N", "cash"))


def _fmt(head: list[str], body: list[list[str]], left=(0, 1)) -> list[str]:
    widths = [max(len(str(x[i])) for x in [head] + body) for i in range(len(head))]
    f = lambda r: "  ".join(str(c).ljust(widths[i]) if i in left else str(c).rjust(widths[i]) for i, c in enumerate(r)).rstrip()  # noqa: E731
    return [f(head)] + [f(b) for b in body]


def _table(rows: list[dict], title: str, green_label: str) -> str:
    head = ["id", "twin", "value", "ret%", "maxDD%", green_label, "vs real pp", "stop%", "mix G/S/C/N/cash", "note"]
    body = [[r["id"], r["label"], f"{r['value']:,.0f}", _pct(r["ret"], True), _pct(r["max_dd"]), _pct(r["green"]), _pct(r["vs_real"], True),
             _pct(r.get("stop"), True, 0), _mix(r["w"]), r["note"]] for r in rows]
    return "\n".join([title] + _fmt(head, body, left=(0, 1, 8, 9)))


def _state(root: Path):
    """(state, message): read WITHOUT quarantining anything; an unusable file is reported and left exactly where it is."""
    try:
        st, why = twins.inspect_state(root / "state" / twins.STATE_NAME)
    except twins.TwinsError as e:
        return {}, str(e)
    if why:
        return {}, f"state/twins.json is not usable ({why}); left untouched"
    return st, ""


def _stop_level(state: dict) -> float:
    v = state.get("account_stop")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    try:
        return float((config.load_params().get("strategy") or {}).get("account_stop") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def show(root: Path) -> str:
    state, msg = _state(root)
    if msg:
        return msg
    books = state.get("books") or {}
    if not books:
        return "No twin data yet (twins.enabled is false, or no 15:10 check has run since it was switched on)."
    px, stop = state.get("px") or {}, _stop_level(state)
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
        if t0 and tid != "T0" and b["real_start_value"] > 0:
            vs = ret - (t0["value"] / b["real_start_value"] - 1.0)
        note = "latched at the account stop" if b.get("latched") else ""
        out.append({"id": tid, "label": twins.LABELS[tid], "value": b["value"], "ret": ret, "max_dd": b["max_dd"],
                    "green": b["days_green"] / b["days"] if b["days"] else None, "vs_real": vs,
                    "stop": (stop / b["start_value"] - 1.0) if stop and tid != "T0" else None,
                    "w": {"G": w[twins.GOLD], "S": w[twins.SILVER], "C": w[twins.COPPER], "N": w[twins.NIFTY], "cash": w["cash"]}, "note": note})
    nstat = (state.get("nifty") or {}).get("status", "")
    title = (f"Shadow twins (paper, never trade) | {state.get('start_day', '?')} to {state.get('last_day', '?')} | {state.get('days', 0)} checks"
             f" | NIFTYBEES feed: {nstat or 'n/a'} | each twin since its OWN start")
    return _table(out, title, "green%(own start)")


def _rowdict(tid: str, d: dict, stop: float, start_value: float | None) -> dict:
    return {"id": tid, "label": twins.LABELS[tid], "value": d["value"], "ret": d["ret"], "max_dd": d["max_dd"], "green": d["green"],
            "vs_real": d["vs_real"], "note": d["note"], "stop": (stop / start_value - 1.0) if (stop and start_value and tid != "T0") else None,
            "w": {"G": d["w"]["w_GOLDBEES"], "S": d["w"]["w_SILVERBEES"], "C": d["w"]["w_HINDCOPPER"], "N": d["w"]["w_NIFTYBEES"],
                  "cash": d["w"]["w_cash"]}}


def _common_section(rows: list[dict]) -> list[str]:
    c = twins.summarize_common(rows)
    if not c["twins"]:
        return ["", "Common start: not enough dated snapshots since the latest twin started."]
    body = [[tid, twins.LABELS[tid], f"{d['value']:,.0f}", _pct(d["ret"], True), _pct(d["max_dd"]), _pct(d["vs_real"], True), str(d["rows"])]
            for tid, d in ((t, c["twins"][t]) for t in twins.TWINS if t in c["twins"])]
    return ["", f"Common start {c['date']} (every twin from the same close; the fair ranking, but few days)"] + \
        _fmt(["id", "twin", "value", "ret%", "maxDD%", "vs real pp", "days"], body)


def _cost_section(state: dict) -> list[str]:
    ct = twins.cost_table(state)
    if not ct:
        return []
    body = [[tid, twins.LABELS[tid], _pct(d["gross"], True, 2), _pct(d["net"], True, 2), f"{d['fees']:,.0f}", f"{d['traded_x']:.2f}",
             f"{d['entry_loss']:,.0f} ({100 * d['entry_loss'] / d['start_value']:.2f}%)"]
            for tid, d in ((t, ct[t]) for t in twins.TWINS if t in ct)]
    return ["", "Gross vs net since each twin's own start (fees added back; slippage stays in both) | traded = bought+sold value / start value |"
            " day-1 entry loss = fees + slippage paid to build the book, shown here, not hidden in the returns",
            *_fmt(["id", "twin", "gross%", "net%", "fees Rs", "traded x", "day-1 entry loss"], body)]


def _paired_section(allrows: list[dict], window_dates: set[str]) -> list[str]:
    rets = twins.daily_returns(allrows)
    rets = {t: {d: r for d, r in m.items() if d in window_dates} for t, m in rets.items()}
    out: list[str] = []
    nmax = 0
    for ref in ("T1", "T5"):
        body = []
        for tid in twins.TWINS:
            if tid == ref:
                continue
            ps = twins.paired_stats(rets, tid, ref)
            if ps is None:
                continue
            nmax = max(nmax, ps["n"])
            if ps["mean"] is None:
                body.append([tid, str(ps["n"]), "-", "-", "-", "-", "-"])
                continue
            ci = ps["ci"]
            body.append([tid, str(ps["n"]), f"{ps['mean'] * 1e4:+.1f}", f"{ps['std'] * 1e4:.1f}", "-" if ps["t"] is None else f"{ps['t']:+.2f}",
                         "n/a (under 2 blocks)" if ci is None else f"{ci[0] * 1e4:+.1f}..{ci[1] * 1e4:+.1f}", f"{ps['mde_year_pp']:.0f}"])
        if body:
            out += ["", f"Daily paired difference vs {ref} ({twins.LABELS[ref]}): twin minus {ref}, basis points a day",
                    *_fmt(["id", "n", "mean bp", "std bp", "t", "95% block-bootstrap CI bp", "min detectable pp/yr"], body, left=(0,))]
    if out:
        out += ["", f"Power: with n={nmax} paired days a difference must exceed the 'min detectable' column (about 2 standard errors, in pp a year) to "
                    "be told from noise; 10 days cannot rank twins, a few months only separate large differences, and a metals bull or bear "
                    "is one regime, not many. Day-1 entry costs and the ramp-in are in the first days of every twin's series."]
    return out


def summary(root: Path, days: int) -> str:
    rows = twins.read_rows(root / "logs")
    if not rows:
        return "No twin snapshots yet (logs/twins-*.csv)."
    state, _msg = _state(root)
    stop = _stop_level(state)
    sv = {tid: b["start_value"] for tid, b in (state.get("books") or {}).items()}
    sm = twins.summarize(rows, days)
    out = [_rowdict(tid, sm[tid], stop, sv.get(tid)) for tid in twins.TWINS if tid in sm]
    first = min(d["from"] for d in sm.values())
    last = max(d["to"] for d in sm.values())
    lines = [_table(out, f"Shadow twins (paper, never trade) | latest {days} dated snapshots: {first} to {last} | returns from the last close before "
                         f"the window (a twin that started inside it: its first close)", "green%(window start)")]
    window = {r["date"] for r in rows if first <= r["date"] <= last}
    lines += _common_section(rows)
    lines += _cost_section(state)
    lines += _paired_section(rows, window)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("show", help="every twin since its own start (with --days N: the same as summary)")
    sh.add_argument("--days", type=int, default=None)
    sm = sub.add_parser("summary", help="the latest N dated snapshots, with the fairness tables")
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
