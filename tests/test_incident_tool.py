import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

from simgold import alerts as A

spec = importlib.util.spec_from_file_location("incident_tool", Path(__file__).parent.parent / "tools" / "incident.py")
I = importlib.util.module_from_spec(spec); sys.modules["incident_tool"] = I; spec.loader.exec_module(I)
NOW = datetime(2026, 10, 13, 11, 0, tzinfo=I.IST)


def rec(**kw):
    return I.new_record("Feed <b>down</b> & \"bad\"", "high", "Summary <script>x</script>", "wf", "step", now=NOW,
                        log=["ERR <img src=x onerror=1> & more"], **kw)


def test_subject_formats_start_with_metals_daybook():
    r = rec()
    assert I.subject_for(r) == "Metals Daybook · ⚠️ Incident · Feed <b>down</b> & \"bad\" · Monitoring"
    r["status"] = "needs-approval"
    assert I.subject_for(r).endswith("· Needs your approval")
    assert I.subject_for(r, "reminder", 3) == "Metals Daybook · 🔁 Reminder #3 · ⚠️ Incident · Feed <b>down</b> & \"bad\" · Needs your approval"
    assert I.subject_for(r, "still-broken").startswith("Metals Daybook · ⚠️ Still broken · ")
    assert I.subject_for(r, "resolved") == "Metals Daybook · ✅ Resolved · Feed <b>down</b> & \"bad\" · Resolved"
    assert len(I.short_title("x" * 100)) == 60


def test_status_colours_and_daybook_palette():
    assert I.STATUSES["resolved"][1] == "#1a7f4b" and I.STATUSES["needs-approval"][1] == "#8a6d12" and I.STATUSES["monitoring"][1] == "#6b7280"
    for st, (label, fg, bg) in I.STATUSES.items():
        r = rec(); r["status"] = st
        h = I.render_html(r)
        assert fg in h and label in h and "border-top:4px solid #c9a227" in h and "#f1f0ec" in h


def test_render_escapes_html():
    r = rec(); r["actions"] = [{"text": "<b>pwn</b>", "kind": "proposal", "link": "https://x/y?a=1&b=\"2", "risk": "<i>r</i>"}]
    h = I.render_html(r)
    for raw in ("<script>", "<img src=x", "<b>pwn</b>", "<i>r</i>"):
        assert raw not in h
    assert "&lt;img src=x" in h and "&amp; more" in h and "&quot;2" in h


def test_buttons_ack_merge_reject_and_hint():
    r = rec(); r["status"] = "needs-approval"; r["issue_url"] = "https://github.com/o/r/issues/7"
    r["actions"] = [{"text": "change rule", "kind": "proposal", "link": "https://github.com/o/r/pull/2", "risk": "stays broken"}]
    h = I.render_html(r)
    assert "I&#x27;ve seen it, stop reminders" in h and 'href="https://github.com/o/r/issues/7"' in h
    assert "Review &amp; merge the fix" in h and "Reject (close the PR)" in h and 'href="https://github.com/o/r/pull/2"' in h
    assert "reply ACK" in h and "never approve" in h
    t = I.render_text(r)
    assert "Stop reminders: https://github.com/o/r/issues/7" in t and "replying to this email can only acknowledge" in t


def test_reminder_mode_banner_and_no_buttons_for_resolved():
    r = rec(); r["issue_url"] = "https://github.com/o/r/issues/7"
    first = datetime(2026, 10, 13, 9, 41, tzinfo=I.IST).timestamp()
    h = I.render_html(r, "reminder", 2, first)
    assert "Reminder #2" in h and "First sent 09:41, no response yet." in h
    done = I.render_html(r, "resolved")
    assert "stop reminders" not in done and "Resolved" in done


def test_non_high_has_no_ack_button():
    r = rec(); r["severity"] = "medium"; r["issue_url"] = "https://x/1"
    assert "stop reminders" not in I.render_html(r)


def test_sections_and_handled_by():
    r = rec(tier="COMPLEX", handled="Sonnet triage → Opus fix"); r["status"] = "needs-approval"
    r["actions"] = [{"text": "restored", "kind": "safe-fix", "link": "https://c/1"}]
    h = I.render_html(r)
    for s in ("What went wrong", "Impact", "What I did", "What happens next", "Handled by: Sonnet triage → Opus fix"):
        assert s.lower() in h.lower()
    assert 'href="https://c/1"' in h and "max-width:600px" in h


def test_inr_indian_grouping():
    assert I.inr(100412) == "₹1,00,412" and I.inr(99500) == "₹99,500" and I.inr(12345678) == "₹1,23,45,678"


def test_cli_roundtrip_with_should_email(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(I, "DIR", tmp_path / "inc"); monkeypatch.setattr(I, "STATE", tmp_path / "state")
    I.main(["open", "--title", "Data feed broke!", "--severity", "high", "--summary", "s", "--workflow", "engine", "--step", "tick"])
    out = json.loads(capsys.readouterr().out); iid = out["id"]
    assert iid.endswith("-data-feed-broke")
    I.main(["should-email", "--id", iid])
    assert capsys.readouterr().out.startswith("SEND incident")          # HIGH: immediately
    I.main(["add-action", "--id", iid, "--text", "did it", "--kind", "safe-fix", "--link", "https://c"])
    I.main(["set", "--id", iid, "--tier", "TRIVIAL", "--handled", "Sonnet", "--emailed"]); capsys.readouterr()
    I.main(["should-email", "--id", iid])
    assert capsys.readouterr().out.startswith("HOLD")                    # reminder not due for an hour
    I.main(["ack", "--id", iid, "--via", "email"]); capsys.readouterr()
    assert A.load_state(tmp_path / "state")["alerts"][out["fingerprint"]]["acked"]
    I.main(["set-status", "--id", iid, "resolved"]); capsys.readouterr()
    I.main(["should-email", "--id", iid])
    assert capsys.readouterr().out.startswith("SEND resolved")
    I.main(["set", "--id", iid, "--emailed"]); capsys.readouterr()
    I.main(["should-email", "--id", iid])
    assert capsys.readouterr().out.startswith("HOLD")                    # exactly one Resolved email
    I.main(["render", "--id", iid, "--mode", "resolved"])
    o = json.loads(capsys.readouterr().out)
    assert o["subject"].startswith("Metals Daybook · ✅ Resolved") and Path(o["html_path"]).exists() and Path(o["text_path"]).exists()


def test_open_issue_uses_rest_title_and_label():
    calls = []
    r = rec()
    I.open_issue(r, runner=lambda args: calls.append(args) or json.dumps({"number": 9, "html_url": "https://github.com/o/r/issues/9"}))
    a = calls[0]
    assert a[:2] == ["api", f"repos/{I.REPO}/issues"] and "title=[HIGH] Feed <b>down</b> & \"bad\"" in a and "labels[]=incident" in a
    assert any(x.startswith("body=") and r["id"] in x for x in a) and r["issue"] == 9
