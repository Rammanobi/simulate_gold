"""One-off helper (NOT run by pytest): rebuilds tests/evals/fixtures from the repo's real logs and Yahoo daily bars.

    python tests/evals/make_fixtures.py        # needs network for the daily bars only
The fixtures are committed, so the eval suite itself never touches the network.
"""
from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIX = HERE / "fixtures"
DAYS = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"]
SYMS = ["GOLDBEES", "SILVERBEES", "HINDCOPPER"]


def main():
    FIX.mkdir(exist_ok=True)
    for d in DAYS:
        rows = list(csv.DictReader(open(ROOT / "logs" / f"ticks-{d}.csv", encoding="utf-8")))
        with open(FIX / f"ticks-{d}.csv", "w", encoding="utf-8") as fh:
            fh.write("time," + ",".join(SYMS) + "\n")
            for r in rows:
                fh.write(r["time"] + "," + ",".join(r[s] for s in SYMS) + "\n")
    from simgold.data import daily_history
    h = daily_history([s + ".NS" for s in SYMS], period="3y")
    out = {}
    for s in SYMS:
        c, hi, lo = (h[k][s + ".NS"].dropna() for k in ("close", "high", "low"))
        c = c[c.index < "2026-10-29"].tail(330)
        out[s] = {"dates": [i.date().isoformat() for i in c.index], "close": [round(float(x), 4) for x in c],
                  "high": [round(float(hi.reindex(c.index).ffill().loc[i]), 4) for i in c.index],
                  "low": [round(float(lo.reindex(c.index).ffill().loc[i]), 4) for i in c.index]}
    (FIX / "daily_bars.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    sha = subprocess.run(["git", "log", "--format=%h", "--until=2026-10-21 09:10 +0530", "-1", "--", "state/portfolio.json"],
                         cwd=ROOT, capture_output=True, text=True).stdout.strip()
    (FIX / "start_portfolio_2026-10-21.json").write_text(
        subprocess.run(["git", "show", f"{sha}:state/portfolio.json"], cwd=ROOT, capture_output=True, text=True).stdout)
    (FIX / "events.json").write_text(subprocess.run(["git", "show", "origin/main:state/events.json"], cwd=ROOT,
                                                    capture_output=True, text=True).stdout)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    main()
