#!/usr/bin/env python3
"""Turn repo data into Daybook Inbox item JSON (the owner approves/rejects items in a claude.ai Artifact).

  item-from-incident --id ID [--status S] [--out FILE]      incidents/ID.json -> item
  item-from-daily --date D [--report-url URL] [--out FILE]  reports/daily/D.json -> item (needs_approval, kind daily)
  bump --file item.json --status S --history "text" [--from-incident ID]
        revision += 1, updated = now, history appended (in place). With --from-incident the content fields
        (title, summary, what_wrong, log, impact, actions, proposals, next, handled_by, report_url, severity)
        are refreshed from incidents/ID.json; id, created, history and revision are kept. --from-daily DATE
        does the same from reports/daily/DATE.json (title, summary, impact, next, report_url when given).

The routine writes the result with the ArtifactData tool: action "set", collection "items", doc_id = the item's
`id`, file_path = the JSON file. Item fields: id, kind incident|daily, severity high|medium|low, status
needs_approval|working|resolved|rejected|monitoring, revision, title, summary, handled_by, what_wrong, log,
impact [[label, value]], actions [{text, url?}], proposals [{title, pr_url, risk}], next, report_url, created,
updated (ISO, IST), history [{at, text}], sample.
Every command takes --now ISO (testing). Item ids hold only letters, digits . _ ~ - (see simgold/inboxlink.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from simgold.inboxlink import daily_item_id, is_valid_id, own_pr_url, safe_id  # noqa: E402
from simgold.report import inr, sg  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
MAX_REVISION = 4      # revision 1 is the first version; each owner rejection allows one more; the 4th is final (incident.md D.2)
STATUSES = ("needs_approval", "working", "resolved", "rejected", "monitoring")
SEVERITIES = ("high", "medium", "low")
# incident record status (tools/incident.py) -> item status
INCIDENT_STATUS = {"needs-approval": "needs_approval", "resolved": "resolved", "monitoring": "monitoring"}


def _now(s: str | None = None) -> datetime:
    if not s:
        return datetime.now(IST)
    d = datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=IST)).astimezone(IST)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _load(path: Path, what: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"no such {what}: {path}")
    except ValueError as exc:
        sys.exit(f"{what} is not valid JSON ({path}): {exc}")


def item_from_incident(rec: dict, now: datetime, status: str | None = None) -> dict:
    status = status or INCIDENT_STATUS.get(rec.get("status", ""), "monitoring")
    if status not in STATUSES:
        raise ValueError(f"bad status: {status}")
    imp = rec.get("impact") or {}
    rows = [["Trading paused", imp.get("trading_paused", "n/a")],
            ["Portfolio vs floor", f"{imp.get('portfolio', 'n/a')} vs {imp.get('floor', 'n/a')}"],
            ["Missed ticks", imp.get("missed_ticks", "n/a")]]
    acts, props = [], []
    for a in rec.get("actions") or []:
        if a.get("kind") == "proposal":
            # only this repository's own PR URLs can ever be merged by an Accept: anything else is blanked here
            props.append({"title": a.get("text", ""), "pr_url": own_pr_url(a.get("link", "")), "risk": a.get("risk", "")})
        else:
            link = str(a.get("link") or "")
            acts.append({"text": a.get("text", ""), **({"url": link} if link.startswith("https://") else {})})
    sev = rec.get("severity") if rec.get("severity") in SEVERITIES else "medium"
    created = rec.get("opened_at") or _iso(now)
    return {
        "id": safe_id(rec["id"]), "kind": "incident", "severity": sev, "status": status, "revision": 1,
        "title": rec.get("title", ""), "summary": rec.get("summary", ""), "handled_by": rec.get("handled_by", ""),
        "what_wrong": rec.get("what") or rec.get("summary", ""), "log": list(rec.get("log") or [])[-30:],
        "impact": rows, "actions": acts, "proposals": props, "next": rec.get("next", ""),
        "report_url": rec.get("issue_url", ""), "created": created, "updated": _iso(now),
        "history": [{"at": _iso(now), "text": f"Incident item created ({status.replace('_', ' ')})"}], "sample": False,
    }


def item_from_daily(day: dict, now: datetime, report_url: str = "") -> dict:
    p = day.get("portfolio") or {}
    decisions = day.get("decisions") or []
    buys = sum(1 for d in decisions if d.get("action") == "BUY")
    sells = sum(1 for d in decisions if d.get("action") == "SELL")
    date = day["date"]
    d0 = datetime.fromisoformat(date)
    label = f"{d0.strftime('%a')} {d0.day} {d0.strftime('%b')}"
    closed = bool(day.get("closed"))
    pnl = float(p.get("day_pnl", 0) or 0)
    rows = [["Portfolio", inr(float(p.get("close", 0) or 0))], ["Day P&L", sg(pnl)],
            ["Floor", inr(float(p.get("floor", 0) or 0))],
            ["Cash in LIQUIDBEES", inr(float(p.get("cash_in_liquidbees", 0) or 0))],
            ["Trades", f"{buys + sells} ({buys} buys, {sells} sells)"]]
    summary = day.get("one_line_summary") or ("Market closed." if closed else f"Day P&L {sg(pnl)}.")
    return {
        "id": daily_item_id(date), "kind": "daily", "severity": "low", "status": "needs_approval", "revision": 1,
        "title": f"Daily report: {label}", "summary": summary, "handled_by": "daily_report routine (Sonnet)",
        "what_wrong": "", "log": [], "impact": rows, "actions": [], "proposals": [],
        "next": day.get("tomorrow", ""), "report_url": report_url or "", "created": _iso(now), "updated": _iso(now),
        "history": [{"at": _iso(now), "text": "Daily report sent"}], "sample": bool(day.get("_note")),
    }


def bump(item: dict, status: str, history: str, now: datetime) -> dict:
    if status not in STATUSES:
        raise ValueError(f"bad status: {status} (use one of {', '.join(STATUSES)})")
    if not is_valid_id(item.get("id")):
        raise ValueError(f"bad item id: {item.get('id')!r}")
    rev = int(item.get("revision", 1)) + 1
    if status == "needs_approval" and rev > MAX_REVISION:
        raise ValueError(f"revision cap: this item was already revised {MAX_REVISION - 1} times; bump it to monitoring "
                         f"and ask the owner for a human look instead")
    item["revision"] = rev
    item["status"] = status
    item["updated"] = _iso(now)
    item.setdefault("history", []).append({"at": _iso(now), "text": history})
    return item


REFRESH = ("severity", "title", "summary", "handled_by", "what_wrong", "log", "impact", "actions", "proposals", "next", "report_url")


def refresh_from_incident(item: dict, rec: dict, now: datetime) -> dict:
    fresh = item_from_incident(rec, now)
    for k in REFRESH:
        item[k] = fresh[k]
    return item


def _emit(item: dict, out: str | None) -> None:
    text = json.dumps(item, ensure_ascii=False, indent=1) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(json.dumps({"id": item["id"], "path": str(out), "status": item["status"], "revision": item["revision"]}))
    else:
        sys.stdout.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("item-from-incident")
    i.add_argument("--id", required=True); i.add_argument("--status", choices=STATUSES); i.add_argument("--root", default=str(ROOT))
    d = sub.add_parser("item-from-daily")
    d.add_argument("--date", required=True); d.add_argument("--report-url", default=""); d.add_argument("--root", default=str(ROOT))
    b = sub.add_parser("bump")
    b.add_argument("--file", required=True); b.add_argument("--status", required=True, choices=STATUSES)
    b.add_argument("--history", required=True); b.add_argument("--from-incident", metavar="ID"); b.add_argument("--from-daily", metavar="DATE")
    b.add_argument("--report-url", default=""); b.add_argument("--root", default=str(ROOT))
    for p in (i, d, b):
        p.add_argument("--now"); p.add_argument("--out", help="write here instead of stdout (bump: default is --file)")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    now = _now(a.now)
    for flag, val in (("--id", getattr(a, "id", None)), ("--from-incident", getattr(a, "from_incident", None))):
        if val is not None and not is_valid_id(val):
            ap.error(f"{flag} must match [A-Za-z0-9._~-]+ and not start with a dot")
    for flag, val in (("--date", getattr(a, "date", None)), ("--from-daily", getattr(a, "from_daily", None))):
        if val is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
            ap.error(f"{flag} must look like 2026-10-13")
    if a.cmd == "item-from-incident":
        item = item_from_incident(_load(Path(a.root) / "incidents" / f"{a.id}.json", "incident"), now, a.status)
    elif a.cmd == "item-from-daily":
        try:
            datetime.fromisoformat(a.date)
        except ValueError:
            ap.error("--date must look like 2026-10-13")
        item = item_from_daily(_load(Path(a.root) / "reports" / "daily" / f"{a.date}.json", "daily report data"), now, a.report_url)
    else:
        item = _load(Path(a.file), "item file")
        if a.from_incident:
            refresh_from_incident(item, _load(Path(a.root) / "incidents" / f"{a.from_incident}.json", "incident"), now)
        if a.from_daily:
            fresh = item_from_daily(_load(Path(a.root) / "reports" / "daily" / f"{a.from_daily}.json", "daily report data"), now, a.report_url)
            for k in ("title", "summary", "impact", "next"):
                item[k] = fresh[k]
            item["report_url"] = fresh["report_url"] or item.get("report_url", "")
        try:
            item = bump(item, a.status, a.history, now)
        except ValueError as exc:
            sys.exit(str(exc))
        a.out = a.out or a.file
    _emit(item, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
