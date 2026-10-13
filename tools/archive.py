#!/usr/bin/env python3
"""Keep the repo small: summarise, then compress, old logs. Safe to run again and again (idempotent).

  python tools/archive.py                 # archive what is old enough
  python tools/archive.py --dry-run       # say what would happen
  python tools/archive.py --check         # only print size warnings (logs/ > 200 MB, archive/ > 1 GB)

1. logs/{decisions,ticks,incidents,health,advice-rejected}-YYYY-MM-DD.(jsonl|csv) older than 35 days
   -> archive/YYYY-MM/<same name>.gz. Before that, one row per day is appended to data/daily_summary.csv
   (date, open, close, day_pnl, floor, buys, sells, skips, blocks, incidents, max_cushion, min_cushion), the
   long-term history for Python and the weekly review. A date already in the CSV is never added twice.
2. memory/daily/YYYY-MM-DD.md older than 60 days -> appended to archive/memory/YYYY-MM.md, then deleted.
3. state/fastlane_requests.jsonl lines older than 35 days -> archive/YYYY-MM/fastlane_requests-YYYY-MM.jsonl.gz.
An archive .gz that already exists is never overwritten with different content (a log that reappears for an old
date is kept next to it as name.2.gz), so nothing is ever lost.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from datetime import date, timedelta
from pathlib import Path

from _common import ROOT, utf8_stdout

LOG_AGE_DAYS = 35
MEMORY_AGE_DAYS = 60
LOGS_WARN_MB = 200
ARCHIVE_WARN_MB = 1024
PREFIXES = ("decisions", "ticks", "incidents", "health", "advice-rejected")
LOG_RE = re.compile(r"^(" + "|".join(PREFIXES) + r")-(\d{4}-\d{2}-\d{2})\.(jsonl|csv)$")
SUMMARY_FIELDS = ["date", "open", "close", "day_pnl", "floor", "buys", "sells", "skips", "blocks", "incidents",
                  "max_cushion", "min_cushion"]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    except OSError:
        pass
    return rows


def summarise_day(logs: Path, day: str) -> dict:
    """One summary row from the day's own log files (blank cells where a file is missing)."""
    row = {k: "" for k in SUMMARY_FIELDS}
    row["date"] = day
    vals, floors = [], []
    try:
        with (logs / f"ticks-{day}.csv").open(newline="", encoding="utf-8") as fh:
            for t in csv.DictReader(fh):
                v, f = _num(t.get("value")), _num(t.get("floor"))
                if v is not None:
                    vals.append(v)
                    floors.append(f if f is not None else 0.0)
    except OSError:
        pass
    if vals:
        cush = [v - f for v, f in zip(vals, floors)]
        row.update(open=round(vals[0], 2), close=round(vals[-1], 2), day_pnl=round(vals[-1] - vals[0], 2),
                   floor=round(floors[-1], 2), max_cushion=round(max(cush), 2), min_cushion=round(min(cush), 2))
    dec = _jsonl(logs / f"decisions-{day}.jsonl")
    for key, act in (("buys", "BUY"), ("sells", "SELL"), ("skips", "SKIP"), ("blocks", "BLOCK")):
        row[key] = sum(1 for r in dec if r.get("action") == act)
    row["incidents"] = len(_jsonl(logs / f"incidents-{day}.jsonl"))
    return row


def read_summary(path: Path) -> dict[str, dict]:
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            return {r["date"]: r for r in csv.DictReader(fh) if r.get("date")}
    except OSError:
        return {}


def write_summary(path: Path, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])
    tmp.replace(path)


def old_logs(logs: Path, today: date, days: int = LOG_AGE_DAYS) -> list[tuple[Path, str]]:
    cutoff = today - timedelta(days=days)
    out = []
    for f in sorted(logs.glob("*")):
        m = LOG_RE.match(f.name)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(2))
        except ValueError:
            continue
        if d < cutoff:
            out.append((f, m.group(2)))
    return out


def archive_logs(root: Path, today: date, dry: bool = False) -> list[str]:
    logs, said = root / "logs", []
    todo = old_logs(logs, today)
    if not todo:
        return said
    summary_path = root / "data" / "daily_summary.csv"
    rows = read_summary(summary_path)
    new_days = sorted({d for _, d in todo} - set(rows))
    for d in new_days:                       # summarise BEFORE anything is compressed
        rows[d] = summarise_day(logs, d)
        said.append(f"summary row for {d}")
    if new_days and not dry:
        write_summary(summary_path, rows)
    for f, d in todo:
        dest = _free_dest(root / "archive" / d[:7] / (f.name + ".gz"), f)
        said.append(f"{f.relative_to(root)} -> {dest.relative_to(root)}")
        if dry:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            _gzip_to(f.read_bytes(), dest)
        f.unlink()
    return said


