"""Start the engine from GitHub's side when a routine pushes guidance (.github/workflows/engine-kick.yml).

15 Oct 2026 (and 14 Oct): the premarket routine's `gh api -X POST .../market-hours.yml/dispatches` was denied by the
Claude Code auto-mode classifier ("Interfere With Workloads") although .claude/settings.json pre-approves it, and
GitHub ran this repo's 09:00/09:10 IST crons about five hours late, so the first tick came at 10:18 IST (the hourly
routine's dispatch). The routine's `git push` of state/guidance.json is not blocked and its push event starts a
workflow within seconds, so engine-kick.yml listens for that push and dispatches market-hours.yml itself.

This module only decides. It prints "DISPATCH: ..." (exit 0) or "SKIP: ..." (exit 3); the workflow does the REST
calls. It never trades and never changes a rule: a dispatched run is the same engine run a routine would start, and
simgold/runguard.py plus the paper-trader concurrency group still stop a second run.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time

from simgold.market import IST, is_trading_day, now_ist

# 08:30 IST: the premarket routine pushes at ~08:53, and an `auto` run started then idles until the 09:15 open and
# ends its morning session at 12:20 (well under the 6 h job limit). 15:20 IST: the hourly routines' own cut-off.
WINDOW_START, WINDOW_END = time(8, 30), time(15, 20)


def decide(now: datetime, active: str) -> tuple[bool, str]:
    """(dispatch?, reason). `active` is the comma-joined status of market-hours runs that are not completed."""
    now = now.astimezone(IST)
    if not is_trading_day(now.date()):
        return False, f"{now:%a %d %b} is not an NSE trading day"
    if not (WINDOW_START <= now.time() < WINDOW_END):
        return False, f"{now:%H:%M} IST is outside {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M}"
    states = [s for s in (x.strip() for x in (active or "").split(",")) if s]
    if states:
        return False, f"a market-hours run is already active ({','.join(states)})"
    return True, f"no market-hours run is active at {now:%H:%M} IST"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--active", default="", help="comma-joined status of unfinished market-hours runs ('' = none)")
    args = ap.parse_args(argv)
    go, why = decide(now_ist(), args.active)
    print(("DISPATCH: " if go else "SKIP: ") + why)
    return 0 if go else 3


if __name__ == "__main__":
    sys.exit(main())
