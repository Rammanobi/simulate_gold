"""Backtest the strategy on daily data.

  python run_backtest.py                       # v1: the old score brain -> reports/backtest.md (the weekly workflow)
  python run_backtest.py --strategy v2         # v2 trend core on Yahoo daily data (network)
  python run_backtest.py --strategy v2 --data-dir DIR   # v2 on the cleaned research CSVs (nse_daily_ohlcv.csv + nse_close_wide.csv)
Options for v2: --start 2010-06-01  --set vol_target=0.10 --set symbols.SILVERBEES.cap=0.6  --out reports/backtest_v2.md
"""
import argparse
import json
from pathlib import Path

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--strategy", choices=["v1", "v2"], default="v1")
ap.add_argument("--start", default=None, help="first trading day (v1 default 2019-01-01, v2 default 2010-06-01)")
ap.add_argument("--data-dir", default=None, help="v2: read the cleaned research CSVs from this folder instead of Yahoo")
ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                help="v2: override a strategy parameter, e.g. vol_target=0.10 or symbols.SILVERBEES.cap=0.6 (repeatable)")
ap.add_argument("--out", default=None, help="v2: also write the markdown table to this file")
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


def run_v2():
    from simgold import backtest, config
    p = backtest.apply_overrides(config.load_params(), args.set)
    start = args.start or "2010-06-01"
    syms = list(p["strategy"]["symbols"])
    frames = backtest.load_csv_frames(args.data_dir, syms) if args.data_dir else backtest.yahoo_frames(p, start)
    rep = backtest.v2_report(frames, p, start)
    pct = lambda x: f"{x * 100:.1f}%"
    lines = [f"# Strategy v2 backtest ({rep['full']['from']} to {rep['full']['to']})", "",
             f"vol_target {p['strategy']['vol_target']}, caps "
             + ", ".join(f"{s} {v['cap']}" for s, v in p["strategy"]["symbols"].items())
             + f"; lag {p['strategy']['backtest']['lag_days']} day(s); costs per side "
             + ", ".join(f"{s} {v * 100:.2f}%" for s, v in p["strategy"]["backtest"]["cost_per_side"].items()) + ". "
             "News layer off, bias neutral. Idle money earns the LIQUIDBEES yield.", "",
             "| period | CAGR | max DD | months > 0 | worst month | trades / month | avg gross | Sharpe |", "|---|---|---|---|---|---|---|---|"]
    for name, s in (("v2 in-sample " + f"{rep['in_sample']['from']} to {rep['in_sample']['to']}", rep["in_sample"]),
                    ("v2 last 2 years (OOS) " + f"{rep['oos']['from']} to {rep['oos']['to']}", rep["oos"]),
                    ("buy and hold GOLDBEES, in-sample", rep["bh_in_sample"]), ("buy and hold GOLDBEES, last 2 years", rep["bh_oos"])):
        lines.append(f"| {name} | {pct(s['cagr'])} | {pct(s['max_drawdown'])} | {pct(s['month_win'])} | {pct(s['worst_month'])} | "
                     f"{s['trades_per_month']:.2f} | {pct(s['avg_gross'])} | {s['sharpe']:.2f} |")
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")


run_v2() if args.strategy == "v2" else run_v1()
