"""Backtest the strategy on daily data.

  python run_backtest.py                       # v1: the old score brain -> reports/backtest.md
  python run_backtest.py --strategy v2         # v2 trend core on Yahoo daily data (network)
  python run_backtest.py --strategy v2 --data-dir DIR   # v2 on the cleaned research CSVs (nse_daily_ohlcv.csv + nse_close_wide.csv)
  python run_backtest.py --strategy v3 --data-dir DIR   # v3 (fully invested D5-FI, owner caps, turnover limits) next to v2, plus a
                                                        # seeded noise "AI" run (--seed, --ai-trust); DIR may also hold close/high/low/open.csv
Options for v2: --start 2010-06-01  --set vol_target=0.10 --set symbols.SILVERBEES.cap=0.6  --out reports/backtest_v2.md
"""
import argparse
import json
from pathlib import Path

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--strategy", choices=["v1", "v2", "v3"], default="v1")  # CLI default stays v1; the weekly workflow passes --strategy v2
ap.add_argument("--start", default=None, help="first trading day (v1 default 2019-01-01, v2 default 2010-06-01)")
ap.add_argument("--data-dir", default=None, help="v2: read the cleaned research CSVs from this folder instead of Yahoo")
ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                help="v2: override a strategy parameter, e.g. vol_target=0.10 or symbols.SILVERBEES.cap=0.6 (repeatable)")
ap.add_argument("--out", default=None, help="v2/v3: also write the markdown table to this file")
ap.add_argument("--seed", type=int, default=7, help="v3: seed of the noise AI run")
ap.add_argument("--ai-trust", type=float, default=1.0, help="v3: allocation.ai_trust used for the noise AI run")
args = ap.parse_args()


