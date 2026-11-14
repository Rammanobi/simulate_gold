#!/usr/bin/env python3
"""Idempotent "make sure something will start the engine" (extra step of report-retry, fallback and reminders workflows).

    python tools/ensure_engine.py [--dry-run]

Nothing to do when an engine-relay or market-hours run is queued or in progress. Otherwise: on a trading day from 08:55 to 15:25 IST it
dispatches market-hours.yml (session=auto); at any other time it dispatches engine-relay.yml (which sleeps until the next 08:55 IST start).
Always exits 0 (a failed look-up is reported as SKIP). Needs GH_TOKEN and GITHUB_REPOSITORY.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from simgold import relay  # noqa: E402
from simgold.market import IST  # noqa: E402


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo = os.environ.get("GITHUB_REPOSITORY", "Rammanobi/simulate_gold")
    relay.ensure(relay.GhApi(os.environ), repo, datetime.now(IST), dry_run="--dry-run" in args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
