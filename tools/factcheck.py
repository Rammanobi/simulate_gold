#!/usr/bin/env python3
"""Fact check of the numbers a desk or a proposal states (strategy v3; paper trading only; code only; read-only).

  python tools/factcheck.py check --date D [--desk macro|gold|silver|copper|risk|proposal] [--strict] [--json]

Reads analysis/D/<desk>.json and state/allocation.json (when its for_date is D), finds stated prices, levels, percentage moves and
trading dates, and prints a table of OK / MISMATCH / UNVERIFIABLE with the engine's own number (simgold/factcheck.py has the rules and
tolerances). It refuses nothing and writes nothing. Exit 0 always, except with --strict: exit 1 when any MISMATCH exists.
tools/desk.py write and tools/guidance.py allocation set run the same check and only print a WARNING.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import ROOT, iso_day, utf8_stdout

from simgold import factcheck

DESKS = ("macro", "gold", "silver", "copper", "risk")


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def check(root, day: str, desk: str | None = None) -> dict:
    root = Path(root)
    reps = []
    for d in ([desk] if desk and desk != "proposal" else ([] if desk == "proposal" else list(DESKS))):
        obj = _load(root / "analysis" / day / f"{d}.json")
        if obj is not None:
            reps.append(factcheck.check_desk(root, day, d, obj))
    if desk in (None, "proposal"):
        obj = _load(root / "state" / "allocation.json")
        if isinstance(obj, dict) and obj.get("for_date") == day:
            reps.append(factcheck.check_proposal(root, day, obj))
    return {"date": day, "reports": reps, "n_mismatch": sum(r["n_mismatch"] for r in reps), "n_files": len(reps)}


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", allow_abbrev=False)
    c.add_argument("--date", required=True)
    c.add_argument("--desk", choices=DESKS + ("proposal",))
    c.add_argument("--strict", action="store_true")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        day = iso_day(a.date).isoformat()
    except ValueError as e:
        print(f"REFUSED: --date: {e}", file=sys.stderr)
        return 2
    out = check(a.root, day, a.desk)
    if a.json:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    elif not out["reports"]:
        print(f"factcheck {day}: no desk or proposal files to check")
    else:
        print("\n".join(factcheck.table(r) for r in out["reports"]))
    return 1 if a.strict and out["n_mismatch"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
