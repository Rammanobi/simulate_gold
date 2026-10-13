#!/usr/bin/env python3
"""Incident record CLI for the incident routine (so it never hand-crafts files).

  open --title T --severity low|medium|high --summary S [--workflow W --step S --category C --error E
        --what TEXT --log LINE|@file ... --paused ... --portfolio ... --floor ... --missed ... --next ...]
  open-issue --id ID            create the GitHub issue "[HIGH] title" (label incident) the owner acks on
  add-action --id ID --text T --kind safe-fix|proposal|info [--link URL] [--risk SENTENCE]
  set-status --id ID resolved|needs-approval|monitoring
  set --id ID [--what .. --log .. --paused .. --portfolio .. --floor .. --missed .. --next .. --tier .. --handled ..]
      [--fix-failed] [--emailed]            (--emailed stamps the send time, or marks the Resolved email as sent)
  should-email --id ID          prints "SEND <mode>: reason" or "HOLD: reason" (mode: incident|reminder|still-broken|resolved)
  ack --id ID --via email|github            stop HIGH reminders (an ACK reply from REPORT_EMAIL, in the same thread)
  clear-safe-mode --reason TEXT             only after a verified fix
  render --id ID [--mode incident|reminder|still-broken|resolved]   -> JSON {subject, html_path, text_path}
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from simgold import alerts  # noqa: E402

DIR = ROOT / "incidents"
STATE = ROOT / "state"
REPO = "Rammanobi/simulate_gold"
# status -> (label, ink, background); same palette as the daily report (green #1a7f4b, gold #8a6d12, grey #6b7280)
STATUSES = {
    "resolved": ("Resolved", "#1a7f4b", "#e8f4ee"),
    "needs-approval": ("Needs your approval", "#8a6d12", "#fbf6e3"),
    "monitoring": ("Monitoring", "#6b7280", "#f3f4f6"),
}
SEVERITIES = ("low", "medium", "high")
KINDS = ("safe-fix", "proposal", "info")
MODES = ("incident", "reminder", "still-broken", "resolved")
IST = timezone(timedelta(hours=5, minutes=30))
INK, GREY, GOLD, GREEN, RED = "#1f2937", "#6b7280", "#c9a227", "#1a7f4b", "#c0392b"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
DEFAULT_CATEGORY = {"high": "engine_not_running", "medium": "workflow_failed", "low": "warning"}


def e(x) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def inr(n: float) -> str:
    """Indian digit grouping: 100412 -> ₹1,00,412."""
    s = f"{abs(round(n)):d}"
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:]); head = head[:-2]
    if head:
        groups.insert(0, head)
    return ("-" if n < 0 else "") + "₹" + ",".join(groups + [tail])


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
        "fingerprint": kw.get("fingerprint") or alerts.fingerprint(workflow or "manual", step or title, kw.get("error") or ""),
        "what": kw.get("what") or summary, "log": _log_lines(kw.get("log")),
        "impact": {"trading_paused": kw.get("paused") or "unknown", "portfolio": kw.get("portfolio") or "n/a",
                   "floor": kw.get("floor") or "n/a", "missed_ticks": kw.get("missed") or "unknown"},
        "next": kw.get("next") or "", "actions": [], "emailed_at": None, "fix_failed": False,
        "issue_url": "", "issue": None, "tier": kw.get("tier") or "", "handled_by": kw.get("handled") or "",
    }


# ---------------------------------------------------------------- rendering
def short_title(t: str, n: int = 60) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def subject_for(rec: dict, mode: str = "incident", n: int = 0) -> str:
    """Always starts with "Metals Daybook" (the Gmail hook requires it)."""
    label, title = STATUSES[rec["status"]][0], short_title(rec["title"])
    if mode == "resolved":
        return f"Metals Daybook · ✅ Resolved · {title} · Resolved"
    if mode == "reminder":
        return f"Metals Daybook · 🔁 Reminder #{n} · ⚠️ Incident · {title} · {label}"
    if mode == "still-broken":
        return f"Metals Daybook · ⚠️ Still broken · {title} · {label}"
    return f"Metals Daybook · ⚠️ Incident · {title} · {label}"


def _hhmm(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), IST).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _section(title: str, body: str) -> str:
    return (f'<tr><td style="padding:26px 20px 8px 20px;font-family:{FONT};font-size:13px;letter-spacing:1px;text-transform:uppercase;'
            f'font-weight:700;color:{GREY};border-top:1px solid #e5e7eb;">{e(title)}</td></tr>'
            f'<tr><td style="padding:0 20px;font-family:{FONT};font-size:15px;line-height:23px;color:{INK};">{body}</td></tr>')


def _button(href: str, label: str, kind: str = "dark") -> str:
    if kind == "dark":
        sty = f"background:{INK};color:#ffffff;border-bottom:3px solid {GOLD};"
    elif kind == "green":
        sty = f"background:{GREEN};color:#ffffff;border-bottom:3px solid #14633b;"
    else:
        sty = f"background:#ffffff;color:{RED};border:1px solid {RED};"
    return (f'<a href="{e(href)}" style="display:block;{sty}text-decoration:none;text-align:center;font-family:{FONT};'
            f'font-size:16px;font-weight:700;padding:14px 18px;border-radius:8px;margin-top:10px;">{e(label)}</a>')


def _action_row(a: dict) -> str:
    link = (f' &nbsp;<a href="{e(a["link"])}" style="color:#1d4ed8;text-decoration:underline;">commit</a>' if a.get("link") else "")
    return (f'<tr><td width="24" valign="top" style="font-family:{FONT};font-size:15px;color:{GREEN};padding:4px 0;">&#10003;</td>'
            f'<td style="font-family:{FONT};font-size:15px;line-height:22px;color:{INK};padding:4px 0;">{e(a["text"])}{link}</td></tr>')


def render_html(rec: dict, mode: str = "incident", n: int = 0, first_sent: float | None = None,
                related: list | None = None) -> str:
    st = "resolved" if mode == "resolved" else rec["status"]
    label, fg, bg = STATUSES[st]
    acts = rec.get("actions", [])
    safe = [a for a in acts if a["kind"] == "safe-fix"]
    props = [a for a in acts if a["kind"] == "proposal"]
    infos = [a for a in acts if a["kind"] == "info"]
    imp = rec.get("impact", {})
    high = rec.get("severity") == "high"
    parts = [f'<p style="margin:0 0 10px 0;">{e(rec.get("what") or rec["summary"])}</p>']
    if rec.get("log"):
        parts.append(f'<div style="background:#111827;color:#e5e7eb;border-radius:8px;padding:12px 14px;font-family:Menlo,Consolas,monospace;'
                     f'font-size:12px;line-height:18px;white-space:pre-wrap;word-break:break-word;">{e(chr(10).join(rec["log"]))}</div>')
    rows = [("Trading paused", imp.get("trading_paused")), ("Portfolio vs floor", f'{imp.get("portfolio")} vs {imp.get("floor")}'),
            ("Missed ticks", imp.get("missed_ticks"))]
    impact = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + "".join(
        f'<tr><td style="padding:7px 0;border-top:1px solid #f1f1f1;font-family:{FONT};font-size:14px;color:{GREY};width:42%;">{e(k)}</td>'
        f'<td style="padding:7px 0;border-top:1px solid #f1f1f1;font-family:{FONT};font-size:14px;font-weight:700;color:{INK};">{e(v)}</td></tr>'
        for k, v in rows) + "</table>")
    did_rows = "".join(_action_row(a) for a in safe + infos)
    did = (f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">{did_rows}</table>' if did_rows
           else f'<span style="color:{GREY};">Nothing changed yet.</span>')
    body = [_section("What went wrong", "".join(parts)), _section("Impact", impact), _section("What I did", did)]
    if related:
        li = "".join(f'<li style="padding:2px 0;">{e(r.get("step"))} ({e(r.get("severity"))}): {e(r.get("detail", "")[:140])}</li>' for r in related[:6])
        body.append(_section("Also happening", f'<ul style="margin:0;padding-left:18px;font-size:14px;color:{INK};">{li}</ul>'))
    if props and mode != "resolved":
        cards = ""
        for a in props:
            btns = ""
            if a.get("link"):
                btns = _button(a["link"], "Review & merge the fix", "green") + _button(a["link"], "Reject (close the PR)", "outline")
            risk = (f'<div style="font-size:13px;line-height:19px;color:{GREY};padding-top:10px;">If not approved: '
                    f'{e(a.get("risk") or "the problem stays as described above.")}</div>')
            cards += (f'<div style="border:1px solid #e5d9a8;background:#fbf6e3;border-radius:10px;padding:14px;margin-bottom:10px;">'
                      f'<div style="font-weight:700;">{e(a["text"])}</div>{btns}{risk}</div>')
        cards += (f'<div style="font-size:12px;line-height:18px;color:{GREY};">Only merging the pull request on GitHub approves a code change. '
                  f'Replying to this email can only acknowledge it, never approve anything.</div>')
        body.append(_section("What needs your approval", cards))
    if high and rec.get("issue_url") and mode != "resolved":
        ack = (_button(rec["issue_url"], "✅ I've seen it, stop reminders", "dark") +
               f'<div style="font-size:13px;line-height:19px;color:{GREY};padding-top:8px;">Or reply ACK to this email, comment ack on the '
               f'issue, or close it. The incident stays open until it is fixed. Reminders: every hour until acknowledged.</div>')
        body.append(_section("Acknowledge", ack))
    nxt = rec.get("next") or ("I will keep watching the next runs." if st != "resolved" else "Nothing further is needed.")
    body.append(_section("What happens next", f'<p style="margin:0;">{e(nxt)}</p>'))
    kicker = {"incident": "Incident", "reminder": f"Reminder #{n}", "still-broken": "Still broken", "resolved": "Resolved"}[mode]
    extra = ""
    if mode == "reminder":
        extra = (f'<div style="font-size:14px;line-height:21px;color:{INK};padding-top:8px;font-weight:700;">'
                 f'First sent {e(_hhmm(first_sent))}, no response yet.</div>')
    handled = ""
    if rec.get("handled_by"):
        handled = (f'<div style="font-size:13px;color:{GREY};padding-top:8px;">Handled by: {e(rec["handled_by"])}'
                   f'{" (" + e(rec["tier"]) + ")" if rec.get("tier") else ""}</div>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>{e(subject_for(rec, mode, n))}</title></head>
<body style="margin:0;padding:0;background:#f1f0ec;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f0ec;"><tr><td align="center" style="padding:16px 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;border-top:4px solid {GOLD};">
<tr><td style="padding:20px 20px 0 20px;font-family:{FONT};"><div style="font-size:14px;color:{GREY};">Metals Daybook &middot; incident report &middot; severity {e(rec["severity"])}</div></td></tr>
<tr><td style="padding:12px 20px 4px 20px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{bg};border-radius:10px;border-left:6px solid {fg};"><tr><td style="padding:14px 16px;font-family:{FONT};">
<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;font-weight:700;color:{fg};">{e(kicker)} &middot; {e(label)}</div>
<div style="font-size:24px;line-height:30px;font-weight:800;color:{INK};padding-top:4px;">{e(rec["title"])}</div>
<div style="font-size:15px;line-height:23px;color:{INK};padding-top:6px;">{e(rec["summary"])}</div>{extra}{handled}</td></tr></table></td></tr>
{"".join(body)}
<tr><td align="center" style="padding:26px 20px 24px 20px;font-family:{FONT};font-size:12px;line-height:18px;color:{GREY};">Incident {e(rec["id"])}<br>Paper trading simulation. No real money is involved. Automated report from the incident routine.</td></tr>
</table></td></tr></table></body></html>
"""


