"""Backtest the strategy on daily data and write reports/backtest.md."""
import argparse
import json
from pathlib import Path

from simgold import backtest

ap = argparse.ArgumentParser()
ap.add_argument("--start", default="2019-01-01")
args = ap.parse_args()

res = backtest.run(start=args.start)
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