def run_v1():
    from simgold import backtest
    res = backtest.run(start=args.start or "2019-01-01")
    s, b = res["stats"], res["bench_stats"]
    Path("reports").mkdir(exist_ok=True)
    res["equity"].join(res["benchmark_gold"].rename("gold_buy_hold")).to_csv("reports/backtest_equity.csv")

    def row(k, fmt="{:.2%}"):
        return f"| {k} | {fmt.format(s[k])} | {fmt.format(b[k])} |"

    monthly = s["monthly"]
    lines = [
        f"# Backtest {res['equity'].index[0].date()} → {res['equity'].index[-1].date()}",
        "", "Starting capital ₹1,00,000. News layer OFF (technical + macro only). Costs and slippage included.", "",
        "| metric | strategy | gold ETF buy & hold |", "|---|---|---|",
        row("final_equity", "₹{:,.0f}"), row("cagr"), row("max_drawdown"), row("sharpe", "{:.2f}"),
        row("avg_month"), row("median_month"), row("pct_months_positive", "{:.0%}"),
        row("pct_months_ge_3pct", "{:.0%}"), row("worst_month"), row("best_month"),
        "", f"Trades: {len(res['portfolio'].trades)} · total costs ₹{res['portfolio'].total_costs:,.0f}", "",
        "## Monthly returns (strategy)", "", "| month | return |", "|---|---|",
        *[f"| {d:%Y-%m} | {r:+.2%} |" for d, r in monthly.items()],
    ]
    Path("reports/backtest.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:18]))
    print("\nLast decision snapshot:\n", json.dumps(res["last_report"], indent=1, default=str)[:3000])


def load_frames(backtest, p, syms, start):
    if not args.data_dir:
        return backtest.yahoo_frames(p, start)
    d = Path(args.data_dir)
    if (d / "nse_daily_ohlcv.csv").exists():
        return backtest.load_csv_frames(d, syms)
    if (d / "close.csv").exists():
        return backtest.load_wide_frames(d, syms)
    raise SystemExit(f"--data-dir {d}: neither nse_daily_ohlcv.csv nor close.csv found")


def run_v3():
    from simgold import backtest, config
    p = backtest.apply_overrides(config.load_params(), args.set)
    start = args.start or "2010-06-01"
    syms = list(p["strategy"]["symbols"])
    frames = load_frames(backtest, p, syms, start)
    rep = backtest.v3_report(frames, p, start, seed=args.seed, ai_trust=args.ai_trust)
    pct = lambda x: f"{x * 100:.1f}%"
    a = p.get("allocation") or {}
    lines = [f"# Strategy v3 backtest ({rep['stats']['v2']['full']['from']} to {rep['stats']['v2']['full']['to']})", "",
             f"v3: fully invested D5-FI default (invested_max {a.get('invested_max', 1.0)}), caps gold/silver/copper "
             + "/".join(str((a.get('ai_caps') or {}).get(s_)) for s_ in syms) + f", turnover {a.get('max_turnover_day')}/day "
             f"{a.get('max_turnover_week')}/week, band {a.get('rebalance_threshold')}, brake level 1 rotates to GOLDBEES (daily backtest brake), "
             f"vol guard {a.get('max_port_vol')} (underwater {a.get('underwater_port_vol')}); the noise AI is a seeded random mix "
             f"(seed {args.seed}, trust {args.ai_trust}) with NO edge: it shows what a worthless AI costs inside the clamp, not what a good one earns. "
             "Lag, costs and idle-cash yield as in v2.", "",
             "| run | period | CAGR | max DD | months > 0 | worst month | trades / month | avg gross | Sharpe |", "|---|---|---|---|---|---|---|---|---|"]
    names = {"v2": "v2 as shipped (50% gold floor)", "v3_default": "v3 default (no AI)", "v3_noise_ai": "v3 + noise AI",
             "buy_hold_gold": "buy and hold GOLDBEES"}
    for key, label in names.items():
        for per, tag in (("full", "full"), ("oos", "last 2 years (OOS)")):
            s_ = rep["stats"][key][per]
            if s_ is None:
                continue
            lines.append(f"| {label} | {tag} {s_['from']} to {s_['to']} | {pct(s_['cagr'])} | {pct(s_['max_drawdown'])} | {pct(s_['month_win'])} | "
                         f"{pct(s_['worst_month'])} | {s_['trades_per_month']:.2f} | {pct(s_['avg_gross'])} | {s_['sharpe']:.2f} |")
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")


def run_v2():
    from simgold import backtest, config
    p = backtest.apply_overrides(config.load_params(), args.set)
    start = args.start or "2010-06-01"
    syms = list(p["strategy"]["symbols"])
    frames = load_frames(backtest, p, syms, start)
    rep = backtest.v2_report(frames, p, start)
    pct = lambda x: f"{x * 100:.1f}%"
    lines = [f"# Strategy v2 backtest ({rep['full']['from']} to {rep['full']['to']})", "",
             f"vol_target {p['strategy']['vol_target']}, caps "
             + ", ".join(f"{s} {v['cap']}" for s, v in p["strategy"]["symbols"].items())
             + f"; lag {p['strategy']['backtest']['lag_days']} day(s); costs per side "
             + ", ".join(f"{s} {v * 100:.2f}%" for s, v in p["strategy"]["backtest"]["cost_per_side"].items()) + ". "
             "News layer off, bias neutral. Idle money earns the LIQUIDBEES yield.", "",
             "| period | CAGR | max DD | months > 0 | worst month | trades / month | avg gross | Sharpe |", "|---|---|---|---|---|---|---|---|"]
    ins, oos = rep["in_sample"], rep["oos"]
    rows = ((f"v2 in-sample {ins['from']} to {ins['to']}" if ins else "", ins),
            (f"v2 last 2 years (OOS) {oos['from']} to {oos['to']}", oos),
            ("buy and hold GOLDBEES, in-sample", rep["bh_in_sample"]), ("buy and hold GOLDBEES, last 2 years", rep["bh_oos"]))
    for name, s in rows:
        if s is None:
            continue
        lines.append(f"| {name} | {pct(s['cagr'])} | {pct(s['max_drawdown'])} | {pct(s['month_win'])} | {pct(s['worst_month'])} | "
                     f"{s['trades_per_month']:.2f} | {pct(s['avg_gross'])} | {s['sharpe']:.2f} |")
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")


def main():
    from simgold.data import HistoryUnavailable
    try:
        {"v1": run_v1, "v2": run_v2, "v3": run_v3}[args.strategy]()
    except HistoryUnavailable as e:
        raise SystemExit(f"backtest aborted: {e}")


main()