def _gzip_to(data: bytes, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    with tmp.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out:
        out.write(data)
    tmp.replace(dest)


def _free_dest(dest: Path, f: Path) -> Path:
    """dest itself if it is free or already holds exactly f's content (a re-run after a crash); else name.N.gz."""
    data = f.read_bytes()
    cand, n = dest, 1
    while cand.exists():
        try:
            if gzip.decompress(cand.read_bytes()) == data:
                return cand
        except (OSError, EOFError):
            pass
        n += 1
        cand = dest.with_name(dest.name[:-3] + f".{n}.gz")
    return cand


def archive_fastlane_requests(root: Path, today: date, dry: bool = False, days: int = LOG_AGE_DAYS) -> list[str]:
    """Move fast-lane request lines older than `days` out of state/ (the file is appended to forever otherwise)."""
    src = root / "state" / "fastlane_requests.jsonl"
    if not src.exists():
        return []
    cutoff = (today - timedelta(days=days)).isoformat()
    keep, old = [], {}
    for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ts = str(json.loads(line).get("ts", ""))[:10]
        except (ValueError, AttributeError):
            ts = ""
        if ts and ts < cutoff:
            old.setdefault(ts[:7], []).append(line)
        else:
            keep.append(line)
    if not old:
        return []
    said = [f"state/fastlane_requests.jsonl: {sum(map(len, old.values()))} old line(s) -> archive/"]
    if dry:
        return said
    for ym, lines in old.items():
        dest = root / "archive" / ym / f"fastlane_requests-{ym}.jsonl.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        have = gzip.decompress(dest.read_bytes()).decode("utf-8").splitlines() if dest.exists() else []
        merged = have + [x for x in lines if x not in set(have)]        # a crash before the rewrite cannot duplicate
        _gzip_to(("\n".join(merged) + "\n").encode("utf-8"), dest)
    tmp = src.with_name(src.name + ".tmp")
    tmp.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
    tmp.replace(src)
    return said


def archive_memory(root: Path, today: date, dry: bool = False) -> list[str]:
    said = []
    cutoff = today - timedelta(days=MEMORY_AGE_DAYS)
    for f in sorted((root / "memory" / "daily").glob("*.md")):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", f.name)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d >= cutoff:
            continue
        dest = root / "archive" / "memory" / f"{m.group(1)[:7]}.md"
        said.append(f"{f.relative_to(root)} -> {dest.relative_to(root)}")
        if dry:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = f.read_text(encoding="utf-8").strip()
        have = dest.read_text(encoding="utf-8") if dest.exists() else ""
        if f"<!-- {m.group(1)} -->" not in have:      # a crash between append and delete must not duplicate the note
            with dest.open("a", encoding="utf-8") as fh:
                fh.write(f"<!-- {m.group(1)} -->\n{text}\n\n")
        f.unlink()
    return said


def dir_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6 if path.exists() else 0.0


def size_warnings(root: Path) -> list[str]:
    """For the health check: logs/ over 200 MB, archive/ over 1 GB."""
    out = []
    logs, arc = dir_mb(root / "logs"), dir_mb(root / "archive")
    if logs > LOGS_WARN_MB:
        out.append(f"logs/ is {logs:.0f} MB (limit {LOGS_WARN_MB} MB)")
    if arc > ARCHIVE_WARN_MB:
        out.append(f"archive/ is {arc:.0f} MB (warning above {ARCHIVE_WARN_MB} MB)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--today", help="YYYY-MM-DD (testing)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="only print size warnings")
    a = ap.parse_args(argv)
    utf8_stdout()
    root = Path(a.root)
    if a.check:
        w = size_warnings(root)
        print("\n".join("WARNING: " + x for x in w) or "sizes ok")
        return 0
    today = date.fromisoformat(a.today) if a.today else date.today()
    said = (archive_logs(root, today, a.dry_run) + archive_memory(root, today, a.dry_run)
            + archive_fastlane_requests(root, today, a.dry_run))
    print("\n".join(said) if said else "nothing to archive")
    for x in size_warnings(root):
        print("WARNING: " + x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