def render_text(rec: dict, mode: str = "incident", n: int = 0, first_sent: float | None = None, related: list | None = None) -> str:
    st = "resolved" if mode == "resolved" else rec["status"]
    imp = rec.get("impact", {})
    L = [subject_for(rec, mode, n), ""]
    if mode == "reminder":
        L += [f"First sent {_hhmm(first_sent)}, no response yet.", ""]
    L += [f"{STATUSES[st][0].upper()} (severity {rec['severity']}): {rec['summary']}", "", "WHAT WENT WRONG", rec.get("what") or rec["summary"]]
    if rec.get("log"):
        L += ["", *["  " + x for x in rec["log"]]]
    if rec.get("handled_by"):
        L += ["", f"Handled by: {rec['handled_by']}" + (f" ({rec['tier']})" if rec.get("tier") else "")]
    L += ["", "IMPACT", f"- Trading paused: {imp.get('trading_paused')}", f"- Portfolio vs floor: {imp.get('portfolio')} vs {imp.get('floor')}",
          f"- Missed ticks: {imp.get('missed_ticks')}", "", "WHAT I DID"]
    done = [a for a in rec.get("actions", []) if a["kind"] != "proposal"]
    L += [f"- [x] {a['text']}" + (f" ({a['link']})" if a.get("link") else "") for a in done] or ["- nothing changed yet"]
    if related:
        L += ["", "ALSO HAPPENING"] + [f"- {r.get('step')} ({r.get('severity')}): {str(r.get('detail', ''))[:140]}" for r in related[:6]]
    props = [a for a in rec.get("actions", []) if a["kind"] == "proposal"]
    if props and mode != "resolved":
        L += ["", "WHAT NEEDS YOUR APPROVAL"]
        for a in props:
            L += [f"- {a['text']}", f"  Review & merge the fix / Reject (close the PR): {a.get('link') or 'n/a'}",
                  f"  If not approved: {a.get('risk') or 'the problem stays as described.'}"]
        L += ["  Only merging the PR approves a change; replying to this email can only acknowledge."]
    if rec.get("severity") == "high" and rec.get("issue_url") and mode != "resolved":
        L += ["", "ACKNOWLEDGE", f"Stop reminders: {rec['issue_url']} (comment ack, close it, or reply ACK to this email)."]
    L += ["", "WHAT HAPPENS NEXT", rec.get("next") or "I will keep watching the next runs.", "", f"Incident {rec['id']}. Paper trading only."]
    return "\n".join(L) + "\n"


