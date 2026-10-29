"""Alert policy: fingerprints, severities, the routine-wake budget, grouping, reminders, acks and safe mode.

State lives in state/alerts.json (shared by the engine, workflows and the routine, always written atomically)
and state/safe_mode.json. Nothing here may raise into the trading loop; CLI:

  python -m simgold.alerts gate --component C --step S --category CAT [--error E] [--detail D]
  python -m simgold.alerts problems FILE        # healthcheck output (JSON lines) -> one grouped decision
  python -m simgold.alerts reminders --repo R   # record acks from GitHub, list HIGH incidents due a reminder
  python -m simgold.alerts resolve-missing --component C --current a,b,c
All CLI commands print one JSON object. Times are epoch seconds; days are IST dates.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("alerts")
IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
BETA = "experimental-cc-routine-2026-04-01"
HIGH, MEDIUM, LOW = "high", "medium", "low"

# category -> severity. This table IS the policy (owner-approved); change it only with the owner.
SEVERITY = {
    # HIGH
    "engine_not_running": HIGH,       # engine not running during market hours (also: no fresh tick)
    "tick_exceptions": HIGH,          # every tick throws, so exits, stops and the floor are not enforced
    "floor_broken": HIGH,             # floor invariant broken (value under floor-slack, floor lowered)
    "near_floor": HIGH,               # value within INR 100 of the floor, or under it
    "account_stop": HIGH,             # strategy v2: value at or under the hard account stop (INR 75,000): buys halted
    "state_corrupt": HIGH,            # state corrupted or unrecoverable
    "test_failure_main": HIGH,        # a test failure on main
    "git_push_failing": HIGH,         # git push failing for more than 15 min
    "watchdog_stale": HIGH,           # the watchdog itself stopped running (dead-man's switch)
    "system_unstable": HIGH,          # safe mode switched on
    "routines_silent": HIGH,          # the Claude routines have stopped (usage limit or credits?)
    # MEDIUM
    "data_source_down": MEDIUM,       # a data source down for more than 10 min
    "workflow_failed": MEDIUM,        # a failed workflow run
    "stale_guidance": MEDIUM,         # guidance older than 3 h in market hours
    "memory_cap": MEDIUM,             # a memory / log size cap exceeded
    "invariant_warning": MEDIUM,      # e.g. LIQUIDBEES share below 30% without a tripwire
    "allocation_stuck": MEDIUM,       # strategy v2: metals weight > 10 points off target at 2 daily checks in a row (never safe mode)
    # LOW
    "transient_fetch": LOW,           # a single transient fetch error
    "slow_run": LOW,
    "warning": LOW,
}
NO_SAFE_MODE = ("system_unstable", "allocation_stuck")      # alerts that never count toward switching safe mode on
DEFAULTS = {
    "max_fires_per_day": 6, "group_window_min": 15, "medium_reminder_hours": [1, 4], "medium_reminder_daily_hours": 24,
    "high_reminder_min": 60, "high_reminder_due_min": 55, "max_reminders_per_incident": 24, "max_medium_reminders": 10, "tick_exceptions": 3,
    "no_prices_min": 10, "git_push_fail_min": 15, "stale_guidance_hours": 3, "safe_mode_incidents": 5,
    "safe_mode_fix_window_min": 30, "watchdog_stale_min": 30, "near_floor_inr": 100,
}


def params(path=None) -> dict:
    cfg = dict(DEFAULTS)
    try:
        import yaml
        with open(path or ROOT / "config" / "params.yaml", encoding="utf-8") as fh:
            cfg.update((yaml.safe_load(fh) or {}).get("alerts") or {})
    except Exception:  # noqa: BLE001 - defaults are fine
        pass
    return cfg


def severity_of(category: str) -> str:
    return SEVERITY.get(category, MEDIUM)


def day_of(now: float) -> str:
    return datetime.fromtimestamp(now, IST).date().isoformat()


# ---------------------------------------------------------------- fingerprints
_NORMALISERS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}([t ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(z|[+-]\d{2}:?\d{2})?)?"), " "),   # timestamps
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"), " "),
    (re.compile(r"(?:[a-z]:)?(?:[\\/][\w.\-~@+]+){2,}"), " "),                                       # paths
    (re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{7,40}\b"), " "),                                            # hashes / addresses
    (re.compile(r"\w*\d[\w,.]*"), " "),          # numbers and any token holding a digit (uuids, run ids, 30s, py311)
    (re.compile(r"[^a-z_ ]+"), " "),
]


def normalise(text: str) -> str:
    t = str(text or "").lower()
    for rx, rep in _NORMALISERS:
        t = rx.sub(rep, t)
    return re.sub(r"\s+", " ", t).strip()


def error_class(text: str) -> str:
    """The exception class name if one is present, else the first words of the message, normalised."""
    s = str(text or "")
    m = re.findall(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Timeout|Warning))\b", s)
    if m:
        return m[-1].lower()
    low = s.lower()
    if "timed out" in low:          # "GOLDBEES.NS: timed out" and "SILVERBEES.NS: timed out" are one incident
        return "timeout"
    for key in ("timed_out", "timeout", "startup_failure", "rejected", "non-fast-forward"):
        if key in low:
            return key
    return " ".join(normalise(s).split()[:4])


def fingerprint(component: str, step: str, error: str = "") -> str:
    parts = [normalise(component), normalise(step), error_class(error) if error else ""]
    return "|".join(p.replace("|", " ") for p in parts)


# ---------------------------------------------------------------- state file
def _empty() -> dict:
    return {"version": 1, "fires": {"date": "", "n": 0, "ts": []}, "last_high_fire": 0.0, "alerts": {}, "pending": []}


def state_path(state_dir) -> Path:
    return Path(state_dir) / "alerts.json"


_ENTRY_DEFAULTS = {"component": "", "step": "", "category": "warning", "severity": MEDIUM, "first_seen": 0.0,
                   "last_seen": 0.0, "count": 0, "status": "open", "emails": [], "acked": False, "resolved_emailed": False,
                   "incident_id": "", "issue": None, "issue_url": "", "deferred": False, "group": "", "detail": ""}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _repair(d) -> dict:
    """A shape-checked copy of a loaded alerts.json: wrong-typed parts are reset, never trusted (and never crash)."""
    base = _empty()
    if not isinstance(d, dict):
        return base
    base.update({k: v for k, v in d.items() if k not in base})
    f = d.get("fires")
    if isinstance(f, dict) and isinstance(f.get("date", ""), str) and _num(f.get("n", 0)) and isinstance(f.get("ts", []), list):
        base["fires"] = {"date": f.get("date", ""), "n": int(f.get("n", 0)), "ts": [t for t in f.get("ts", []) if _num(t)]}
    if _num(d.get("last_high_fire")):
        base["last_high_fire"] = float(d["last_high_fire"])
    if isinstance(d.get("pending"), list):
        base["pending"] = [p for p in d["pending"] if isinstance(p, str)]
    for fp, e in (d.get("alerts") or {}).items() if isinstance(d.get("alerts"), dict) else []:
        if not isinstance(e, dict):
            continue
        e = {**_ENTRY_DEFAULTS, **e, "fp": str(fp)}
        if not isinstance(e["emails"], list):
            e["emails"] = []
        e["emails"] = [t for t in e["emails"] if _num(t)]
        for k in ("first_seen", "last_seen", "count"):
            if not _num(e[k]):
                e[k] = 0
        if e["severity"] not in (HIGH, MEDIUM, LOW):
            e["severity"] = MEDIUM
        base["alerts"][str(fp)] = e
    return base


def load_state(state_dir) -> dict:
    try:
        return _repair(json.loads(state_path(state_dir).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _empty()


def atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_state(state_dir, st: dict) -> None:
    atomic_write(state_path(state_dir), json.dumps(st, indent=1, sort_keys=True))


# ---------------------------------------------------------------- budget
def fires_today(st: dict, now: float) -> int:
    f = st["fires"]
    return f["n"] if f.get("date") == day_of(now) else 0


def budget_left(st: dict, now: float, cfg: dict) -> bool:
    return fires_today(st, now) < cfg["max_fires_per_day"]


def record_fire(st: dict, now: float, high: bool = False) -> None:
    f = st["fires"]
    if f.get("date") != day_of(now):
        f.update({"date": day_of(now), "n": 0, "ts": []})
    f["n"] += 1
    f["ts"].append(now)
    if high:
        st["last_high_fire"] = now


# ---------------------------------------------------------------- raising alerts
def _entry(st: dict, fp: str) -> dict | None:
    return st["alerts"].get(fp)


def raise_alert(state_dir, component: str, step: str, category: str, detail: str = "", error: str = "",
                now: float | None = None, cfg: dict | None = None, wake: bool = True, commit: bool = True,
                st: dict | None = None) -> dict:
    """Record an alert and decide whether the routine must be woken. Returns
    {fingerprint, severity, new, fire, reason}. A repeat of the same fingerprint is the same incident and never wakes the
    routine again (the reminder ladder does that). Never raises."""
    import time as _t
    now = _t.time() if now is None else now
    cfg = cfg or params()
    sev = severity_of(category)
    fp = fingerprint(component, step, error)
    out = {"fingerprint": fp, "severity": sev, "new": False, "fire": False, "reason": ""}
    try:
        own = st is None
        st = st if st is not None else load_state(state_dir)
        e = _entry(st, fp)
        reopened = bool(e and e.get("status") == "resolved")
        if e is None or reopened:
            e = {"fp": fp, "component": component, "step": step, "category": category, "severity": sev,
                 "first_seen": now, "count": 0, "status": "open", "emails": [], "acked": False, "resolved_emailed": False,
                 "incident_id": "", "issue": None, "issue_url": "", "deferred": False, "group": ""}
            if reopened:
                e["emails"] = []
            st["alerts"][fp] = e
            out["new"] = True
        e.update({"last_seen": now, "detail": str(detail)[:1500], "severity": sev, "category": category})
        e["count"] += 1
        if not out["new"] and e.get("deferred") and e["status"] == "open" and sev != LOW and wake \
                and budget_left(st, now, cfg):
            # it was held back only because the day's budget was used up; the budget is back (e.g. a new IST day)
            record_fire(st, now, high=sev == HIGH)
            e["deferred"] = False
            out.update(fire=True, reason="deferred incident still open; the fire budget is available again")
        elif not out["new"]:
            out["reason"] = "same fingerprint, already an open incident"
        elif sev == LOW and not cfg.get("low_wakes_routine", False):
            out["reason"] = "LOW: recorded only; the next routine run will pick it up"
            _pend(st, fp)
        elif not wake:
            out["reason"] = "wake not requested"
        elif sev != HIGH and now - st.get("last_high_fire", 0) < cfg["group_window_min"] * 60:
            e["group"] = "held"
            out["reason"] = f"grouped into the HIGH incident email sent in the last {cfg['group_window_min']} min"
            _pend(st, fp)
        elif not budget_left(st, now, cfg):
            e["deferred"] = True
            out["reason"] = f"routine budget of {cfg['max_fires_per_day']} fires/day used up; will be in the next email"
            _pend(st, fp)
        else:
            record_fire(st, now, high=sev == HIGH)
            out.update(fire=True, reason="new incident")
        if own and commit:
            save_state(state_dir, st)
    except Exception:  # noqa: BLE001
        log.exception("raise_alert failed")
        out["reason"] = "internal error (alert recorded best-effort)"
    return out


def raise_batch(state_dir, items: list[dict], now: float | None = None, cfg: dict | None = None) -> dict:
    """Several alerts from one source (e.g. a watchdog run) -> at most ONE routine fire."""
    import time as _t
    now = _t.time() if now is None else now
    cfg = cfg or params()
    st = load_state(state_dir)
    decisions = [raise_alert(state_dir, wake=False, st=st, now=now, cfg=cfg, **it) for it in items]
    new = [d for d, it in zip(decisions, items) if not (d["severity"] == LOW) and (
        d["new"] or (st["alerts"][d["fingerprint"]].get("deferred") and st["alerts"][d["fingerprint"]]["status"] == "open"))]
    fire = False
    reason = "nothing new"
    if new:
        top = HIGH if any(d["severity"] == HIGH for d in new) else MEDIUM
        if top != HIGH and now - st.get("last_high_fire", 0) < cfg["group_window_min"] * 60:
            reason = "grouped into the recent HIGH incident"
            for d in new:
                st["alerts"][d["fingerprint"]]["group"] = "held"; _pend(st, d["fingerprint"])
        elif not budget_left(st, now, cfg):
            reason = "routine budget used up; queued for the next email"
            for d in new:
                st["alerts"][d["fingerprint"]]["deferred"] = True; _pend(st, d["fingerprint"])
        else:
            record_fire(st, now, high=top == HIGH)
            for d in new:
                st["alerts"][d["fingerprint"]]["deferred"] = False
            fire, reason = True, "new incident(s)"
    else:
        for d in decisions:
            if d["new"]:
                _pend(st, d["fingerprint"])
    save_state(state_dir, st)
    return {"fire": fire, "reason": reason, "decisions": decisions}


def _pend(st: dict, fp: str) -> None:
    if fp not in st["pending"]:
        st["pending"].append(fp)


def attach_incident(state_dir, fp: str, incident_id: str, **fields) -> None:
    st = load_state(state_dir)
    if fp in st["alerts"]:
        st["alerts"][fp]["incident_id"] = incident_id
        st["alerts"][fp].update(fields)
        save_state(state_dir, st)


def find_by_incident(st: dict, incident_id: str) -> dict | None:
    for e in st["alerts"].values():
        if e.get("incident_id") == incident_id:
            return e
    return None


# ---------------------------------------------------------------- emails, ladder, resolved
def record_email(state_dir, fp: str, now: float | None = None) -> None:
    import time as _t
    now = _t.time() if now is None else now
    st = load_state(state_dir)
    e = st["alerts"].get(fp)
    if e is not None:
        e["emails"].append(now)
        e["group"] = ""
        e["deferred"] = False
        st["pending"] = [p for p in st["pending"] if p != fp]
        save_state(state_dir, st)


def take_pending(state_dir, exclude_fp: str = "") -> list[dict]:
    """Alerts held back (grouped, LOW, or over budget) that the next email should mention."""
    st = load_state(state_dir)
    return [st["alerts"][p] for p in st["pending"] if p in st["alerts"] and p != exclude_fp
            and st["alerts"][p]["status"] == "open"]


def reminder_due_at(e: dict, cfg: dict) -> float | None:
    """When the next reminder for an emailed, open, unacked incident is due (None if never)."""
    if e.get("status") != "open" or not e.get("emails") or e.get("acked"):
        return None
    sent = e["emails"]
    if e["severity"] == HIGH:
        if len(sent) - 1 >= cfg["max_reminders_per_incident"]:
            return None
        return sent[-1] + cfg["high_reminder_min"] * 60
    if len(sent) - 1 >= cfg.get("max_medium_reminders", 10):
        return None
    hrs = cfg["medium_reminder_hours"]
    n = len(sent)
    if n <= len(hrs):
        return sent[0] + hrs[n - 1] * 3600
    return sent[-1] + cfg["medium_reminder_daily_hours"] * 3600


def reminders_due(st: dict, now: float, cfg: dict | None = None) -> list[dict]:
    """HIGH incidents whose last email is at least high_reminder_due_min old (workflow cadence is hourly at :07)."""
    cfg = cfg or params()
    out = []
    for e in st["alerts"].values():
        if e["severity"] != HIGH or e["status"] != "open" or e.get("acked") or not e.get("emails"):
            continue
        if len(e["emails"]) - 1 >= cfg["max_reminders_per_incident"]:
            continue
        if now - e["emails"][-1] >= cfg["high_reminder_due_min"] * 60:
            out.append(e)
    return out


def should_email(e: dict, now: float, cfg: dict | None = None, fix_failed: bool = False,
                 needs_approval: bool = False, status: str = "") -> tuple[str, str, str]:
    """-> (SEND|HOLD, mode, reason); mode in incident|reminder|still-broken|resolved."""
    cfg = cfg or params()
    sev, sent = e["severity"], e.get("emails") or []
    if e["status"] == "resolved":
        if sent and not e.get("resolved_emailed"):
            return "SEND", "resolved", "was emailed before and is now resolved (one Resolved email)"
        return "HOLD", "resolved", "resolved, and either never emailed or the Resolved email already went out"
    if sev == LOW:
        return "HOLD", "incident", "LOW alerts are never emailed; the fix goes into the daily report"
    if not sent:
        if sev == HIGH:
            return "SEND", "incident", "HIGH: email immediately"
        if needs_approval or fix_failed or status == "needs-approval":
            return "SEND", "incident", "MEDIUM needs your approval or the fix failed"
        return "HOLD", "incident", "MEDIUM: auto-fixing, emailed only if approval is needed or the fix fails"
    if e.get("acked"):
        return "HOLD", "reminder", "acknowledged by the owner; reminders stopped"
    due = reminder_due_at(e, cfg)
    if due is None:
        return "HOLD", "reminder", "reminder cap reached" if sev == HIGH else "no reminder pending"
    if now >= due - 300:
        return "SEND", "reminder" if sev == HIGH else "still-broken", f"unresolved and unacknowledged, reminder #{len(sent)} due"
    return "HOLD", "reminder", f"next reminder due in {int((due - now) / 60)} min"


def ack(state_dir, fp_or_incident: str, via: str, now: float | None = None) -> bool:
    import time as _t
    now = _t.time() if now is None else now
    st = load_state(state_dir)
    e = st["alerts"].get(fp_or_incident) or find_by_incident(st, fp_or_incident)
    if not e or e.get("acked"):
        return False
    e.update(acked=True, acked_at=now, acked_via=via)
    save_state(state_dir, st)
    return True


def resolve(state_dir, fp_or_incident: str, now: float | None = None) -> dict | None:
    import time as _t
    now = _t.time() if now is None else now
    st = load_state(state_dir)
    e = st["alerts"].get(fp_or_incident) or find_by_incident(st, fp_or_incident)
    if not e:
        return None
    e.update(status="resolved", resolved_at=now)
    st["pending"] = [p for p in st["pending"] if p != e["fp"]]
    save_state(state_dir, st)
    return e


def mark_resolved_emailed(state_dir, fp: str) -> None:
    st = load_state(state_dir)
    if fp in st["alerts"]:
        st["alerts"][fp]["resolved_emailed"] = True
        save_state(state_dir, st)


# ---------------------------------------------------------------- acknowledgement (GitHub, no AI)
_ACK_RE = re.compile(r"^\s*(ack|ok|seen)\b", re.I)
_TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}


def issue_acked(issue: dict, comments: list[dict]) -> str:
    """'' if not acked, else how. Closing the issue, the `ack` label, or an `ack` comment from someone with write access.
    (A public repo lets anyone comment, so only OWNER/MEMBER/COLLABORATOR comments count; labels and closing need write access anyway.)"""
    if issue.get("state") == "closed":
        return "closed"
    if any((lb.get("name") if isinstance(lb, dict) else lb) == "ack" for lb in issue.get("labels", [])):
        return "label"
    for c in comments:
        if c.get("author_association") in _TRUSTED and (c.get("user") or {}).get("type") != "Bot" \
                and _ACK_RE.match(c.get("body") or ""):
            return "comment"
    return ""


def real_gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=True).stdout


def check_acks(state_dir, repo: str, gh=real_gh, now: float | None = None) -> list[str]:
    """Record GitHub acknowledgements for unacked HIGH incidents. Returns the fingerprints acked now."""
    import time as _t
    now = _t.time() if now is None else now
    st = load_state(state_dir)
    done = []
    for e in st["alerts"].values():
        if e["severity"] != HIGH or e["status"] != "open" or e.get("acked") or not e.get("issue"):
            continue
        try:
            n = e["issue"]
            issue = json.loads(gh(["api", f"repos/{repo}/issues/{n}"]))
            comments = json.loads(gh(["api", f"repos/{repo}/issues/{n}/comments?per_page=100"]))
        except Exception as ex:  # noqa: BLE001
            log.warning("ack check failed for %s: %s", e["fp"], ex)
            continue
        how = issue_acked(issue, comments if isinstance(comments, list) else [])
        if how:
            e.update(acked=True, acked_at=now, acked_via="github " + how)
            done.append(e["fp"])
    if done:
        save_state(state_dir, st)
    return done


# ---------------------------------------------------------------- routine webhook
def post_routine(text: str, url: str, token: str, timeout: float = 10.0) -> bool:
    """Never raises: an empty or malformed URL (e.g. a secret pasted without https://) is just a failed post."""
    try:
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": BETA, "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - owner-supplied URL
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        log.warning("routine POST failed: %s", e)
        return False


def build_text(facts: dict, limit: int = 12_000) -> str:
    text = json.dumps(facts, separators=(",", ":"), default=str, ensure_ascii=False)
    if len(text) > limit:
        facts = {**facts, "detail": str(facts.get("detail", ""))[:2000]}
        text = json.dumps(facts, separators=(",", ":"), default=str, ensure_ascii=False)[:limit]
    return text


# ---------------------------------------------------------------- safe mode
def safe_path(state_dir) -> Path:
    return Path(state_dir) / "safe_mode.json"


def safe_mode_on(state_dir) -> bool:
    """Read from disk every tick. A missing file is off; an unreadable one fails closed (on)."""
    p = safe_path(state_dir)
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return bool(d.get("on")) if isinstance(d, dict) else True


def safe_mode_info(state_dir) -> dict:
    """{} when there is no file; an unreadable or wrong-shaped file reads as ON (fail closed, never auto-cleared)."""
    p = safe_path(state_dir)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = None
    return d if isinstance(d, dict) else {"on": True, "since": "", "reason": "state/safe_mode.json is unreadable"}


def set_safe_mode(state_dir, reason: str, now: float | None = None, notify=True, cfg: dict | None = None) -> bool:
    """Turn safe mode on (idempotent). Raises the single 'System unstable' HIGH alert. Returns True if newly set."""
    import time as _t
    now = _t.time() if now is None else now
    if safe_mode_on(state_dir):
        return False
    atomic_write(safe_path(state_dir), json.dumps(
        {"on": True, "since": datetime.fromtimestamp(now, IST).isoformat(timespec="seconds"), "reason": reason}, indent=1))
    if notify:
        raise_alert(state_dir, "system", "safe-mode", "system_unstable", reason, now=now, cfg=cfg)
    return True


def clear_safe_mode(state_dir, reason: str = "cleared", now: float | None = None) -> bool:
    if not safe_path(state_dir).exists():
        return False
    atomic_write(safe_path(state_dir), json.dumps({"on": False, "cleared": reason}, indent=1))
    return True


def evaluate_safe_mode(state_dir, now: float | None = None, cfg: dict | None = None) -> bool:
    """>5 distinct incidents today, or a HIGH incident still open after the 30 min fix window -> safe mode."""
    import time as _t
    now = _t.time() if now is None else now
    cfg = cfg or params()
    if safe_mode_on(state_dir):
        return False
    st = load_state(state_dir)
    today = day_of(now)
    todays = [e for e in st["alerts"].values() if day_of(e["first_seen"]) == today and e["fp"] != "system|safe mode|"
              and e.get("category") not in NO_SAFE_MODE]
    if len(todays) > cfg["safe_mode_incidents"]:
        return set_safe_mode(state_dir, f"{len(todays)} distinct incidents today (limit {cfg['safe_mode_incidents']})", now, cfg=cfg)
    for e in todays:
        if e["severity"] == HIGH and e["status"] == "open" and now - e["first_seen"] > cfg["safe_mode_fix_window_min"] * 60:
            return set_safe_mode(state_dir, f"HIGH incident not fixed within {cfg['safe_mode_fix_window_min']} min: {e['step']}", now, cfg=cfg)
    return False


def auto_clear_safe_mode(state_dir, now: float, healthy: bool) -> bool:
    """At the next day's pre-market (08:30-09:15 IST), if the healthcheck is clean."""
    info = safe_mode_info(state_dir)
    if not info.get("on") or not healthy:
        return False
    t = datetime.fromtimestamp(now, IST)
    since = str(info.get("since", ""))[:10]
    if since and since < t.date().isoformat() and (8, 30) <= (t.hour, t.minute) < (9, 15):
        return clear_safe_mode(state_dir, "automatic: next pre-market, healthcheck clean", now)
    return False


# ---------------------------------------------------------------- git merge driver for state/alerts.json
def _merge_entry(a: dict, b: dict) -> dict:
    newer, older = (a, b) if max(a.get("last_seen", 0), a.get("resolved_at", 0)) >= \
        max(b.get("last_seen", 0), b.get("resolved_at", 0)) else (b, a)
    out = {**older, **newer}
    out["emails"] = sorted(set(a.get("emails", [])) | set(b.get("emails", [])))
    out["count"] = max(a.get("count", 0), b.get("count", 0))
    firsts = [x for x in (a.get("first_seen"), b.get("first_seen")) if x]
    out["first_seen"] = min(firsts) if firsts else 0
    out["acked"] = bool(a.get("acked") or b.get("acked"))
    out["resolved_emailed"] = bool(a.get("resolved_emailed") or b.get("resolved_emailed"))
    for k in ("incident_id", "issue", "issue_url"):
        out[k] = newer.get(k) or older.get(k) or out.get(k)
    return out


def merge_states(base: dict, ours: dict, theirs: dict) -> dict:
    """Three-way merge of two alert states that diverged from base: union of incidents, emails, fires and pending."""
    ours, theirs = _repair(ours), _repair(theirs)
    out = {**theirs, **ours}
    out["alerts"] = dict(theirs["alerts"])
    for fp, e in ours["alerts"].items():
        out["alerts"][fp] = _merge_entry(e, theirs["alerts"][fp]) if fp in theirs["alerts"] else e
    fo, ft = ours["fires"], theirs["fires"]
    if fo["date"] == ft["date"]:
        ts = sorted(set(fo["ts"]) | set(ft["ts"]))
        out["fires"] = {"date": fo["date"], "n": max(len(ts), fo["n"], ft["n"]), "ts": ts}
    else:
        out["fires"] = fo if fo["date"] > ft["date"] else ft
    out["last_high_fire"] = max(ours["last_high_fire"], theirs["last_high_fire"])
    pend = [p for p in ours["pending"] + theirs["pending"] if p in out["alerts"]]
    out["pending"] = [p for i, p in enumerate(pend) if p not in pend[:i] and out["alerts"][p].get("status") == "open"]
    return out


def merge_file(base_path, ours_path, theirs_path) -> int:
    """git merge driver: writes the merge into ours_path (%A) and always exits 0 with valid JSON."""
    def read(pth):
        try:
            return json.loads(Path(pth).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    base, ours, theirs = read(base_path), read(ours_path), read(theirs_path)
    merged = merge_states(base or {}, ours if ours is not None else (theirs or {}), theirs if theirs is not None else (ours or {}))
    Path(ours_path).write_text(json.dumps(merged, indent=1, sort_keys=True), encoding="utf-8")
    return 0


# ---------------------------------------------------------------- CLI
def _env_args(a) -> list[dict]:
    return [{"component": a.component, "step": a.step, "category": a.category, "detail": a.detail or "", "error": a.error or ""}]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=str(ROOT / "state"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--component", required=True); g.add_argument("--step", required=True)
    g.add_argument("--category", required=True); g.add_argument("--error"); g.add_argument("--detail")
    g.add_argument("--log-file")
    pr = sub.add_parser("problems"); pr.add_argument("file")
    rm = sub.add_parser("reminders"); rm.add_argument("--repo", required=True)
    po = sub.add_parser("post"); po.add_argument("--text-file", required=True)
    rs = sub.add_parser("resolve-missing"); rs.add_argument("--component", required=True); rs.add_argument("--current", default="")
    mf = sub.add_parser("merge-file", help="git merge driver: merge-file %%O %%A %%B")
    mf.add_argument("base"); mf.add_argument("ours"); mf.add_argument("theirs")
    a = ap.parse_args(argv)
    if a.cmd == "merge-file":
        return merge_file(a.base, a.ours, a.theirs)
    sd = Path(a.state)
    if a.cmd == "gate":
        err = a.error or ""
        if a.log_file and Path(a.log_file).exists():
            err = err or Path(a.log_file).read_text(errors="replace")[-4000:]
        d = raise_alert(sd, a.component, a.step, a.category, a.detail or "", err)
        evaluate_safe_mode(sd)
        print(json.dumps(d))
    elif a.cmd == "problems":
        items = []
        for ln in Path(a.file).read_text().splitlines():
            try:
                p = json.loads(ln)
            except ValueError:
                continue
            items.append({"component": "watchdog", "step": p["check"], "category": p.get("category") or "warning",
                          "detail": p.get("detail", "")})
        d = raise_batch(sd, items) if items else {"fire": False, "reason": "no problems", "decisions": []}
        evaluate_safe_mode(sd)
        print(json.dumps(d))
    elif a.cmd == "reminders":
        acked = check_acks(sd, a.repo)
        st = load_state(sd)
        due = reminders_due(st, __import__("time").time())
        print(json.dumps({"acked": acked, "due": [e["incident_id"] or e["fp"] for e in due]}))
    elif a.cmd == "post":     # POST {"text": ...} to the incident routine; always exit 0
        url, token = os.getenv("ROUTINE_INCIDENT_URL", ""), os.getenv("ROUTINE_INCIDENT_TOKEN", "")
        text = Path(a.text_file).read_text(encoding="utf-8")[:12000]
        ok = bool(url and token) and post_routine(text, url, token, timeout=30)
        print(json.dumps({"configured": bool(url and token), "sent": ok}))
    elif a.cmd == "resolve-missing":
        cur = {c for c in a.current.split(",") if c}
        st = load_state(sd)
        res = []
        for e in list(st["alerts"].values()):
            if e["component"] == a.component and e["status"] == "open" and e["step"] not in cur:
                resolve(sd, e["fp"])
                if e.get("emails"):
                    res.append(e["incident_id"] or e["fp"])
        print(json.dumps({"resolved_emailed": res}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
