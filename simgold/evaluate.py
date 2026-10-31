"""Monthly / since-inception performance evaluation (paper trading, read-only, no AI).

Inputs (all under ``root``): state/equity.csv (date,value), logs/decisions-*.jsonl, logs/ticks-DATE.csv
(last row per day = the day's ETF closes; every row feeds time-in-market), state/portfolio.json, and an optional
benchmark CSV (``date,GOLDBEES,SILVERBEES``) that adds to or overrides the recorded closes.
"""
from __future__ import annotations

import csv
import html
import json
from datetime import date, timedelta
from pathlib import Path

from . import config

START_CAPITAL = 100000.0
CASH_RATE = 0.065          # all-cash LIQUIDBEES benchmark, per year, pro rata per calendar day
LIQUID_YIELD = 0.063       # engine's modelled LIQUIDBEES yield (config/params.yaml), for the income estimate
MIN_ANNUALISE_DAYS = 90    # under ~3 months a CAGR is not meaningful
SYMS = ("GOLDBEES", "SILVERBEES", "HINDCOPPER")


def _d(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _r(x, n=2):
    return None if x is None else round(x, n)


# ----------------------------------------------------------------------------------------------- loading
def load_equity(root: Path) -> list[tuple[date, float]]:
    out: dict[date, float] = {}
    p = Path(root) / "state" / "equity.csv"
    if p.exists():
        for row in csv.reader(p.read_text(encoding="utf-8").splitlines()):
            if len(row) >= 2:
                try:
                    v = _f(row[1])
                    if v is not None:
                        out[_d(row[0])] = v
                except ValueError:
                    continue   # header or junk line
    return sorted(out.items())


def load_ticks(root: Path) -> dict[date, list[dict]]:
    res: dict[date, list[dict]] = {}
    for p in sorted((Path(root) / "logs").glob("ticks-*.csv")):
        try:
            d = _d(p.stem.split("ticks-", 1)[1])
            with p.open(encoding="utf-8", newline="") as fh:
                rows = [r for r in csv.DictReader(fh)]
        except (ValueError, OSError):
            continue
        if rows:
            res[d] = rows
    return res


def closes_from_ticks(ticks: dict[date, list[dict]]) -> dict[date, dict[str, float]]:
    out: dict[date, dict[str, float]] = {}
    for d, rows in ticks.items():
        got: dict[str, float] = {}
        for r in rows:                      # last valid value per symbol wins
            for s in SYMS:
                v = _f(r.get(s))
                if v and v > 0:
                    got[s] = v
        if got:
            out[d] = got
    return out


def load_benchmark_csv(path) -> dict[date, dict[str, float]]:
    out: dict[date, dict[str, float]] = {}
    if not path:
        return out
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                d = _d(r.get("date", ""))
            except ValueError:
                continue
            for s in SYMS:
                v = _f(r.get(s))
                if v and v > 0:
                    out.setdefault(d, {})[s] = v
    return out


def load_decisions(root: Path) -> list[dict]:
    seen, out = set(), []
    for p in sorted((Path(root) / "logs").glob("decisions-*.jsonl")):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("action") not in ("BUY", "SELL"):
                continue
            key = (r.get("ts"), r.get("action"), r.get("symbol"), r.get("qty"))
            if key in seen:
                continue
            seen.add(key)
            try:
                r["_date"] = _d(r.get("ts") or p.stem.split("decisions-", 1)[1])
            except (ValueError, IndexError):
                continue
            out.append(r)
    return out


def load_portfolio(root: Path) -> dict:
    try:
        d = json.loads((Path(root) / "state" / "portfolio.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# ----------------------------------------------------------------------------------------------- maths
def month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def at_or_before(series: list[tuple[date, float]], d: date):
    best = None
    for k, v in series:
        if k <= d:
            best = (k, v)
        else:
            break
    return best


def drawdown(points: list[tuple[date, float]]) -> dict:
    """Largest peak-to-trough fall of a series of (date, value)."""
    peak = None
    best = {"rupees": 0.0, "pct": 0.0, "peak_date": None, "trough_date": None}
    for d, v in points:
        if peak is None or v > peak[1]:
            peak = (d, v)
        fall = peak[1] - v
        if fall > best["rupees"] + 1e-9:
            best = {"rupees": fall, "pct": fall / peak[1] * 100 if peak[1] else 0.0,
                    "peak_date": peak[0].isoformat(), "trough_date": d.isoformat()}
    return {"rupees": round(best["rupees"], 2), "pct": round(best["pct"], 3),
            "peak_date": best["peak_date"], "trough_date": best["trough_date"]}


def cagr(start: float, end: float, days: int):
    if start <= 0 or days <= 0 or end <= 0:
        return None
    return ((end / start) ** (365.0 / days) - 1) * 100


def trade_stats(decs: list[dict]) -> dict:
    buys = [x for x in decs if x["action"] == "BUY"]
    sells = [x for x in decs if x["action"] == "SELL"]
    pn = [float(x["pnl"]) for x in sells if _f(x.get("pnl")) is not None]
    wins, losses = [p for p in pn if p > 0], [p for p in pn if p < 0]
    gw, gl = sum(wins), -sum(losses)
    return {
        "buys": len(buys), "sells": len(sells), "trades": len(buys) + len(sells), "closed": len(pn),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": _r(len(wins) / len(pn) * 100, 1) if pn else None,
        "avg_win": _r(gw / len(wins)) if wins else None,
        "avg_loss": _r(-gl / len(losses)) if losses else None,
        "profit_factor": _r(gw / gl, 2) if gl > 0 else None,
        "profit_factor_note": ("no losing trades" if gl == 0 and wins else
                               "no closed trades" if not pn else None),
        "realised_pnl": _r(sum(pn)),
    }


def time_in_market(ticks: dict[date, list[dict]], start: date, end: date) -> dict:
    per_day = []
    for d, rows in sorted(ticks.items()):
        if not (start <= d <= end):
            continue
        fr = []
        for r in rows:
            v, c, lq = _f(r.get("value")), _f(r.get("cash")), _f(r.get("liquidbees"))
            if v and v > 0 and c is not None and lq is not None:
                fr.append(min(1.0, max(0.0, 1 - (c + lq) / v)))
        if fr:
            per_day.append(sum(fr) / len(fr))
    return {"avg_invested_pct": _r(sum(per_day) / len(per_day) * 100, 2) if per_day else None,
            "days_measured": len(per_day)}


def liquid_income(ticks, equity_dates: list[date], start: date, end: date) -> float:
    """Estimate: previous close's LIQUIDBEES balance x yield/365 x calendar days since the previous equity row."""
    tot = 0.0
    for prev, cur in zip(equity_dates, equity_dates[1:]):
        if not (start < cur <= end) or prev not in ticks:
            continue
        lq = _f(ticks[prev][-1].get("liquidbees"))
        if lq:
            tot += lq * LIQUID_YIELD / 365 * (cur - prev).days
    return tot


def _bench(name, closes_a, closes_b, start_d, end_d, closes) -> dict:
    """Buy-and-hold over [start_d, end_d] from recorded closes. closes_a/b: symbols (b optional, 50/50 mix)."""
    syms = [s for s in (closes_a, closes_b) if s]
    days = sorted(d for d, c in closes.items() if all(s in c for s in syms))
    if not days:
        return {"name": name, "available": False, "note": "no closes recorded"}
    base = [d for d in days if d <= start_d]
    b0 = base[-1] if base else next((d for d in days if d >= start_d), None)
    inwin = [d for d in days if b0 and b0 <= d <= end_d]
    if b0 is None or len(inwin) < 1:
        return {"name": name, "available": False, "note": "no closes in period"}
    e0 = inwin[-1]
    w = 1.0 / len(syms)
    curve = [(d, sum(w * closes[d][s] / closes[b0][s] for s in syms) * START_CAPITAL) for d in inwin]
    ret = curve[-1][1] / START_CAPITAL - 1
    return {"name": name, "available": True, "start_date": b0.isoformat(), "end_date": e0.isoformat(),
            "return_pct": round(ret * 100, 3), "max_drawdown_pct": drawdown(curve)["pct"],
            "stale_days": (end_d - e0).days if end_d > e0 else 0, "points": len(inwin)}


def _verdict(ret_pct, dd_pct, gold: dict, cash: dict, green: bool) -> str:
    if not gold.get("available"):
        return f"Return {ret_pct:+.2f}%; no gold benchmark data to compare against."
    ex = gold.get("excess_pct", ret_pct - gold["return_pct"])
    s = (f"Beat gold buy-and-hold by {ex:+.2f}%" if ex >= 0 else f"Lagged gold buy-and-hold by {-ex:.2f}%")
    gd = gold["max_drawdown_pct"]
    if gd > 0:
        if dd_pct <= 0.55 * gd:
            s += " with half its drawdown" if dd_pct > 0.2 * gd else " with a fraction of its drawdown"
        elif dd_pct < gd:
            s += " with a smaller drawdown"
        elif dd_pct > 1.05 * gd:
            s += " with a deeper drawdown"
        else:
            s += " with a similar drawdown"
    elif dd_pct > 0.0:
        s += " but with a drawdown gold did not have"
    if cash.get("available") and ret_pct < cash["return_pct"]:
        s += f"; below the {'return idle money would have earned in LIQUIDBEES' if config.idle_is_cash() else 'cash (LIQUIDBEES) return'} of {cash['return_pct']:+.2f}%"
    s += "." if green else "; ended below the Rs 1,00,000 line."
    return s


# ----------------------------------------------------------------------------------------------- main
def evaluate(root, month: str | None = None, benchmark_csv=None, weekly: bool = False) -> dict:
    """month=None -> since inception. weekly=True adds the "rules vs live" line for the latest week in the period (needs price data
    from the network; left out when anything is missing)."""
    root = Path(root)
    eq = load_equity(root)
    ticks = load_ticks(root)
    closes = closes_from_ticks(ticks)
    for d, c in load_benchmark_csv(benchmark_csv).items():
        closes.setdefault(d, {}).update(c)
    decs = load_decisions(root)
    pf = load_portfolio(root)
    flags: list[str] = []
    res: dict = {"period": month or "since inception", "paper_trading": True}
    if not eq:
        res.update(available=False, note="no equity data")
        return res

    if month:
        first, last = month_bounds(month)
        prior = [(d, v) for d, v in eq if d < first]
        rows = [(d, v) for d, v in eq if first <= d <= last]
        if not rows:
            res.update(available=False, note=f"no equity rows in {month}")
            return res
        base = prior[-1] if prior else rows[0]
        if not prior:
            flags.append("first month: baseline is the first recorded equity row")
        pts = [base] + [x for x in rows if x[0] != base[0]]
        p_start, p_end = first, last
    else:
        base = eq[0]
        pts = list(eq)
        p_start, p_end = eq[0][0], eq[-1][0]
    start_d, end_d = base[0], pts[-1][0]
    start_v, end_v = base[1], pts[-1][1]
    days = (end_d - start_d).days
    tot = end_v - start_v
    ret = tot / start_v * 100 if start_v else 0.0

    # daily changes
    chg = [(b[0], (b[1] / a[1] - 1) * 100, b[1] - a[1]) for a, b in zip(pts, pts[1:]) if a[1]]
    best = max(chg, key=lambda x: x[2]) if chg else None
    worst = min(chg, key=lambda x: x[2]) if chg else None
    pos = sum(1 for c in chg if c[2] > 0)
    dd = drawdown(pts)

    ann = cagr(start_v, end_v, days)
    short = days < MIN_ANNUALISE_DAYS
    if short:
        flags.append(f"under 3 months of data ({days} days): CAGR is indicative only, do not rely on it")

    in_p = [x for x in decs if p_start <= x["_date"] <= p_end]
    ts = trade_stats(in_p)

    tr = [t for t in (pf.get("trades") or []) if isinstance(t, dict)]
    costs = 0.0
    for t in tr:
        try:
            if p_start <= _d(t.get("ts", "")) <= p_end:
                costs += float(t.get("costs") or 0)
        except ValueError:
            continue
    costs_note = None
    if not month and not tr and _f(pf.get("total_costs")):
        costs, costs_note = float(pf["total_costs"]), "from portfolio total_costs"
    if not tr and ts["trades"]:
        flags.append("trade costs unavailable (portfolio.trades empty)")

    tim = time_in_market(ticks, p_start, p_end)
    liq = liquid_income(ticks, [d for d, _ in eq], start_d, end_d)
    share = round(liq / tot * 100, 1) if tot > 0 else None

    missing = [d.isoformat() for d, _ in pts[1:] if d not in closes]
    if missing:
        flags.append(f"{len(missing)} equity day(s) without recorded closes: {', '.join(missing[:5])}"
                     + (" ..." if len(missing) > 5 else ""))

    gold = _bench("Buy-and-hold GOLDBEES", "GOLDBEES", None, start_d, end_d, closes)
    silver = _bench("Buy-and-hold SILVERBEES", "SILVERBEES", None, start_d, end_d, closes)
    mix = _bench("50/50 gold-silver", "GOLDBEES", "SILVERBEES", start_d, end_d, closes)
    cash_ret = CASH_RATE * days / 365 * 100
    cash = {"name": ("Idle money in LIQUIDBEES instead (6.5%/yr)" if config.idle_is_cash() else "All-cash LIQUIDBEES (6.5%/yr)"), "available": days > 0, "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(), "return_pct": round(cash_ret, 3), "max_drawdown_pct": 0.0}
    benches = [gold, silver, mix, cash]
    for b in benches:
        if not b["available"]:
            flags.append(f"benchmark unavailable: {b['name']} ({b['note']})")
            continue
        # compare against the strategy over the benchmark's own window
        s0 = at_or_before(eq, date.fromisoformat(b["start_date"]))
        s1 = at_or_before(eq, date.fromisoformat(b["end_date"]))
        sr = (s1[1] / s0[1] - 1) * 100 if s0 and s1 and s0[1] else ret
        b["strategy_return_pct"] = round(sr, 3)
        b["excess_pct"] = round(sr - b["return_pct"], 3)
        if b.get("stale_days", 0) > 3:
            flags.append(f"{b['name']}: last close is {b['stale_days']} days before period end")

    green = end_v >= START_CAPITAL
    res.update({
        "available": True, "period_start": start_d.isoformat(), "period_end": end_d.isoformat(), "days": days,
        "trading_days": len(chg), "start_value": round(start_v, 2), "end_value": round(end_v, 2),
        "return_rupees": round(tot, 2), "return_pct": round(ret, 3), "green": green,
        "cagr_pct": _r(ann, 2), "cagr_reliable": not short,
        "best_day": {"date": best[0].isoformat(), "pct": round(best[1], 3), "rupees": round(best[2], 2)} if best else None,
        "worst_day": {"date": worst[0].isoformat(), "pct": round(worst[1], 3), "rupees": round(worst[2], 2)} if worst else None,
        "positive_days_pct": _r(pos / len(chg) * 100, 1) if chg else None,
        "max_drawdown": dd, "time_in_market": tim, "trades": ts,
        "costs_paid": round(costs, 2), "liquidbees_income_est": round(liq, 2), "liquidbees_income_share_pct": share,
        "benchmarks": benches, "flags": flags,
        "verdict": _verdict(ret, dd["pct"], gold, cash, green),
    })
    if weekly:
        from . import weekly as _wk
        wl = _wk.week_line(root, end_d)
        if wl:
            res["weekly_line"] = wl
    return res


# ----------------------------------------------------------------------------------------------- output
def _rs(x):
    if x is None:
        return "n/a"
    s = f"{abs(x):,.2f}"
    return ("-" if x < 0 else "") + "Rs " + s


def _pc(x, plus=True):
    return "n/a" if x is None else (f"{x:+.2f}%" if plus else f"{x:.2f}%")


def rows(r: dict) -> list[tuple[str, str]]:
    if not r.get("available"):
        return [("Status", r.get("note", "no data"))]
    t, dd, tm = r["trades"], r["max_drawdown"], r["time_in_market"]
    cg = _pc(r["cagr_pct"]) + ("" if r["cagr_reliable"] else " (under 3 months: indicative)")
    pf = t["profit_factor"] if t["profit_factor"] is not None else (t["profit_factor_note"] or "n/a")
    out = [
        ("Period", f"{r['period_start']} to {r['period_end']} ({r['trading_days']} trading days)"),
        ("Start / end value", f"{_rs(r['start_value'])} / {_rs(r['end_value'])} ({'green' if r['green'] else 'RED'})"),
        ("Total return", f"{_rs(r['return_rupees'])} ({_pc(r['return_pct'])})"),
        ("CAGR", cg),
        ("Best day", f"{_pc(r['best_day']['pct'])} on {r['best_day']['date']}" if r["best_day"] else "n/a"),
        ("Worst day", f"{_pc(r['worst_day']['pct'])} on {r['worst_day']['date']}" if r["worst_day"] else "n/a"),
        ("Positive days", f"{r['positive_days_pct']}%" if r["positive_days_pct"] is not None else "n/a"),
        ("Max drawdown", f"{_rs(dd['rupees'])} ({dd['pct']:.2f}%)"
         + (f", {dd['peak_date']} to {dd['trough_date']}" if dd["peak_date"] else "")),
        ("Time in market", f"{tm['avg_invested_pct']}% of capital" if tm["avg_invested_pct"] is not None else "n/a"),
        ("Trades", f"{t['trades']} ({t['buys']} buys, {t['sells']} sells)"),
        ("Win rate", f"{t['win_rate_pct']}% of {t['closed']} closed" if t["win_rate_pct"] is not None else "n/a"),
        ("Avg win / avg loss", f"{_rs(t['avg_win'])} / {_rs(t['avg_loss'])}"),
        ("Profit factor", str(pf)),
        ("Costs paid", _rs(r["costs_paid"])),
        ("LIQUIDBEES income (before the switch to cash)" if config.idle_is_cash() else "LIQUIDBEES income", f"{_rs(r['liquidbees_income_est'])} est."
         + (f" ({r['liquidbees_income_share_pct']}% of profit)" if r["liquidbees_income_share_pct"] is not None else "")),
    ]
    for b in r["benchmarks"]:
        out.append((b["name"], f"{_pc(b['return_pct'])}, drawdown {b['max_drawdown_pct']:.2f}%, excess {_pc(b['excess_pct'])}"
                    if b["available"] else "unavailable"))
    if r.get("weekly_line"):
        out.append(("Latest week", r["weekly_line"].removeprefix("This week: ")))
    out.append(("Verdict", r["verdict"]))
    return out


def to_md(r: dict) -> str:
    lines = [f"# Metals Daybook - performance scorecard: {r['period']}", "", "Paper trading only, no real money.", "",
             "| Measure | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows(r)]
    if r.get("flags"):
        lines += ["", "Notes:"] + [f"- {f}" for f in r["flags"]]
    return "\n".join(lines) + "\n"


def to_html(r: dict) -> str:
    e = html.escape
    body = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows(r))
    notes = "".join(f"<li>{e(f)}</li>" for f in r.get("flags", []))
    return (f"<h2>Metals Daybook - performance scorecard: {e(r['period'])}</h2><p>Paper trading only.</p>"
            f"<table>{body}</table>" + (f"<ul>{notes}</ul>" if notes else "") + "\n")


def render(r: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(r, indent=2) + "\n"
    return to_md(r) if fmt == "md" else to_html(r)
