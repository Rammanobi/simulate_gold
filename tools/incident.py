#!/usr/bin/env python3
"""Incident record CLI for the incident routine (so it never hand-crafts files).

  python tools/incident.py open --title T --severity low|medium|high --summary S [--workflow W --step S
        --what TEXT --log LINE|@file ... --paused yes|no --portfolio TEXT --floor TEXT --missed TEXT --next TEXT]
  python tools/incident.py add-action --id ID --text T --kind safe-fix|proposal|info [--link URL]
  python tools/incident.py set-status --id ID resolved|needs-approval|monitoring
  python tools/incident.py set --id ID [--what ..] [--log ..] [--paused ..] [--portfolio ..] [--floor ..] [--missed ..] [--next ..]
  python tools/incident.py render --id ID     # writes incidents/<id>.email.html/.txt, prints JSON
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "incidents"
STATUSES = {
    "resolved": ("Resolved", "#1e7b4b", "#e7f5ed"),
    "needs-approval": ("Needs your approval", "#b26a00", "#fff4de"),
    "monitoring": ("Monitoring", "#5b6470", "#eef0f3"),
}
SEVERITIES = ("low", "medium", "high")
KINDS = ("safe-fix", "proposal", "info")
IST = timezone(timedelta(hours=5, minutes=30))


def e(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (s[:40].strip("-")) or "incident"


def path_for(iid: str, d: Path | None = None) -> Path:
    return (d or DIR) / f"{iid}.json"


def load(iid: str, d: Path | None = None) -> dict:
    p = path_for(iid, d)
    if not p.exists():
        sys.exit(f"no such incident: {iid}")
    return json.loads(p.read_text(encoding="utf-8"))


def save(rec: dict, d: Path | None = None) -> None:
    p = path_for(rec["id"], d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _log_lines(items) -> list[str]:
    out: list[str] = []
    for it in items or []:
        if it.startswith("@"):
            out += Path(it[1:]).read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            out.append(it)
    return [ln[:300] for ln in out][-30:]


def new_record(title, severity, summary, workflow="", step="", now=None, **kw) -> dict:
    now = now or datetime.now(IST)
    return {
        "id": f"{now.date().isoformat()}-{slugify(title)}", "title": title, "severity": severity, "summary": summary,
        "status": "monitoring", "opened_at": now.isoformat(timespec="seconds"), "workflow": workflow, "step": step,
        "what": kw.get("what") or summary, "log": _log_lines(kw.get("log")),
        "impact": {"trading_paused": kw.get("paused") or "unknown", "portfolio": kw.get("portfolio") or "n/a",
                   "floor": kw.get("floor") or "n/a", "missed_ticks": kw.get("missed") or "unknown"},
        "next": kw.get("next") or "", "actions": [], "emailed_at": None,
        "tier": kw.get("tier") or "", "handled_by": kw.get("handled") or "",
    }


# ---------------------------------------------------------------- rendering
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def short_title(t: str, n: int = 60) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def subject_for(rec: dict) -> str:
    label = STATUSES[rec["status"]][0]
    return f"Metals Daybook · ⚠️ Incident · {short_title(rec['title'])} · {label}"


def _section(title: str, body: str) -> str:
    return (f'<tr><td style="padding:18px 24px 4px 24px;font:700 13px {FONT};letter-spacing:.06em;text-transform:uppercase;'
            f'color:#5b6470;">{e(title)}</td></tr><tr><td style="padding:4px 24px 8px 24px;font:15px/1.55 {FONT};color:#1d2430;">{body}</td></tr>')


def _action_li(a: dict) -> str:
    link = (f' &nbsp;<a href="{e(a["link"])}" style="color:#1a5fb4;text-decoration:underline;">commit</a>' if a.get("link") else "")
    return (f'<tr><td width="24" valign="top" style="font:15px {FONT};color:#1e7b4b;padding:3px 0;">&#10003;</td>'
            f'<td style="font:15px/1.5 {FONT};color:#1d2430;padding:3px 0;">{e(a["text"])}{link}</td></tr>')


def render_html(rec: dict) -> str:
    label, fg, bg = STATUSES[rec["status"]]
    acts = rec.get("actions", [])
    safe = [a for a in acts if a["kind"] == "safe-fix"]
    props = [a for a in acts if a["kind"] == "proposal"]
    infos = [a for a in acts if a["kind"] == "info"]
    imp = rec.get("impact", {})
    parts = [f'<p style="margin:0 0 10px 0;">{e(rec.get("what") or rec["summary"])}</p>']
    if rec.get("log"):
        parts.append(f'<div style="background:#12161c;color:#d7e0ea;border-radius:8px;padding:12px 14px;'
                     f'font:12px/1.5 Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word;">'
                     f'{e(chr(10).join(rec["log"]))}</div>')
    went_wrong = "".join(parts)
    rows = [("Trading paused", imp.get("trading_paused")), ("Portfolio vs floor", f'{imp.get("portfolio")} vs {imp.get("floor")}'),
            ("Missed ticks", imp.get("missed_ticks"))]
    impact = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">' +
              "".join(f'<tr><td style="padding:6px 0;border-bottom:1px solid #e6e9ee;font:14px {FONT};color:#5b6470;width:42%;">{e(k)}</td>'
                      f'<td style="padding:6px 0;border-bottom:1px solid #e6e9ee;font:600 14px {FONT};color:#1d2430;">{e(v)}</td></tr>'
                      for k, v in rows) + "</table>")
    did_rows = "".join(_action_li(a) for a in safe + infos)
    did = (f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">{did_rows}</table>' if did_rows
           else '<span style="color:#5b6470;">Nothing changed yet.</span>')
    body_rows = [_section("What went wrong", went_wrong), _section("Impact", impact), _section("What I did", did)]
    if props:
        cards = ""
        for a in props:
            btn = (f'<a href="{e(a["link"])}" style="display:block;background:#b26a00;color:#ffffff;text-decoration:none;'
                   f'text-align:center;font:700 16px {FONT};padding:14px 18px;border-radius:8px;margin-top:10px;">Review the pull request</a>'
                   if a.get("link") else "")
            risk = f'<div style="font:13px/1.45 {FONT};color:#5b6470;margin-top:8px;">If not approved: {e(a.get("risk") or "the problem stays as described above.")}</div>'
            cards += (f'<div style="border:1px solid #f0d9a8;background:#fffaf0;border-radius:10px;padding:14px;margin-bottom:10px;">'
                      f'<div style="font:600 15px/1.45 {FONT};color:#1d2430;">{e(a["text"])}</div>{btn}{risk}</div>')
        body_rows.append(_section("What needs your approval", cards))
    nxt = rec.get("next") or ("I will keep watching the next runs." if rec["status"] != "resolved" else "Nothing further is needed.")
    body_rows.append(_section("What happens next", f'<p style="margin:0;">{e(nxt)}</p>'))
    hb = rec.get("handled_by") or ""
    handled_html = (f'<div style="font:13px {FONT};color:#5b6470;margin-top:8px;">Handled by: {e(hb)}'
                    f'{" (" + e(rec["tier"]) + ")" if rec.get("tier") else ""}</div>') if hb else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{e(subject_for(rec))}</title></head>
<body style="margin:0;padding:0;background:#f3f5f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f5f8;"><tr><td align="center" style="padding:16px 8px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e1e5ea;">
<tr><td style="background:#12161c;padding:16px 24px;font:700 18px {FONT};color:#f4c95d;">Metals Daybook <span style="font-weight:400;color:#9aa6b4;font-size:13px;">&middot; incident report</span></td></tr>
<tr><td style="background:{bg};border-left:6px solid {fg};padding:16px 24px;">
<div style="font:700 12px {FONT};letter-spacing:.08em;text-transform:uppercase;color:{fg};">{e(label)} &middot; severity {e(rec["severity"])}</div>
<div style="font:700 20px/1.3 {FONT};color:#1d2430;margin-top:4px;">{e(rec["title"])}</div>
<div style="font:15px/1.5 {FONT};color:#1d2430;margin-top:6px;">{e(rec["summary"])}</div>{handled_html}</td></tr>
{"".join(body_rows)}
<tr><td style="padding:20px 24px;border-top:1px solid #e6e9ee;font:12px/1.5 {FONT};color:#8a94a1;">Incident {e(rec["id"])} &middot; opened {e(rec["opened_at"])}<br>
Paper-trading simulator &mdash; no real money is involved. Automated report from the incident routine.</td></tr>
</table></td></tr></table></body></html>
"""


