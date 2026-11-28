#!/usr/bin/env python3
"""Recommended AI trust from measured forecast skill (strategy v3; paper trading only; read-only; code only).

  python tools/trust.py show [--week 2026-W48] [--json]

Prints the evidence (Brier skill against a base-rate forecaster with n and a confidence interval, the paired AI-vs-default daily
P&L difference with t-stat and block-bootstrap interval, proposal validity, fact-check mismatch rate) and a RECOMMENDED
allocation.ai_trust in {0, 0.25, 0.5, 0.75, 1.0} with the reasons. It only recommends: it never writes config, state or the ledger,
and never applies trust. The owner commits `allocation.ai_trust` to config/params.yaml. Rules and table: simgold/trust.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import trust


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", allow_abbrev=False)
    s.add_argument("--week")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        t = trust.compute(Path(a.root), parse_now(a.now), a.week)
    except ValueError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    print(json.dumps(t, indent=1) if a.json else trust.text(t))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
