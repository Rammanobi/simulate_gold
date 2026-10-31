#!/usr/bin/env python3
"""Rebuild logs/alldays.json (every trading day for the dashboard) by hand. Read-only over the logs.

  python tools/alldays.py --write            # writes logs/alldays.json (the engine does this at every git sync)
  python tools/alldays.py --out FILE         # another file; --write is not needed
  python tools/alldays.py                    # only prints a summary
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold import alldays


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--write", action="store_true", help="write logs/alldays.json")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root).resolve()
    doc = alldays.build(root)
    target = None
    if a.out:
        target = alldays.write(root, Path(a.out).resolve())
    elif a.write:
        target = alldays.write(root)
    last = doc["days"][-1] if doc["days"] else None
    print(f"{len(doc['days'])} days, {len(doc['trades_all'])} trades"
          + (f", last {last['date']} close {last['close_value']:.2f}" if last else "")
          + (f" -> {target}" if target else " (not written; use --write)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