def render_text(rec: dict) -> str:
    label = STATUSES[rec["status"]][0]
    imp = rec.get("impact", {})
    L = [subject_for(rec), "", f"{label.upper()} (severity {rec['severity']}): {rec['summary']}", "", "WHAT WENT WRONG",
         rec.get("what") or rec["summary"]]
    if rec.get("log"):
        L += ["", *["  " + x for x in rec["log"]]]
    if rec.get("handled_by"):
        L += ["", f"Handled by: {rec['handled_by']}" + (f" ({rec['tier']})" if rec.get("tier") else "")]
    L += ["", "IMPACT", f"- Trading paused: {imp.get('trading_paused')}", f"- Portfolio vs floor: {imp.get('portfolio')} vs {imp.get('floor')}",
          f"- Missed ticks: {imp.get('missed_ticks')}", "", "WHAT I DID"]
    done = [a for a in rec.get("actions", []) if a["kind"] != "proposal"]
    L += [f"- [x] {a['text']}" + (f" ({a['link']})" if a.get("link") else "") for a in done] or ["- nothing changed yet"]
    props = [a for a in rec.get("actions", []) if a["kind"] == "proposal"]
    if props:
        L += ["", "WHAT NEEDS YOUR APPROVAL"]
        for a in props:
            L += [f"- {a['text']}", f"  PR: {a.get('link') or 'n/a'}", f"  If not approved: {a.get('risk') or 'the problem stays as described.'}"]
    L += ["", "WHAT HAPPENS NEXT", rec.get("next") or "I will keep watching the next runs.", "", f"Incident {rec['id']}. Paper trading only."]
    return "\n".join(L) + "\n"


