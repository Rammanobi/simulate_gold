#!/usr/bin/env python3
"""One-off, idempotent bootstrap of memory/ltm and memory/stm from what already exists (owner session; no deletions).

  python tools/memory_migrate.py [--no-network] [--root DIR] [--now ISO]

1. regime.json from 2 y of daily history (skipped with --no-network or when the download fails)
2. memory/stm/days/D.json for every date that has logs/decisions-D.jsonl, then window.json
3. calendar.json seeded from state/events.json (by "migration") plus holidays and month-end by code
4. lesson_ledger.json seeded from every "Lesson candidate:" line in memory/daily/*.md and every line of
   memory/lessons-candidates.md (then frozen with a header line); theses.json/theses.md stubs (stance neutral)
5. performance.json from state/equity.csv
Running it again changes nothing that is already there.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

import memory_ltm
import memory_stm

from simgold import brief, memsafe, memstore

CAND_RE = re.compile(r"^\s*(?:[-*]\s*)?Lesson candidate:\s*(.+)$", re.I)
FROZEN = "<!-- moved to memory/ltm/lesson_ledger.json on "


def _text(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= 240 else s[:236].rsplit(" ", 1)[0] + "..."


def seed_ledger(root: Path, now: datetime) -> list[str]:
    msgs, added, skipped = [], 0, 0
    items = []
    for f in sorted((root / "memory" / "daily").glob("*.md")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f.name):
            continue
        for ln in f.read_text(encoding="utf-8").splitlines():
            m = CAND_RE.match(ln)
            if m:
                items.append((f.stem, _text(m.group(1)), "daily"))
    lc = root / "memory" / "lessons-candidates.md"
    frozen = False
    if lc.exists():
        body = lc.read_text(encoding="utf-8")
        frozen = FROZEN in body
        for ln in body.splitlines():
            m = re.match(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\s*\|(.+)$", ln)
            if m:
                items.append((m.group(1), _text(m.group(2).replace("|", ";")), "owner"))
    for d, text, src in items:
        try:
            before = {x["id"]: len(x["support"]) for x in memory_ltm._ledger(root)["items"]}
            lid = memory_ltm.add_candidate(root, now.date(), text, f"{d}: {text[:150]}", src)
            after = {x["id"]: len(x["support"]) for x in memory_ltm._ledger(root)["items"]}
            added += after.get(lid, 0) != before.get(lid, -1)
        except memsafe.Refused as e:
            skipped += 1
            msgs.append(f"ledger: skipped one {src} line from {d}: {e}")
    if lc.exists() and not frozen:
        lc.write_text(f"{FROZEN}{now.date().isoformat()} -->\n" + lc.read_text(encoding="utf-8"), encoding="utf-8")
        msgs.append("memory/lessons-candidates.md frozen (header added); the incident routine now uses the tool")
    msgs.append(f"ledger: {added} new support entries, {skipped} skipped")
    return msgs


def seed_calendar(root: Path, now: datetime) -> str:
    cal = memstore.calendar_events(root)
    have = {(str(e["date"])[:10], e["type"]) for e in cal}
    n = 0
    for e in memstore.engine_events(root):
        typ = e["type"]
        if (e["date"].isoformat(), typ) in have or typ not in memory_ltm.ENGINE_TYPES:
            continue
        row = {"date": e["date"].isoformat(), "type": typ, "metals": list(memstore.METALS), "impact": 3,
               "confirmed": True, "added": now.date().isoformat(), "by": "migration"}
        if e.get("time"):
            row["time"] = e["time"]
        cal.append(row)
        n += 1
    memory_ltm.write_calendar(root, cal, now)
    return f"calendar.json: {n} engine event(s) seeded, holidays and month-end filled"


def migrate(root: Path, now: datetime, network: bool = True, hist=None) -> list[str]:
    root = Path(root)
    out: list[str] = []
    if network or hist is not None:
        try:
            reg = memory_ltm.build_regime(root, now, hist)
            memstore.write_atomic(memstore.ltm_path(root, "regime.json"), memory_ltm._compact(reg))
            out.append("regime.json written: " + ", ".join(f"{m}={r.get('label')}" for m, r in reg["metals"].items()))
        except Exception as e:      # no network / Yahoo down: the engine defaults (LTM FLAT) stay in force
            out.append(f"regime.json NOT written ({type(e).__name__}: {e}); consumers treat LTM as FLAT until it is")
    else:
        out.append("regime.json skipped (--no-network)")
    days = sorted(p.stem.split("decisions-")[1] for p in (root / "logs").glob("decisions-*.jsonl"))
    wrote = []
    for d in days:
        if (memstore.stm_path(root, "days", f"{d}.json")).exists():
            continue
        try:
            rec = memory_stm.build_day(root, d, now)
        except Exception as e:
            out.append(f"day {d}: skipped ({type(e).__name__})")
            continue
        if "value" not in rec and not rec.get("trades") and not rec.get("pauses"):
            continue
        memstore.write_compact(memstore.stm_path(root, "days", f"{d}.json"), rec)
        wrote.append(d)
    out.append(f"stm days written: {', '.join(wrote) or 'none new'}")
    out.append(seed_calendar(root, now))
    out += seed_ledger(root, now)
    have = sorted(p.stem for p in memstore.stm_path(root, "days").glob("????-??-??.json"))
    if have:
        w = memory_stm.build_window(root, have[-1], now)
        memstore.write_atomic(memstore.stm_path(root, "window.json"), memory_stm._window_text(w))
        out.append(f"window.json written (asof {have[-1]})")
    if not memstore.ltm_path(root, "theses.json").exists():
        memstore.write_json(memstore.ltm_path(root, "theses.json"), {})
        memstore.write_atomic(memstore.ltm_path(root, "theses.md"),
                              "# Theses (tool-written; facts + sources only)\n\n(no thesis yet: stance neutral, no effect)\n")
        out.append("theses stubs written")
    obj = memory_ltm.perf_rows(root)
    obj["updated"] = now.isoformat(timespec="seconds")
    import json
    memstore.write_atomic(memstore.ltm_path(root, "performance.json"), json.dumps(obj, ensure_ascii=False, indent=1) + "\n")
    out.append(f"performance.json written ({len(obj['months'])} month row(s))")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now")
    ap.add_argument("--no-network", action="store_true")
    a = ap.parse_args(argv)
    utf8_stdout()
    print("\n".join(migrate(Path(a.root), parse_now(a.now), network=not a.no_network)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
