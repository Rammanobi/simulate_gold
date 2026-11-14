"""tools/alert_email.py: Claude-free missed-session email."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from simgold import alerts

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("alert_email", ROOT / "tools" / "alert_email.py")
AE = importlib.util.module_from_spec(spec)
sys.modules["alert_email"] = AE
spec.loader.exec_module(AE)

ENV = {"SMTP_USER": "sender@gmail.com", "SMTP_APP_PASSWORD": "abcd efgh ijkl mnop", "REPORT_EMAIL": "owner@example.com"}
T0 = 1_794_000_000.0
PROBLEM = json.dumps({"check": "session_missed", "severity": "high", "category": "session_missed",
                      "detail": "ticks-2026-11-12.csv does not exist: the engine did not run the 2026-11-12 session"})


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): self.p = p
    def send_message(self, msg, from_addr, to_addrs): FakeSMTP.sent.append((msg, from_addr, to_addrs))


def go(tmp_path, text=PROBLEM, env=ENV, now=T0, factory=FakeSMTP, dry=False):
    out: list[str] = []
    return AE.run(text, tmp_path, env, now, dry, factory, out.append), out


def test_sends_one_plain_email_to_the_owner_only(tmp_path):
    FakeSMTP.sent = []
    res, _ = go(tmp_path)
    assert res == "sent" and len(FakeSMTP.sent) == 1
    msg, frm, to = FakeSMTP.sent[0]
    assert to == ["owner@example.com"] and frm == "sender@gmail.com"
    assert msg["Subject"] == "Metals Daybook · ALERT · no trading session today" and msg["Subject"].startswith("Metals Daybook")
    assert not msg.is_multipart() and all(ord(c) < 0x1F300 for c in msg.get_content())          # plain text, no emoji
    assert "2026-11-12" in msg.get_content() and "abcd" not in msg.as_string()


def test_once_per_day_then_again_next_day(tmp_path):
    FakeSMTP.sent = []
    assert go(tmp_path)[0] == "sent"
    assert go(tmp_path, now=T0 + 3600)[0] == "duplicate"
    assert go(tmp_path, now=T0 + 86400 + 3600)[0] == "sent"
    assert len(FakeSMTP.sent) == 2


def test_no_problem_no_mail_and_other_problems_do_not_mail(tmp_path):
    other = json.dumps({"check": "relay_missing", "category": "relay_missing", "detail": "x"})
    assert go(tmp_path, text="")[0] == "none" and go(tmp_path, text=other)[0] == "none"


def test_a_failing_send_never_raises(tmp_path):
    class Boom(FakeSMTP):
        def login(self, u, p): raise OSError("smtp down")
    res, out = go(tmp_path, factory=Boom)
    assert res == "failed" and "OSError" in out[-1] and "owner@example.com" not in "".join(out)
    assert AE.main(["--problems", str(tmp_path / "missing.txt"), "--state", str(tmp_path)]) == 0


def test_missing_or_bad_configuration_sends_nothing(tmp_path):
    assert go(tmp_path, env={})[0] == "not-configured"
    assert go(tmp_path, env={**ENV, "REPORT_EMAIL": "a@b.com, c@d.com"})[0] == "not-configured"


def test_dry_run_sends_nothing(tmp_path):
    FakeSMTP.sent = []
    assert go(tmp_path, dry=True)[0] == "dry-run" and FakeSMTP.sent == []
