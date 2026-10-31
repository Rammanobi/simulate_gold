"""tools/backup_email.py: the SMTP backup of the daily report after the 8 retries failed (all SMTP calls mocked)."""
import json
import smtplib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import backup_email as BE  # noqa: E402

AT = "\x40"
TO = f"owner{AT}example.com"
USER = f"sender{AT}example.com"
PW = "abcd efgh ijkl mnop-SECRET"
ENV = {"SMTP_USER": USER, "SMTP_APP_PASSWORD": PW, "REPORT_EMAIL": TO}
DAY = "2026-10-13"


def make_root(tmp_path):
    for d in ("state", "logs", "reports/daily"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps({"cash": 0, "positions": {}, "floor": 99500, "liquidbees": 100200}))
    (tmp_path / "state" / "equity.csv").write_text("2026-10-12,100100\n2026-10-13,100200\n")
    (tmp_path / "logs" / f"ticks-{DAY}.csv").write_text("time,value,floor,cash,liquidbees\n09:15,100100,99500,0,100100\n15:30,100200,99500,0,100200\n")
    return tmp_path


class FakeSSL:
    instances = []
    fail = None

    def __init__(self, host, port, **kw):
        self.host, self.port, self.log = host, port, []
        FakeSSL.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): self.log.append(("login", u, p))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if FakeSSL.fail:
            raise FakeSSL.fail
        self.log.append(("send", msg, from_addr, to_addrs))


@pytest.fixture(autouse=True)
def _reset():
    FakeSSL.instances, FakeSSL.fail = [], None


@pytest.mark.parametrize("bad", ["", "   ", None, f"a{AT}x.com,b{AT}y.com", f"a{AT}x.com;b{AT}y.com", f"a{AT}x.com b{AT}y.com",
                                 f"a{AT}x.com\nBcc: b{AT}y.com", "not-an-address", f"a{AT}{AT}x.com"])
def test_recipient_must_be_exactly_one_plain_address(bad):
    with pytest.raises(BE.Refused) as e:
        BE.check_recipient(bad)
    assert AT not in str(e.value)
    assert BE.check_recipient(TO) == TO


def test_sender_and_password_checks():
    assert BE.check_sender(USER, PW) == (USER, PW.replace(" ", ""))
    for u, p in [("", PW), (f"a{AT}x.com,b{AT}y.com", PW), (USER, ""), (USER, " \n ")]:
        with pytest.raises(BE.Refused) as e:
            BE.check_sender(u, p)
        assert PW not in str(e.value) and AT not in str(e.value)


def test_subject_keeps_prefix_and_adds_backup_suffix():
    s = BE.backup_subject("Metals Daybook · 📈 Tue 13 Oct · +₹100 (green)")
    assert s == "Metals Daybook · 📈 Tue 13 Oct · +₹100 (green) · backup (Claude unavailable)"
    assert "\n" not in BE.backup_subject("Metals Daybook · x\r\nBcc: evil")
    with pytest.raises(BE.Refused):
        BE.backup_subject("Hello · Metals Daybook")


def test_mask_hides_the_address():
    assert BE.mask(f"rammanobi{AT}gmail.com") == f"r***{AT}gmail.com"
    assert BE.mask("") == "(invalid)" and BE.mask(f"a{AT}x.com,b{AT}y.com") == "(invalid)"


def test_message_is_multipart_alternative_with_generated_parts_verbatim():
    html = "<html><body><p>Report &amp; table</p></body></html>\n"
    msg = BE.build_message("Metals Daybook · 📊 Tue 13 Oct · ₹0 (flat)", USER, TO, "TEXT body\n", html)
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["To"] == TO and msg["Cc"] is None and msg["Bcc"] is None
    assert msg["Subject"].endswith("· backup (Claude unavailable)")
    assert msg.get_body(("plain",)).get_content() == "TEXT body\n"
    assert msg.get_body(("html",)).get_content() == html
    with_summary = BE.build_message("Metals Daybook · x", USER, TO, "T\n", html, summary="Calm <day>")
    h = with_summary.get_body(("html",)).get_content()
    assert "Calm &lt;day&gt;" in h and h.index("Backup email") < h.index("<p>Report") and h.endswith(html[html.index("<p>"):])


def test_run_sends_once_to_exactly_one_recipient_and_never_prints_secrets(tmp_path):
    root, said = make_root(tmp_path), []
    assert BE.run(root, DAY, dict(ENV), smtp_factory=FakeSSL, out=said.append) == 0
    s = FakeSSL.instances[0]
    assert (s.host, s.port) == ("smtp.gmail.com", 465)
    assert s.log[0] == ("login", USER, PW.replace(" ", ""))
    _, msg, frm, to = s.log[1]
    assert frm == USER and to == [TO] and msg["To"] == TO
    assert msg["Subject"].startswith("Metals Daybook") and msg["Subject"].endswith("backup (Claude unavailable)")
    gen = (root / "reports" / "daily" / f"{DAY}.email.html").read_text(encoding="utf-8")
    assert msg.get_body(("html",)).get_content() == gen
    # an ASCII-only body gets a final newline from the MIME layer, so compare without trailing newlines
    assert msg.get_body(("plain",)).get_content().rstrip("\n") == (root / "reports" / "daily" / f"{DAY}.txt").read_text(encoding="utf-8").rstrip("\n")
    out = "\n".join(said)
    assert PW not in out and TO not in out and USER not in out and f"o***{AT}example.com" in out


def test_dry_run_builds_but_does_not_send(tmp_path):
    root, said = make_root(tmp_path), []
    assert BE.run(root, DAY, {"REPORT_EMAIL": TO}, dry_run=True, smtp_factory=FakeSSL, out=said.append) == 0
    assert not FakeSSL.instances
    out = "\n".join(said)
    assert "subject: Metals Daybook" in out and "(1 recipient)" in out and "bytes" in out and "dry run" in out and TO not in out


