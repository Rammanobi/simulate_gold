#!/usr/bin/env python3
"""Print the memory a routine needs, in this order, under a hard size cap. The ONE command routines run to load context.

  python tools/context.py                      # unchanged default (everything, 25 KB cap)
  python tools/context.py --view hourly|fastlane|premarket|daily|weekly   # per-routine view with per-section caps
Default order: memory/brief.md, memory/lessons.md, the last 3 memory/daily/*.md (oldest first), the latest memory/weekly/*.md.
Over routines.context_max_bytes (25 KB) the oldest daily note is dropped first, then the weekly note and the lessons
are shortened, and only as a last resort the brief.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from _common import ROOT, parse_now, utf8_stdout

from simgold import brief, config, memstore


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


# ------------------------------------------------------------------ per-routine views (two-tier memory)
# view -> (total cap in bytes, [(section, cap in bytes)] in priority order; trimmed from the bottom)
VIEWS: dict[str, tuple[int, list[tuple[str, int]]]] = {
    "hourly": (8000, [("alignment", 1000), ("brief", 4000), ("stm_window", 1500), ("notes_today", 1300),
                      ("events_3d", 300), ("lessons_tactical", 1000)]),
    "fastlane": (6000, [("alignment", 1000), ("brief", 4000), ("thesis_lines", 400), ("notes_today", 1000)]),
    "premarket": (18000, [("alignment", 1000), ("regime", 1500), ("theses", 3500), ("calendar_14d", 1200),
                          ("stm_window_full", 2000), ("daily_2", 2000), ("lessons", 4000), ("brief", 3000)]),
    "daily": (10000, [("stm_day", 1500), ("notes_today", 1300), ("daily_2", 2000), ("brief", 3000),
                      ("lesson_titles", 1500)]),
    "weekly": (25000, [("performance", 4000), ("alignment", 1000), ("regime", 2500), ("theses", 3500),
                       ("calendar_8w", 5000), ("lessons", 4000), ("ledger", 3000), ("stm_window_full", 2000),
                       ("daily_week", 4000), ("weekly_last", 2000)]),
}
MISSING = "(missing: not built yet; the owner runs tools/memory_migrate.py once)"


def _cap(body: str, n: int) -> str:
    if len(body.encode("utf-8")) <= n:
        return body
    return _shrink(body, len(body.encode("utf-8")) - n)


def _trim_headlines(text: str, n: int) -> str:
    out, inside, seen = [], False, 0
    for ln in text.splitlines():
        if ln.startswith("## "):
            inside, seen = ln.startswith("## New headlines"), 0
        elif inside and ln.strip() and ln != "none":
            seen += 1
            if seen > n:
                if seen == n + 1:
                    out.append("(more headlines omitted)")
                continue
        out.append(ln)
    return "\n".join(out)


def _json(root: Path, *rel: str):
    return brief.load_json(Path(root).joinpath(*rel), {})


def _s_alignment(root, now, p):
    return "\n".join(memstore.alignment_lines(root, now))


def _shorten_decisions(text: str, keep: int) -> str:
    """Keep only the last `keep` lines of the "Today's decisions" section (the oldest go first)."""
    out, sec = [], []
    inside = False
    for ln in text.splitlines():
        if ln.startswith("## "):
            if inside and len(sec) > keep:
                out.append(f"({len(sec) - keep} earlier decision lines omitted)")
                sec = sec[-keep:] if keep else []
            out += sec
            sec, inside = [], ln.startswith("## Today's decisions")
            out.append(ln)
        elif inside:
            sec.append(ln)
        else:
            out.append(ln)
    if inside and len(sec) > keep:
        out.append(f"({len(sec) - keep} earlier decision lines omitted)")
        sec = sec[-keep:] if keep else []
    return "\n".join(out + sec)


def _s_brief(root, now, p, headlines=8):
    """memory/brief.md with at most 8 headlines; the section view cap later drops the oldest decision lines first."""
    t = _read(Path(root) / "memory" / "brief.md")
    return _trim_headlines(t, headlines) if t else ""


def _window_lines(root, full: bool) -> str:
    w = memstore.window_data(root)
    if not w:
        return ""
    L = [f"window asof {w.get('asof')}"]
    for m, r in (w.get("metals") or {}).items():
        if r:
            sh = r.get("last_shock")
            L.append(f"{m}: stm={r.get('stm_state')} m5 {r.get('m5_pct')}% m10 {r.get('m10_pct')}% z5 {r.get('z5')} "
                     f"streak {r.get('streak')} ema10 {r.get('ema10_side')} volx {r.get('vol_ratio')}"
                     + (f" shock {sh['sessions_ago']}d ago z{sh['z']}" if sh else ""))
    pf = w.get("portfolio") or {}
    L.append(f"10d: pnl {pf.get('pnl_10d')} trades {pf.get('trades_10d')} hit {pf.get('hit_rate_10d')} "
             f"pauses {pf.get('pauses_10d')} near-misses {pf.get('near_misses_10d')}")
    if w.get("events_next_5d"):
        L.append("next: " + ", ".join(w["events_next_5d"]))
    return "\n".join(L if full else L[:5])


def _s_notes_today(root, now, p):
    out = []
    for ln in _read(memstore.stm_path(root, "today.jsonl")).splitlines():
        try:
            n = json.loads(ln)
        except ValueError:
            continue
        if isinstance(n, dict) and n.get("date") == now.date().isoformat():
            out.append(f"{n.get('t')} {n.get('who')}/{n.get('metal')}: {n.get('text')}" + (f" ({n['source']})" if n.get("source") else ""))
    return "\n".join(out)


def _events(root, today: date, days: int) -> list[str]:
    rows = {}
    for e in memstore.calendar_events(root):
        try:
            d = date.fromisoformat(str(e["date"])[:10])
        except ValueError:
            continue
        if today <= d <= today + timedelta(days=days):
            rows[(d, e["type"])] = f"{d:%a %d %b} {e['type']}" + (f" {e['time']}" if e.get("time") else "") + \
                (f" - {e['expect']}" if e.get("expect") else "") + ("" if e.get("confirmed", True) else " (unconfirmed)")
    for e in memstore.engine_events(root):
        if today <= e["date"] <= today + timedelta(days=days):
            rows.setdefault((e["date"], e["type"]), f"{e['date']:%a %d %b} {e['type']}" + (f" {e['time']}" if e.get("time") else ""))
    return [v for _, v in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1]))]


def _s_lessons_tactical(root, now, p):
    body = _read(Path(root) / "memory" / "lessons.md")
    return "\n".join(ln for ln in body.splitlines() if ln.lstrip().startswith("- ") and "tactical" in ln.lower())


def _s_thesis_lines(root, now, p):
    out = []
    for m, t in memstore.theses(root).items():
        if isinstance(t, dict):
            inv = t.get("invalidation") or {}
            out.append(f"{m}: {t.get('stance')}{' INVALIDATED ' + str(t.get('invalidated_on')) if t.get('invalidated') else ''}; "
                       f"invalid if {inv.get('kind')} {inv.get('symbol', '')} {inv.get('value')}; review by {t.get('review_by')}")
    return "\n".join(out)


def _s_regime(root, now, p):
    reg = memstore.regime_data(root)
    if not reg:
        return ""
    L = [f"regime asof {reg.get('asof')} (code-computed)"]
    for m, r in (reg.get("metals") or {}).items():
        L.append(f"{m}: {r.get('label')} since {r.get('since')} votes {r.get('votes')}/{r.get('tests')} r21 {r.get('r21')} "
                 f"r63 {r.get('r63')} r126 {r.get('r126')} r252 {r.get('r252')} vol {r.get('vol_regime')} "
                 f"drivers {r.get('driver_score')}" + (" STALE" if r.get("stale") else ""))
    return "\n".join(L)


def _s_theses(root, now, p):
    return _read(memstore.ltm_path(root, "theses.md"))


def _s_calendar(days):
    def f(root, now, p):
        return "\n".join(_events(root, now.date(), days))
    return f


def _s_daily_n(n):
    def f(root, now, p):
        return "\n\n".join(_read(x) for x in sorted((Path(root) / "memory" / "daily").glob("*.md"))[-n:])
    return f


def _s_daily_week(root, now, p):
    start = now.date() - timedelta(days=now.weekday())
    fs = [x for x in sorted((Path(root) / "memory" / "daily").glob("*.md")) if x.stem >= start.isoformat()]
    return "\n\n".join(_read(x) for x in fs[-5:])


def _s_lessons(root, now, p):
    return _read(Path(root) / "memory" / "lessons.md")


def _s_lesson_titles(root, now, p):
    return "\n".join(ln.strip()[:70] for ln in _read(Path(root) / "memory" / "lessons.md").splitlines()
                      if ln.lstrip().startswith("- "))


def _s_stm_day(root, now, p):
    import memory_stm
    fs = sorted(memstore.stm_path(root, "days").glob("????-??-??.json"))
    fs = [f for f in fs if f.stem <= now.date().isoformat()]
    rec = brief.load_json(fs[-1]) if fs else None
    return memory_stm.render_day(rec) if isinstance(rec, dict) else ""


def _s_performance(root, now, p):
    return _read(memstore.ltm_path(root, "performance.json"))


def _s_ledger(root, now, p):
    led = _json(root, "memory", "ltm", "lesson_ledger.json")
    rows = []
    for it in (led.get("items") if isinstance(led, dict) else None) or []:
        sup = {x.get("date") for x in it.get("support", [])}
        rows.append(f"{it.get('id')} [{it.get('status')}] first {it.get('first_seen')} support {len(sup)} "
                    f"contra {len(it.get('contradict', []))}: {str(it.get('text'))[:100]}")
    return "\n".join(rows)


def _s_weekly_last(root, now, p):
    w = sorted((Path(root) / "memory" / "weekly").glob("*.md"))
    return _read(w[-1]) if w else ""


SECTIONS = {
    "alignment": _s_alignment, "brief": _s_brief, "stm_window": lambda r, n, p: _window_lines(r, False),
    "stm_window_full": lambda r, n, p: _window_lines(r, True), "notes_today": _s_notes_today,
    "events_3d": _s_calendar(3), "calendar_14d": _s_calendar(14), "calendar_8w": _s_calendar(56),
    "lessons_tactical": _s_lessons_tactical, "thesis_lines": _s_thesis_lines, "regime": _s_regime,
    "theses": _s_theses, "daily_2": _s_daily_n(2), "daily_week": _s_daily_week, "lessons": _s_lessons,
    "lesson_titles": _s_lesson_titles, "stm_day": _s_stm_day, "performance": _s_performance,
    "ledger": _s_ledger, "weekly_last": _s_weekly_last,
}
NAMES = {"alignment": "alignment (code: LTM x STM)", "brief": "memory/brief.md", "stm_window": "STM window (compact)",
         "stm_window_full": "STM window (10 sessions)", "notes_today": "today's routine notes",
         "events_3d": "events next 3 days", "calendar_14d": "calendar next 14 days", "calendar_8w": "calendar next 8 weeks",
         "lessons_tactical": "lessons tagged tactical", "thesis_lines": "thesis invalidation lines",
         "regime": "LTM regime", "theses": "memory/ltm/theses.md", "daily_2": "last 2 daily notes",
         "daily_week": "this week's daily notes", "lessons": "memory/lessons.md", "lesson_titles": "lesson titles",
         "stm_day": "STM day facts", "performance": "memory/ltm/performance.json", "ledger": "lesson ledger summary",
         "weekly_last": "latest weekly note"}


def build_view(root: Path, view: str, now: datetime | None = None) -> str:
    """One routine's context: fixed sections, each capped, the whole under the view cap (trimmed from the bottom)."""
    root = Path(root)
    p = config.load_params()
    now = now or parse_now(None)
    conf = ((p.get("routines") or {}).get("context_views") or {}).get(view) or {}
    total, sections = VIEWS[view]
    total = int(conf.get("cap", total))
    caps = conf.get("sections") or {}
    parts: list[tuple[str, str]] = []
    for sec, cap in sections:
        try:
            body = SECTIONS[sec](root, now, p)
        except Exception as e:     # a broken memory file must never break a routine
            body = f"(error reading {sec}: {type(e).__name__})"
        scap = int(caps.get(sec, cap))
        if sec == "brief" and body:
            for keep in (12, 8, 5, 3, 1, 0):
                if len(body.encode("utf-8")) <= scap:
                    break
                body = _shorten_decisions(body, keep)
        parts.append((NAMES[sec], _cap(body, scap) if body else MISSING))
    while _size(parts) > total:
        over = _size(parts) - total
        for i in range(len(parts) - 1, -1, -1):
            n, b = parts[i]
            if len(b) > len(MARK) + 2:
                parts[i] = (n, _shrink(b, over))
                break
        else:
            return render(parts).encode("utf-8")[:total].decode("utf-8", "ignore")
    return render(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--view", choices=sorted(VIEWS), help="per-routine view with per-section caps (default: everything)")
    ap.add_argument("--now", help="ISO timestamp (testing)")
    a = ap.parse_args(argv)
    utf8_stdout()
    if a.view:
        print(build_view(Path(a.root), a.view, parse_now(a.now)), end="")
    else:
        print(build(Path(a.root)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
