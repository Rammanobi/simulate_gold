#!/usr/bin/env python3
"""Engine relay CLI (run by .github/workflows/engine-relay.yml). Logic lives in simgold/relay.py.

    python tools/engine_relay.py [--dry-run] [--until ISO_TIME]

Sleeps (at most 5h45m per run) until 08:55 IST of the next trading day, dispatches market-hours.yml (session=auto) if no run is
queued or in progress, re-checks at ~09:30 and ~12:20 IST, then ends. When the next event is further away than one run can sleep,
it dispatches the next relay run (never when another relay run, other than this one, is pending) and ends.
--dry-run: no sleeping (virtual clock) and nothing is dispatched; every would-be dispatch is printed.
--until: ISO time (IST if no zone; not more than 5 min in the past or 7 days ahead, else exit 2) replacing the next 08:55 start; the
re-checks keep their offsets. A run with --dry-run or --until is a TEST run: it never hands over to a successor relay.
Needs GH_TOKEN, GITHUB_REPOSITORY and (optionally) GITHUB_RUN_ID. Never trades, never commits.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from simgold import relay  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--until", default="")
    a = ap.parse_args(argv)
    try:
        until = relay.parse_until(a.until, relay.Clock().now())
    except ValueError as e:
        print(f"bad --until value: {e}")
        return 2
    repo = os.environ.get("GITHUB_REPOSITORY", "Rammanobi/simulate_gold")
    clock = relay.Clock()
    if a.dry_run:
        clock = relay.VirtualClock(clock.now())
    r = relay.Relay(relay.GhApi(os.environ), repo, os.environ.get("GITHUB_RUN_ID", ""), clock, a.dry_run, until)
    print(f"outcome: {r.run()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
