import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

from simgold import report
from simgold.inboxlink import INBOX_LABEL, inbox_link, load_inbox_url, safe_id

REPO = Path(__file__).parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m


B = _load("inbox_tool", "tools/inbox.py")
I = _load("incident_tool_ib", "tools/incident.py")
NOW = "2026-10-13T16:00:00+05:30"
URL = "https://claude.ai/artifact/23b6gaJRa2bT4wNyQCKMvh"
ID_OK = re.compile(r"^[A-Za-z0-9._~-]+$")


def incident_record():
    r = I.new_record("Feed down", "high", "Prices missing", "wf", "step", now=datetime(2026, 10, 13, 10, 0, tzinfo=I.IST),
                     log=["ERR a"], paused="yes", portfolio="₹1,00,412", floor="₹99,500", missed="14", tier="COMPLEX", handled="Sonnet -> Opus")
    r["status"] = "needs-approval"; r["issue_url"] = "https://github.com/o/r/issues/7"
    r["actions"] = [{"text": "re-ran", "kind": "safe-fix", "link": "https://c/1"}, {"text": "noted", "kind": "info"},
                    {"text": "Add fallback", "kind": "proposal", "link": "https://github.com/Rammanobi/simulate_gold/pull/2", "risk": "stays broken"}]
    return r


def test_safe_id_and_link():
    assert safe_id("2026-10-13_feed.down~x") == "2026-10-13_feed.down~x"
    assert ID_OK.match(safe_id("a b/c#d?e&f é\n"))
    assert safe_id("///") == "item"
    assert inbox_link(URL, "daily-2026-10-13") == URL + "#daily-2026-10-13"
    assert inbox_link(URL + "#old", "a/b") == URL + "#a-b"
    assert inbox_link(None, "x") is None


def test_load_inbox_url(tmp_path):
    assert load_inbox_url(tmp_path) is None
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "inbox_url.txt").write_text(URL + "\n")
    assert load_inbox_url(tmp_path) == URL
    (tmp_path / "config" / "inbox_url.txt").write_text("javascript:alert(1)")
    assert load_inbox_url(tmp_path) is None
    assert load_inbox_url(REPO) == URL  # the committed config