def render(rec: dict, d: Path | None = None, mode: str = "incident", state_dir: Path | None = None) -> dict:
    d = d or DIR
    d.mkdir(parents=True, exist_ok=True)
    sd = state_dir or STATE
    first, n, related = None, 0, []
    try:
        st = alerts.load_state(sd)
        ent = st["alerts"].get(rec.get("fingerprint", "")) or {}
        if ent.get("emails"):
            first, n = ent["emails"][0], len(ent["emails"])
        related = alerts.take_pending(sd, exclude_fp=rec.get("fingerprint", ""))
    except Exception:  # noqa: BLE001
        pass
    if mode not in ("reminder",):
        n = n if mode != "incident" else 0
    hp, tp = d / f"{rec['id']}.email.html", d / f"{rec['id']}.email.txt"
    hp.write_text(render_html(rec, mode, n, first, related), encoding="utf-8")
    tp.write_text(render_text(rec, mode, n, first, related), encoding="utf-8")
    return {"subject": subject_for(rec, mode, n), "html_path": str(hp), "text_path": str(tp)}


# ---------------------------------------------------------------- GitHub issue
def open_issue(rec: dict, runner=None) -> dict:
    """REST only (GraphQL is unavailable to the routine). Title "[HIGH] <title>", body = summary + incident id."""
    runner = runner or (lambda args: subprocess.run(["gh", *args], capture_output=True, text=True, check=True, timeout=60).stdout)
    out = runner(["api", f"repos/{REPO}/issues", "-f", f"title=[{rec['severity'].upper()}] {rec['title']}",
                  "-f", f"body={rec['summary']}\n\nIncident id: {rec['id']}\n\nAcknowledge with a comment `ack`, the `ack` label, or by closing this issue.",
                  "-f", "labels[]=incident"])
    d = json.loads(out)
    rec["issue"], rec["issue_url"] = d.get("number"), d.get("html_url", "")
    return rec


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
    if getattr(a, "tier", None):
        rec["tier"] = a.tier
    if getattr(a, "handled", None):
        rec["handled_by"] = a.handled
    if getattr(a, "fix_failed", False):
        rec["fix_failed"] = True


