"""git merge driver for the append-only daily logs (logs/ticks-DATE.csv, logs/decisions-DATE.jsonl, ...).

These files are only ever appended to, by the engine and (health) by the watchdog. A normal line merge of two
appends at the end of the same file is a conflict, and the sync resolved it with `pull --rebase -X theirs`, which
kept one side's rows and DROPPED the other side's. On 14 Oct 2026 that deleted the whole morning session
(ticks 10:17-12:20 and 28 decisions) and the 14:26-14:52 ticks. This driver merges as a union instead:

  * every row from both sides is kept; rows are never deleted by a merge (append-only);
  * ticks CSV: one header (the widest one), one row per HH:MM (ours first), sorted by time; a row from a file
    without the price columns is padded with empty cells;
  * JSONL: exact duplicate lines are dropped; when every line is a JSON object with a "ts", the result is sorted
    by ts (stable), otherwise ours come first and the new lines of theirs follow.

  python -m simgold.logmerge merge-file %O %A %B %P     (.gitattributes: merge=simgold-append)
It writes the result to %A and exits 0. Only if it crashes does it exit 1 (a conflict), so a bug can never make
git silently keep one side.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DRIVER = "simgold-append"


def _lines(path) -> list[str]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [x.rstrip("\r") for x in text.split("\n") if x.strip()]


def merge_ticks(ours: list[str], theirs: list[str]) -> list[str]:
    heads = [x[0] for x in (ours, theirs) if x and x[0].startswith("time,")]
    header = max(heads, key=lambda h: len(h.split(","))) if heads else "time,value,floor,cash,liquidbees"
    width = len(header.split(","))
    rows: dict[str, str] = {}
    for side in (ours, theirs):
        for row in side:
            if row.startswith("time,"):
                continue
            cells = row.split(",")
            if len(cells) < width:
                cells += [""] * (width - len(cells))
            rows.setdefault(cells[0].strip(), ",".join(cells))
    return [header] + [rows[k] for k in sorted(rows)]


def _ts(line: str):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return str(d["ts"]) if isinstance(d, dict) and d.get("ts") is not None else None


def merge_jsonl(ours: list[str], theirs: list[str]) -> list[str]:
    out, seen = [], set()
    for line in ours + theirs:
        if line not in seen:
            seen.add(line)
            out.append(line)
    keys = [_ts(x) for x in out]
    if out and all(k is not None for k in keys):
        out = [x for _, x in sorted(zip(keys, out), key=lambda kv: kv[0])]     # sorted() is stable
    return out


def merge_file(base_path, ours_path, theirs_path, name: str = "") -> int:
    ours, theirs = _lines(ours_path), _lines(theirs_path)
    name = name or str(ours_path)
    merged = merge_ticks(ours, theirs) if name.endswith(".csv") else merge_jsonl(ours, theirs)
    Path(ours_path).write_text("".join(x + "\n" for x in merged), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mf = sub.add_parser("merge-file", help="git merge driver: merge-file %%O %%A %%B %%P")
    mf.add_argument("base"); mf.add_argument("ours"); mf.add_argument("theirs"); mf.add_argument("name", nargs="?", default="")
    a = ap.parse_args(argv)
    try:
        return merge_file(a.base, a.ours, a.theirs, a.name)
    except Exception as e:      # noqa: BLE001 - report a conflict rather than silently keep one side
        sys.stderr.write(f"logmerge: {type(e).__name__}: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