def test_item_from_incident(tmp_path):
    (tmp_path / "incidents").mkdir()
    rec = incident_record()
    (tmp_path / "incidents" / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    out = tmp_path / "item.json"
    assert B.main(["item-from-incident", "--id", rec["id"], "--root", str(tmp_path), "--now", NOW, "--out", str(out)]) == 0
    it = json.loads(out.read_text(encoding="utf-8"))
    assert it["id"] == rec["id"] and ID_OK.match(it["id"])
    assert (it["kind"], it["severity"], it["status"], it["revision"], it["sample"]) == ("incident", "high", "needs_approval", 1, False)
    assert it["proposals"] == [{"title": "Add fallback", "pr_url": "https://github.com/Rammanobi/simulate_gold/pull/2", "risk": "stays broken"}]
    assert it["actions"] == [{"text": "re-ran", "url": "https://c/1"}, {"text": "noted"}]
    assert ["Missed ticks", "14"] in it["impact"] and ["Portfolio vs floor", "₹1,00,412 vs ₹99,500"] in it["impact"]
    assert it["updated"].endswith("+05:30") and it["history"][0]["at"] == "2026-10-13T16:00:00+05:30"
    assert it["handled_by"] == "Sonnet -> Opus" and it["report_url"].endswith("/issues/7")
    assert B.item_from_incident({**rec, "status": "resolved"}, B._now(NOW))["status"] == "resolved"
    assert B.item_from_incident(rec, B._now(NOW), "working")["status"] == "working"


def test_item_from_incident_missing_file(tmp_path):
    with pytest.raises(SystemExit):
        B.main(["item-from-incident", "--id", "nope", "--root", str(tmp_path)])


def test_item_from_daily(tmp_path, capsys):
    (tmp_path / "reports" / "daily").mkdir(parents=True)
    (tmp_path / "reports" / "daily" / "2026-10-12.json").write_text((REPO / "templates" / "sample_day.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert B.main(["item-from-daily", "--date", "2026-10-12", "--report-url", "https://r/x", "--root", str(tmp_path), "--now", NOW]) == 0
    it = json.loads(capsys.readouterr().out)
    assert (it["id"], it["kind"], it["status"], it["revision"]) == ("daily-2026-10-12", "daily", "needs_approval", 1)
    assert [r[0] for r in it["impact"]] == ["Portfolio", "Day P&L", "Floor", "Cash in LIQUIDBEES", "Trades"]
    assert dict(it["impact"])["Portfolio"] == "₹1,00,147" and dict(it["impact"])["Day P&L"] == "+₹147"
    assert dict(it["impact"])["Trades"].startswith("4 (2 buys, 2 sells)")
    assert it["summary"].startswith("Small green day") and it["report_url"] == "https://r/x" and it["sample"] is True
    assert it["title"] == "Daily report: Mon 12 Oct"


def test_bump(tmp_path):
    f = tmp_path / "item.json"
    f.write_text(json.dumps(B.item_from_daily(json.loads((REPO / "templates" / "sample_day.json").read_text(encoding="utf-8")), B._now(NOW))))
    assert B.main(["bump", "--file", str(f), "--status", "resolved", "--history", "Owner confirmed", "--now", "2026-10-13T17:00:00"]) == 0
    it = json.loads(f.read_text(encoding="utf-8"))
    assert it["revision"] == 2 and it["status"] == "resolved" and it["updated"] == "2026-10-13T17:00:00+05:30"
    assert it["history"][-1] == {"at": "2026-10-13T17:00:00+05:30", "text": "Owner confirmed"} and len(it["history"]) == 2
    with pytest.raises(SystemExit):
        B.main(["bump", "--file", str(f), "--status", "bogus", "--history", "x"])


def test_daily_email_has_inbox_button():
    day = json.loads((REPO / "templates" / "sample_day.json").read_text(encoding="utf-8"))
    h = report.render_email(report.prepare(day), inbox_url=URL)
    assert INBOX_LABEL in h and f'href="{URL}#daily-2026-10-12"' in h
    assert INBOX_LABEL + ": " + URL + "#daily-2026-10-12" in report.render_text(report.prepare(day), inbox_url=URL)
    assert INBOX_LABEL not in report.render_email(report.prepare(day))  # no URL configured, no button


def test_incident_email_inbox_primary_github_secondary():
    r = incident_record()
    h = I.render_html(r, inbox_url=URL)
    assert INBOX_LABEL in h and f'href="{URL}#{r["id"]}"' in h
    assert h.index(INBOX_LABEL) < h.index("Review &amp; merge the fix") < h.index("I&#x27;ve seen it, stop reminders")
    assert "Reject (close the PR)" in h and "reply ACK" in h and "never approve" in h
    t = I.render_text(r, inbox_url=URL)
    assert f"{INBOX_LABEL}: {URL}#{r['id']}" in t and "Review & merge the fix" in t
    assert INBOX_LABEL not in I.render_html(r, "resolved", inbox_url=URL)
    assert INBOX_LABEL not in I.render_html(r)
    # a hostile id cannot break out of the fragment
    r["id"] = 'x"><script>/ y'
    assert 'x"><script>' not in I.render_html(r, inbox_url=URL) and "#x-script-y" in I.render_html(r, inbox_url=URL)


def test_bump_refreshes_content_from_incident(tmp_path):
    rec = incident_record()
    (tmp_path / "incidents").mkdir()
    (tmp_path / "incidents" / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    f = tmp_path / "item.json"
    f.write_text(json.dumps(B.item_from_incident(rec, B._now(NOW))), encoding="utf-8")
    rec["actions"].append({"text": "Revised fallback", "kind": "proposal", "link": "https://github.com/Rammanobi/simulate_gold/pull/9", "risk": "r2"})
    (tmp_path / "incidents" / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    B.main(["bump", "--file", str(f), "--status", "needs_approval", "--history", "Revised after your feedback: x",
            "--from-incident", rec["id"], "--root", str(tmp_path), "--now", "2026-10-13T18:00:00"])
    it = json.loads(f.read_text(encoding="utf-8"))
    assert it["revision"] == 2 and len(it["proposals"]) == 2 and it["created"] == rec["opened_at"] and len(it["history"]) == 2


def test_bump_refreshes_daily_and_revise_proposal(tmp_path, capsys):
    (tmp_path / "reports" / "daily").mkdir(parents=True)
    day = json.loads((REPO / "templates" / "sample_day.json").read_text(encoding="utf-8"))
    (tmp_path / "reports" / "daily" / "2026-10-12.json").write_text(json.dumps(day), encoding="utf-8")
    f = tmp_path / "item.json"
    old = B.item_from_daily(day, B._now(NOW)); old["summary"] = "stale"
    f.write_text(json.dumps(old), encoding="utf-8")
    B.main(["bump", "--file", str(f), "--status", "needs_approval", "--history", "Re-checked", "--from-daily", "2026-10-12",
            "--root", str(tmp_path), "--now", NOW])
    it = json.loads(f.read_text(encoding="utf-8"))
    assert it["revision"] == 2 and it["summary"].startswith("Small green day")
    rec = incident_record()
    (tmp_path / "incidents").mkdir()
    (tmp_path / "incidents" / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    # revise-proposal lives in the incident tool; load it against tmp incidents
    I.DIR = tmp_path / "incidents"; I.STATE = tmp_path / "state"
    pr = "https://github.com/Rammanobi/simulate_gold/pull/2"
    I.main(["revise-proposal", "--id", rec["id"], "--link", pr, "--new-link", "https://github.com/Rammanobi/simulate_gold/pull/3", "--risk", "new risk"])
    got = I.load(rec["id"])["actions"][-1]
    assert got["link"].endswith("/pull/3") and got["risk"] == "new risk" and got["text"] == "Add fallback"
    with pytest.raises(SystemExit):
        I.main(["revise-proposal", "--id", rec["id"], "--link", pr])
