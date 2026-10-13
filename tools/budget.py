#!/usr/bin/env python3
"""Thrift mode helper for the routines (see simgold/budget.py).

  python tools/budget.py mode --rate-limit-type T [--overage true|false]   prints NORMAL or THRIFT, writes state/budget.json
  python tools/budget.py show                                              prints the current mode

T comes from the claude-code-remote `get_session` tool: external_metadata.rate_limit_info.rateLimitType.
"ccr_promotional" is NORMAL; anything else (also empty or unknown, or get_session unavailable) is THRIFT.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from simgold import budget  # noqa: E402


def _bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mode")
    m.add_argument("--rate-limit-type", default="")
    m.add_argument("--overage", default="false")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    state = Path(a.root) / "state"
    if a.cmd == "mode":
        print(budget.write(state, a.rate_limit_type, _bool(a.overage))["mode"])
    else:
        print(budget.read_mode(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
