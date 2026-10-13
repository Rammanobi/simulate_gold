#!/usr/bin/env python3
"""Print the memory a routine needs, in this order, under a hard size cap. The ONE command routines run to load context.

  python tools/context.py
Order: memory/brief.md, memory/lessons.md, the last 3 memory/daily/*.md (oldest first), the latest memory/weekly/*.md.
Over routines.context_max_bytes (25 KB) the oldest daily note is dropped first, then the weekly note and the lessons
are shortened, and only as a last resort the brief.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import ROOT, utf8_stdout

from simgold import config


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _size(parts: list[tuple[str, str]]) -> int:
    return len(render(parts).encode("utf-8"))


def render(parts: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"===== {name} =====\n{body}" for name, body in parts if body is not None) + "\n"


MARK = "[...cut to fit the size cap]"


def _shrink(body: str, over: int) -> str:
    """Cut `over` bytes (and a little extra for the marker) from the end of body, on a line boundary."""
    keep = body.encode("utf-8")[: max(0, len(body.encode("utf-8")) - over - 40)].decode("utf-8", "ignore")
    keep = keep.rsplit("\n", 1)[0] if "\n" in keep else keep
    return (keep + "\n" + MARK) if keep else MARK


def build(root: Path, cap: int | None = None, daily_n: int | None = None) -> str:
    root = Path(root)
    r = config.load_params()["routines"]
    cap = cap or r["context_max_bytes"]
    daily_n = daily_n or r["context_daily_notes"]
    mem = root / "memory"
    parts: list[tuple[str, str]] = [
        ("memory/brief.md", _read(mem / "brief.md") or "(missing: run python tools/write_brief.py)"),
        ("memory/lessons.md", _read(mem / "lessons.md") or "(missing)"),
    ]
    for f in sorted((mem / "daily").glob("*.md"))[-daily_n:]:
        parts.append((f"memory/daily/{f.name}", _read(f)))
    weekly = sorted((mem / "weekly").glob("*.md"))
    if weekly:
        parts.append((f"memory/weekly/{weekly[-1].name}", _read(weekly[-1])))

    def idx(prefix: str) -> list[int]:
        return [i for i, (n, _) in enumerate(parts) if n.startswith(prefix)]

    while _size(parts) > cap:
        daily = idx("memory/daily/")
        if daily:
            parts.pop(daily[0])          # oldest daily first
            continue
        over = _size(parts) - cap
        for prefix in ("memory/weekly/", "memory/lessons.md", "memory/brief.md"):
            hit = [i for i in idx(prefix) if len(parts[i][1]) > len(MARK) + 2]
            if hit:
                n, b = parts[hit[0]]
                parts[hit[0]] = (n, _shrink(b, over))
                break
        else:       # every part is already down to its marker: hard cut as the last resort
            return render(parts).encode("utf-8")[:cap].decode("utf-8", "ignore")
    return render(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    utf8_stdout()
    print(build(Path(a.root)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