def _fields(p) -> None:
    p.add_argument("--what"); p.add_argument("--log", action="append"); p.add_argument("--paused")
    p.add_argument("--portfolio"); p.add_argument("--floor"); p.add_argument("--missed"); p.add_argument("--next")
    p.add_argument("--tier", choices=["TRIVIAL", "STANDARD", "COMPLEX"]); p.add_argument("--handled", help="e.g. 'Sonnet triage -> Opus fix'")


def _entry_for(rec: dict, sd: Path) -> dict:
    st = alerts.load_state(sd)
    ent = st["alerts"].get(rec.get("fingerprint", ""))
    if ent:
        return ent
    return {"fp": rec.get("fingerprint", ""), "severity": rec["severity"], "status": "resolved" if rec["status"] == "resolved" else "open",
            "emails": [], "acked": False, "resolved_emailed": False}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open"); o.add_argument("--title", required=True)
    o.add_argument("--severity", required=True, choices=SEVERITIES); o.add_argument("--summary", required=True)
    o.add_argument("--workflow", default=""); o.add_argument("--step", default=""); o.add_argument("--category")
    o.add_argument("--error", default=""); o.add_argument("--fingerprint"); _fields(o)
    x = sub.add_parser("add-action"); x.add_argument("--id", required=True); x.add_argument("--text", required=True)
    x.add_argument("--kind", required=True, choices=KINDS); x.add_argument("--link"); x.add_argument("--risk")
    s = sub.add_parser("set-status"); s.add_argument("--id", required=True); s.add_argument("status", choices=list(STATUSES))
    st = sub.add_parser("set"); st.add_argument("--id", required=True); _fields(st)
    st.add_argument("--fix-failed", action="store_true"); st.add_argument("--emailed", action="store_true")
    se = sub.add_parser("should-email"); se.add_argument("--id", required=True)
    ak = sub.add_parser("ack"); ak.add_argument("--id", required=True); ak.add_argument("--via", default="email")
    cs = sub.add_parser("clear-safe-mode"); cs.add_argument("--reason", required=True)
    oi = sub.add_parser("open-issue"); oi.add_argument("--id", required=True)
    r = sub.add_parser("render"); r.add_argument("--id", required=True); r.add_argument("--mode", choices=MODES, default="incident")
    a = ap.parse_args(argv)
    now = time.time()
    if a.cmd == "open":
        rec = new_record(a.title, a.severity, a.summary, a.workflow, a.step, what=a.what, log=a.log, paused=a.paused,
                         portfolio=a.portfolio, floor=a.floor, missed=a.missed, next=a.next, error=a.error,
                         fingerprint=a.fingerprint, tier=a.tier, handled=a.handled)
        save(rec)
        ent = alerts.load_state(STATE)["alerts"].get(rec["fingerprint"])
        if ent is None:
            alerts.raise_alert(STATE, a.workflow or "manual", a.step or a.title, a.category or DEFAULT_CATEGORY[a.severity],
                               a.summary, a.error, wake=False)
        alerts.attach_incident(STATE, rec["fingerprint"], rec["id"], severity=a.severity)
        print(json.dumps({"id": rec["id"], "path": str(path_for(rec["id"])), "fingerprint": rec["fingerprint"]}))
        return 0
    if a.cmd == "clear-safe-mode":
        print(json.dumps({"cleared": alerts.clear_safe_mode(STATE, a.reason)})); return 0
    rec = load(a.id)
    fp = rec.get("fingerprint", "")
    if a.cmd == "add-action":
        act = {"text": a.text, "kind": a.kind}
        if a.link: act["link"] = a.link
        if a.risk: act["risk"] = a.risk
        rec["actions"].append(act)
    elif a.cmd == "set-status":
        rec["status"] = a.status
        if a.status == "resolved":
            alerts.resolve(STATE, fp, now)
    elif a.cmd == "set":
        _apply_fields(rec, a)
        if a.emailed:
            rec["emailed_at"] = datetime.now(IST).isoformat(timespec="seconds")
            if rec["status"] == "resolved":
                alerts.mark_resolved_emailed(STATE, fp)
            else:
                alerts.record_email(STATE, fp, now)
    elif a.cmd == "open-issue":
        open_issue(rec)
        alerts.attach_incident(STATE, fp, rec["id"], issue=rec["issue"], issue_url=rec["issue_url"])
        print(json.dumps({"issue": rec["issue"], "url": rec["issue_url"]}))
    elif a.cmd == "ack":
        print(json.dumps({"acked": alerts.ack(STATE, fp, a.via, now)})); return 0
    elif a.cmd == "should-email":
        ent = _entry_for(rec, STATE)
        verdict, mode, why = alerts.should_email(ent, now, fix_failed=rec.get("fix_failed", False), status=rec["status"])
        print(f"SEND {mode}: {why}" if verdict == "SEND" else f"HOLD: {why}"); return 0
    elif a.cmd == "render":
        print(json.dumps(render(rec, mode=a.mode))); return 0
    save(rec)
    if a.cmd != "open-issue":
        print(json.dumps({"id": rec["id"], "status": rec["status"], "actions": len(rec["actions"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
