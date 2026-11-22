#!/usr/bin/env python3
"""India price gap, read-only view (no AI, never trades, writes nothing).

  python tools/pricegap.py show [--json]          # state/pricegap.json as the report reads it
  python tools/pricegap.py show --live [--json]   # download fresh closes and compute now (prints only; does not write the state file)

The estimate constants and the maths are documented in simgold/pricegap.py. Information only: GOLDBEES / SILVERBEES against the
world price in rupees (COMEX x USDINR x estimated metal per unit, plus the import duty). The cause of any gap is not verified.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold import pricegap


def _fmt(obj: dict, source: str) -> list[str]:
    L = [f"India price gap ({source}) for {obj.get('date')}, band {obj.get('warn_pct')}%, duty {obj.get('duty_pct')}% in parity; ESTIMATE"]
    for sym, g in (obj.get("gaps") or {}).items():
        med = "n/a" if g.get("median20_pct") is None else f"{g['median20_pct']:+.2f}%"
        z = "n/a" if g.get("z") is None else f"{g['z']:+.2f}"
        w = g.get("world_premium_pct")
        L.append(f"  {sym}: premium {g['premium_pct']:+.2f}% over parity"
                 + ("" if w is None else f" ({w:+.2f}% over the bare world price)") + f" | 20d median {med} | z {z} | n {g.get('n', '?')}"
                 + (" | ABOVE BAND" if g.get("warn") else "") + (" | STALE (not today's reading)" if g.get("stale") else ""))
    w = obj.get("world") or {}
    if w:
        L.append("  world: " + ", ".join(f"{k} {v}" for k, v in sorted(w.items())))
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("show", help="print the state file (or a fresh computation with --live)")
    sh.add_argument("--live", action="store_true")
    sh.add_argument("--json", action="store_true")
    sh.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root)
    if a.live:
        try:
            cfg = pricegap.settings()
            obj = pricegap.compute(pricegap.fetch_frame(cfg), cfg)
        except Exception as e:  # noqa: BLE001
            print(f"cannot compute live: {type(e).__name__}: {e}")
            return 1
        if obj is None:
            print("no usable data")
            return 1
        src = "live, not saved"
    else:
        try:
            obj = json.loads(pricegap.state_path(root / "state").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("no state/pricegap.json (pricegap is dormant until pricegap.enabled is true; try --live)")
            return 0
        if not isinstance(obj, dict) or not isinstance(obj.get("gaps"), dict):
            print("state/pricegap.json is not in the expected shape")
            return 1
        src = "state file"
    print(json.dumps(obj, indent=1, sort_keys=True) if a.json else "\n".join(_fmt(obj, src)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