def test_refused_config_exits_2_without_sending(tmp_path):
    root = make_root(tmp_path)
    for env in ({**ENV, "REPORT_EMAIL": ""}, {**ENV, "REPORT_EMAIL": f"{TO},x{AT}evil.com"}, {**ENV, "SMTP_APP_PASSWORD": ""}):
        said = []
        assert BE.run(root, DAY, env, smtp_factory=FakeSSL, out=said.append) == 2
        assert "REFUSED" in "\n".join(said) and PW not in "\n".join(said)
    assert not FakeSSL.instances


def test_smtp_failure_exits_1_and_hides_the_error_text(tmp_path):
    root, said = make_root(tmp_path), []
    FakeSSL.fail = smtplib.SMTPRecipientsRefused({TO: (550, b"no such user " + TO.encode())})
    assert BE.run(root, DAY, dict(ENV), smtp_factory=FakeSSL, out=said.append) == 1
    out = "\n".join(said)
    assert "SMTP failed: SMTPRecipientsRefused" in out and TO not in out and PW not in out


def test_main_with_mocked_smtp_ssl_never_leaks_to_stdout_or_stderr(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(BE.smtplib, "SMTP_SSL", FakeSSL)
    assert BE.main(["--date", DAY, "--root", str(root)]) == 0
    assert FakeSSL.instances[0].log[1][3] == [TO]
    cap = capsys.readouterr()
    both = cap.out + cap.err
    assert PW not in both and TO not in both and USER not in both and "backup email sent" in both


def test_cli_dry_run_and_refusal(tmp_path):
    root = make_root(tmp_path)
    base = {k: v for k, v in __import__("os").environ.items() if k not in ("REPORT_EMAIL", "DEEPSEEK_API_KEY", "SMTP_USER", "SMTP_APP_PASSWORD")}
    cmd = [sys.executable, str(ROOT / "tools" / "backup_email.py"), "--date", DAY, "--root", str(root), "--dry-run"]
    r = subprocess.run(cmd, capture_output=True, text=True, env={**base, **ENV})
    assert r.returncode == 0 and "dry run" in r.stdout and TO not in r.stdout + r.stderr and PW not in r.stdout + r.stderr
    r = subprocess.run(cmd, capture_output=True, text=True, env={**base, "REPORT_EMAIL": f"{TO}; x{AT}evil.com"})
    assert r.returncode == 2 and "REFUSED" in r.stdout and AT not in r.stdout + r.stderr


class FakeClient:
    chosen = {"fast": "deepseek-test"}

    def __init__(self, reply): self.reply, self.calls = reply, 0

    def chat(self, tier, messages, json_mode=False):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_deepseek_summary_only_when_market_notes_missing_and_failures_still_send(tmp_path):
    root = make_root(tmp_path)
    (root / "state" / "market_today.json").write_text(json.dumps({"date": DAY, "market": []}))
    c = FakeClient("A calm day.")
    assert BE.deepseek_summary(root, DAY, {"DEEPSEEK_API_KEY": "k"}, c)[0] == "" and c.calls == 0
    (root / "state" / "market_today.json").write_text(json.dumps({"date": "2026-10-12", "market": []}))
    assert BE.deepseek_summary(root, DAY, {}, None) == ("", "DEEPSEEK_API_KEY not set; deterministic report only")
    assert BE.deepseek_summary(root, DAY, {"DEEPSEEK_API_KEY": "k"}, c)[0] == "A calm day." and c.calls == 1
    assert BE.deepseek_summary(root, DAY, {"DEEPSEEK_API_KEY": "k"}, FakeClient("Up 9999 rupees"))[0] == ""   # number check
    assert BE.deepseek_summary(root, DAY, {"DEEPSEEK_API_KEY": "k"}, FakeClient(OSError("down")))[0] == ""
    said = []
    assert BE.run(root, DAY, {**ENV, "DEEPSEEK_API_KEY": "k"}, smtp_factory=FakeSSL, client=FakeClient(RuntimeError("x")),
                  out=said.append) == 0
    assert FakeSSL.instances and "k" not in [x for x in said]
    # an earlier fallback report's paragraph is reused without calling DeepSeek
    (root / "reports" / "daily" / f"{DAY}.json").write_text(json.dumps({"fallback": True, "fallback_summary": "Quiet day."}))
    c2 = FakeClient("unused")
    assert BE.deepseek_summary(root, DAY, {"DEEPSEEK_API_KEY": "k"}, c2)[0] == "Quiet day." and c2.calls == 0


def test_no_address_or_secret_in_the_tool_or_doc():
    import re
    for p in (ROOT / "tools" / "backup_email.py", ROOT / "docs" / "BACKUP_EMAIL.md", ROOT / ".github" / "workflows" / "report-retry.yml"):
        text = p.read_text(encoding="utf-8").replace("actions\x40users.noreply.github.com", "")   # the commit bot
        assert not re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+\.[A-Za-z]{2,}", text), p.name


def test_pasted_secrets_with_spaces_or_line_breaks_are_cleaned():
    # GitHub's secret box can keep a pasted trailing line break; Gmail shows app passwords as "abcd efgh ijkl mnop"
    assert BE.check_recipient(TO + "\n") == TO
    assert BE.check_recipient("  " + TO + "\r\n") == TO
    assert BE.check_sender(USER + "\n", "abcd efgh ijkl mnop\n") == (USER, "abcdefghijklmnop")
    with pytest.raises(BE.Refused):
        BE.check_recipient(f"a{AT}x.com\nBcc: b{AT}y.com")      # a line break inside is still refused (header injection)
