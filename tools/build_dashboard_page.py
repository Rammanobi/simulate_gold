#!/usr/bin/env python3
"""Build the self-contained dashboard page (docs/dashboard/template.html + logs/alldays.json) for the artifact.

  python tools/build_dashboard_page.py --out /somewhere/dashboard.html          # data = logs/alldays.json (rebuilt if missing)
  python tools/build_dashboard_page.py --out FILE --data other-alldays.json     # e.g. a synthetic file for previews

--out must be outside the repo. The page works without a network (the data is embedded); online it keeps itself
fresh from the `live` branch (now.json) and main (logs/alldays.json).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold import alldays


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--out", required=True)
    ap.add_argument("--data", default=None)
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root).resolve()
    out = Path(a.out).resolve()
    if root in out.parents:
        print("ERROR: --out must be outside the repo")
        return 2
    src = Path(a.data) if a.data else root / "logs" / alldays.OUT_NAME
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if a.data:
            print(f"ERROR: cannot read {src}")
            return 2
        data = alldays.build(root)
    if not data.get("days"):
        print("ERROR: no days in the data")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(alldays.render_page(data), encoding="utf-8")
    print(f"{out} | {len(data['days'])} days | {out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
