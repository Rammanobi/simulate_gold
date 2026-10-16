"""tools/send_report.py: `to` comes from REPORT_EMAIL (no echo/printenv in the shell); --mark-sent writes the sent marker."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import send_report as sr  # noqa: E402

ADDR = "owner@example.invalid"


def test_recipient_reads_env_and_strips(monkeypatch):
    monkeypatch.setenv("REPORT_EMAIL", f"  {ADDR}\n")
    assert sr.recipient() == ADDR
    monkeypatch.delenv("REPORT_EMAIL")
    assert sr.recipient() == ""


def test_make_includes_to(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "generate", lambda d, root, report_url=None: (
        {"closed": False, "warnings": []}, {k: tmp_path / k for k in ("email", "txt", "page")}))
    monkeypatch.setattr(sr.report, "subject_line", lambda day: "Metals Daybook · x")
    monkeypatch.setenv("REPORT_EMAIL", ADDR)
    assert sr.make("2026-10-16", tmp_path)["to"] == ADDR
    monkeypatch.delenv("REPORT_EMAIL")
    assert sr.make("2026-10-16", tmp_path)["to"] == ""


def test_recipient_only_flag(monkeypatch, capsys):
    monkeypatch.setenv("REPORT_EMAIL", ADDR)
    assert sr.main(["--recipient"]) == 0
    assert json.loads(capsys.readouterr().out) == {"to": ADDR}


def test_mark_sent_writes_marker_and_retry_sees_it(tmp_path, capsys):
    import report_retry as rr
    assert rr.sent(tmp_path, "2026-10-16") is False
    assert sr.main(["--date", "2026-10-16", "--mark-sent", "--root", str(tmp_path)]) == 0
    m = json.loads((tmp_path / "reports" / "daily" / "2026-10-16.sent.json").read_text())
    assert m["date"] == "2026-10-16" and m["sent_at"].startswith("20") and m["sent_at"].endswith("+05:30")
    assert "example" not in capsys.readouterr().out
    assert rr.sent(tmp_path, "2026-10-16") is True


def test_incident_render_includes_to(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("incident_tool_to", ROOT / "tools" / "incident.py")
    inc = importlib.util.module_from_spec(spec)
    sys.modules["incident_tool_to"] = inc
    spec.loader.exec_module(inc)
    from datetime import datetime
    rec = inc.new_record("t", "low", "s", "wf", "step", now=datetime(2026, 10, 13, 11, 0, tzinfo=inc.IST))
    monkeypatch.setenv("REPORT_EMAIL", ADDR)
    assert inc.render(rec, d=tmp_path, state_dir=tmp_path, inbox_url="")["to"] == ADDR
    monkeypatch.delenv("REPORT_EMAIL")
    assert inc.render(rec, d=tmp_path, state_dir=tmp_path, inbox_url="")["to"] == ""
