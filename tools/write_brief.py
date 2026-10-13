#!/usr/bin/env python3
"""Write memory/brief.md (the small, cheap, no-AI summary the routines read).

  python tools/write_brief.py            # write memory/brief.md and print it
  python tools/write_brief.py --dry-run  # print only; do not touch files or the headline dedupe state
The engine runs this at every git sync and at the end of the run.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import brief


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", help="ISO timestamp (testing)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    utf8_stdout()
    text = brief.build_brief(Path(a.root), parse_now(a.now), write=not a.dry_run)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
