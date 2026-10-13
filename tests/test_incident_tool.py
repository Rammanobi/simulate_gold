import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location("incident_tool", Path(__file__).parent.parent / "tools" / "incident.py")
I = importlib.util.module_from_spec(spec); sys.modules["incident_tool"] = I; spec.loader.exec_module(I)
NOW = datetime(2026, 10, 13, 11, 0, tzinfo=I.IST)


def rec(**kw):
    r = I.new_record("Feed <b>down</b> & \"bad\"", "high", "Summary <script>x</script>", "wf", "step", now=NOW,
                     log=["ERR <img src=x onerror=1> & more"], **kw)
    return r


def test_subject_format_and_status_labels():
    r = rec()
    assert I.subject_for(r) == "Metals Daybook · ⚠️ Incident · Feed <b>down</b> & \"bad\" · Monitoring"
    r["status"] = "needs-approval"
    assert I.subject_for(r).endswith("· Needs your approval")
    r["status"] = "resolved"
    assert I.subject_for(r).endswith("· Resolved")
    r["title"] = "x" * 100
    assert len(I.short_title(r["title"])) == 60


def test_status_colours():
    assert I.STATUSES["resolved"][1] == "#1e7b4b"
    assert I.STATUSES["needs-approval"][1] == "#b26a00"
    assert I.STATUSES["monitoring"][1] == "#5b6470"
    for st, (label, fg, bg) in I.STATUSES.items():
        r = rec(); r["status"] = st
        h = I.render_html(r)
        assert fg in h and label in h


def test_render_escapes_html():
    r = rec(); r["actions"] = [{"text": "<b>pwn</b>", "kind": "proposal", "link": "https://x/y?a=1&b=\"2", "risk": "<i>r</i>"}]
    h = I.render_html(r)
    for raw in ("<script>", "<img src=x", "<b>pwn</b>", "<i>r</i>"):
        assert raw not in h
    assert "&lt;img src=x" in h and "&amp; more" in h and "&quot;2" in h
    assert "<script>" not in I.render_html(r)


def test_sections_and_handled_by():
    r = rec(tier="COMPLEX", handled="Sonnet triage → Opus fix"); r["status"] = "needs-approval"
    r["actions"] = [{"text": "restored", "kind": "safe-fix", "link": "https://c/1"},
                    {"text": "change rule", "kind": "proposal", "link": "https://pr/2"}]
    h = I.render_html(r)
    for s in ("What went wrong", "Impact", "What I did", "What needs your approval", "What happens next", "Handled by: Sonnet triage → Opus fix"):
        assert s.lower() in h.lower()
    assert 'href="https://pr/2"' in h and 'href="https://c/1"' in h and 'width="600"' in h


def test_no_proposals_no_approval_section():
    assert "needs your approval" not in I.render_html(rec()).lower().replace("status", "")


def test_cli_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(I, "DIR", tmp_path)
    assert I.main(["open", "--title", "Data feed broke!", "--severity", "low", "--summary", "s"]) == 0
    iid = json.loads(capsys.readouterr().out)["id"]
    assert iid.endswith("-data-feed-broke")
    I.main(["add-action", "--id", iid, "--text", "did it", "--kind", "safe-fix", "--link", "https://c"])
    I.main(["set-status", "--id", iid, "resolved"])
    I.main(["set", "--id", iid, "--tier", "TRIVIAL", "--handled", "Sonnet"])
    capsys.readouterr()
    I.main(["render", "--id", iid])
    out = json.loads(capsys.readouterr().out)
    assert out["subject"].endswith("· Resolved") and Path(out["html_path"]).exists() and Path(out["text_path"]).exists()
    assert json.loads((tmp_path / f"{iid}.json").read_text())["tier"] == "TRIVIAL"