def render(rec: dict, d: Path | None = None) -> dict:
    d = d or DIR
    d.mkdir(parents=True, exist_ok=True)
    hp, tp = d / f"{rec['id']}.email.html", d / f"{rec['id']}.email.txt"
    hp.write_text(render_html(rec), encoding="utf-8")
    tp.write_text(render_text(rec), encoding="utf-8")
    return {"subject": subject_for(rec), "html_path": str(hp), "text_path": str(tp)}


# ---------------------------------------------------------------- CLI
def _apply_fields(rec: dict, a) -> None:
    if getattr(a, "what", None):
        rec["what"] = a.what
    if getattr(a, "log", None):
        rec["log"] = _log_lines(a.log)
    for arg, key in (("paused", "trading_paused"), ("portfolio", "portfolio"), ("floor", "floor"), ("missed", "missed_ticks")):
        if getattr(a, arg, None):
            rec["impact"][key] = getattr(a, arg)
    if getattr(a, "next", None):
        rec["next"] = a.next
    if getattr(a, "emailed", False):
        rec["emailed_at"] = datetime.now(IST).isoformat(timespec="seconds")
    if getattr(a, "tier", None):
        rec["tier"] = a.tier
    if getattr(a, "handled", None):
        rec["handled_by"] = a.handled


def _fields(p) -> None:
    p.add_argument("--what"); p.add_argument("--log", action="append"); p.add_argument("--paused")
    p.add_argument("--portfolio"); p.add_argument("--floor"); p.add_argument("--missed"); p.add_argument("--next")
    p.add_argument("--tier", choices=["TRIVIAL", "STANDARD", "COMPLEX"]); p.add_argument("--handled", help="e.g. 'Sonnet triage -> Opus fix'")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open"); o.add_argument("--title", required=True)
    o.add_argument("--severity", required=True, choices=SEVERITIES); o.add_argument("--summary", required=True)
    o.add_argument("--workflow", default=""); o.add_argument("--step", default=""); _fields(o)
    x = sub.add_parser("add-action"); x.add_argument("--id", required=True); x.add_argument("--text", required=True)
    x.add_argument("--kind", required=True, choices=KINDS); x.add_argument("--link"); x.add_argument("--risk")
    s = sub.add_parser("set-status"); s.add_argument("--id", required=True); s.add_argument("status", choices=list(STATUSES))
    st = sub.add_parser("set"); st.add_argument("--id", required=True); _fields(st)
    st.add_argument("--emailed", action="store_true", help="stamp emailed_at (after sending the email)")
    r = sub.add_parser("render"); r.add_argument("--id", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "open":
        rec = new_record(a.title, a.severity, a.summary, a.workflow, a.step, what=a.what, log=a.log, paused=a.paused,
                         portfolio=a.portfolio, floor=a.floor, missed=a.missed, next=a.next)
        save(rec); print(json.dumps({"id": rec["id"], "path": str(path_for(rec["id"]))}))
    else:
        rec = load(a.id)
        if a.cmd == "add-action":
            act = {"text": a.text, "kind": a.kind}
            if a.link: act["link"] = a.link
            if a.risk: act["risk"] = a.risk
            rec["actions"].append(act)
        elif a.cmd == "set-status":
            rec["status"] = a.status
        elif a.cmd == "set":
            _apply_fields(rec, a)
        elif a.cmd == "render":
            print(json.dumps(render(rec))); return 0
        save(rec); print(json.dumps({"id": rec["id"], "status": rec["status"], "actions": len(rec["actions"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
