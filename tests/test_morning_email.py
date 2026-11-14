"""tools/morning_email.py + .github/workflows/morning-email.yml (no network, fake SMTP, fake clock, fake renderer)."""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import morning_email as ME  # noqa: E402
from simgold.market import IST  # noqa: E402

AT = "\x40"
TO = f"owner{AT}example.com"
USER = f"sender{AT}example.com"
PW = "abcd efgh ijkl mnop-SECRET"
ENV = {"SMTP_USER": USER, "SMTP_APP_PASSWORD": PW, "REPORT_EMAIL": TO}
DAY = "2026-11-12"                      # Thursday, trading day
SUBJECT = "Metals Daybook · Thu 12 Nov · morning"


def at(day=DAY, hm="08:56"):
    return dt.datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=IST)


class FakeSMTP:
    instances = []
    fail = None

    def __init__(self, host, port, **kw):
        self.host, self.port, self.log = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): self.log.append(("login", u, p))

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.log.append(("send", msg, from_addr, to_addrs))


def make_runner(root, kind_supported=True, subject=SUBJECT):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if "--help" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "usage: --date --kind\n" if kind_supported else "usage: --date\n", "")
        (Path(root) / "m.html").write_text("<html><body><p>Good morning</p></body></html>", encoding="utf-8")
        (Path(root) / "m.txt").write_text("Good morning\n", encoding="utf-8")
        js = json.dumps({"subject": subject, "html_path": str(Path(root) / "m.html"), "text_path": str(Path(root) / "m.txt")})
        return subprocess.CompletedProcess(cmd, 0, "noise\n" + js + "\n", "")
    runner.calls = calls
    return runner


@pytest.fixture(autouse=True)
def _reset():
    FakeSMTP.instances, FakeSMTP.fail = [], None


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "state").mkdir()
    return tmp_path


def set_switch(root, on):
    (root / "config" / "params.yaml").write_text(yaml.safe_dump({"report": {"morning_email": on}}), encoding="utf-8")


def go(root, env=None, now=None, runner=None, **kw):
    said = []
    code = ME.run(root, kw.pop("date", None), dict(ENV if env is None else env), now=now or at(), smtp_factory=FakeSMTP,
                  runner=runner or make_runner(root), out=said.append, **kw)
    return code, "\n".join(said)


def test_real_params_switch_is_off_by_default():
    assert ME.switch_on(ROOT) is False


def test_switch_off_sends_nothing(root):
    set_switch(root, False)
    code, out = go(root)
    assert code == 0 and not FakeSMTP.instances and "switch is off" in out and not (root / ME.MARKER).exists()


@pytest.mark.parametrize("day", ["2026-11-14", "2026-11-15", "2026-11-10"])       # Sat, Sun, holiday
def test_weekend_or_holiday_sends_nothing(root, day):
    set_switch(root, True)
    code, out = go(root, now=at(day), force=True)
    assert code == 0 and not FakeSMTP.instances and "not an NSE trading day" in out


def test_after_11_ist_is_skipped_even_with_force(root):
    set_switch(root, True)
    for hm in ("11:00", "16:00"):
        code, out = go(root, now=at(hm=hm), force=True)
        assert code == 0 and not FakeSMTP.instances and "too late, skipped" in out
    code, out = go(root, now=at(hm="10:59"))
    assert code == 0 and len(FakeSMTP.instances) == 1


def test_past_date_is_too_late(root):
    set_switch(root, True)
    code, out = go(root, date="2026-11-11", now=at())
    assert code == 0 and not FakeSMTP.instances and "too late" in out


def test_already_sent_today_sends_nothing(root):
    set_switch(root, True)
    (root / ME.MARKER).write_text(json.dumps({"date": DAY, "sent_at": "x"}))
    code, out = go(root, force=True)
    assert code == 0 and not FakeSMTP.instances and "already sent" in out
    (root / ME.MARKER).write_text(json.dumps({"date": "2026-11-11", "sent_at": "x"}))      # yesterday's marker does not block
    assert go(root)[0] == 0 and len(FakeSMTP.instances) == 1


def test_renderer_missing_is_a_clean_skip(root):
    set_switch(root, True)
    code, out = go(root, runner=make_runner(root, kind_supported=False))
    assert code == 0 and not FakeSMTP.instances and "morning renderer not available yet (compact email not merged)" in out
    assert not (root / ME.MARKER).exists()


def test_happy_path_one_send_marker_and_no_secrets_in_output(root):
    set_switch(root, True)
    runner = make_runner(root)
    code, out = go(root, runner=runner)
    assert code == 0 and len(FakeSMTP.instances) == 1
    s = FakeSMTP.instances[0]
    assert (s.host, s.port) == ("smtp.gmail.com", 465)
    assert s.log[0] == ("login", USER, PW.replace(" ", ""))
    _, msg, frm, to = s.log[1]
    assert frm == USER and to == [TO] and msg["To"] == TO and msg["Cc"] is None and msg["Bcc"] is None
    assert msg["Subject"] == SUBJECT and "backup" not in msg["Subject"]
    assert msg.get_body(("plain",)).get_content().strip() == "Good morning"
    assert "Good morning" in msg.get_body(("html",)).get_content()
    assert json.loads((root / ME.MARKER).read_text())["date"] == DAY
    assert any("--kind" in c and "morning" in c and DAY in c for c in runner.calls)
    assert PW not in out and TO not in out and USER not in out
    assert go(root, runner=runner)[0] == 0 and len(FakeSMTP.instances) == 1          # second run: once per day


def test_force_bypasses_only_the_switch(root):
    set_switch(root, False)
    assert go(root, force=True)[0] == 0 and len(FakeSMTP.instances) == 1
    FakeSMTP.instances.clear()
    (root / ME.MARKER).unlink()
    assert "too late" in go(root, force=True, now=at(hm="12:00"))[1] and not FakeSMTP.instances


def test_dry_run_sends_nothing_and_writes_no_marker(root):
    set_switch(root, True)
    code, out = go(root, dry_run=True)
    assert code == 0 and not FakeSMTP.instances and "dry run" in out and "subject: Metals Daybook" in out
    assert not (root / ME.MARKER).exists()


def test_bad_subject_and_bad_recipient_are_refused(root):
    set_switch(root, True)
    assert go(root, runner=make_runner(root, subject="Hello"))[0] == 2
    assert go(root, env={**ENV, "REPORT_EMAIL": f"a{AT}x.com,b{AT}y.com"})[0] == 2
    assert go(root, env={**ENV, "REPORT_EMAIL": ""})[0] == 2
    assert not FakeSMTP.instances and not (root / ME.MARKER).exists()


def test_smtp_failure_exits_1_without_marker_or_exception_text(root):
    set_switch(root, True)
    FakeSMTP.fail = RuntimeError(f"boom {TO}")
    code, out = go(root)
    assert code == 1 and "SMTP failed: RuntimeError" in out and TO not in out and not (root / ME.MARKER).exists()


# ---- workflow structure ----
WF_TEXT = (ROOT / ".github" / "workflows" / "morning-email.yml").read_text(encoding="utf-8")
WF = yaml.safe_load(WF_TEXT)


def test_workflow_triggers_permissions_and_concurrency():
    on = WF.get("on", WF.get(True))
    assert [c["cron"] for c in on["schedule"]] == ["20 3 * * 1-5", "50 3 * * 1-5"]
    assert set(on["workflow_dispatch"]["inputs"]) == {"date", "force", "dry_run"}
    assert WF["permissions"] == {"contents": "write"}
    assert WF["concurrency"] == {"group": "morning-email", "cancel-in-progress": False}


def test_workflow_secrets_only_in_the_send_step_and_inputs_not_inlined():
    steps = WF["jobs"]["send"]["steps"]
    holders = [s.get("name", s.get("uses", s.get("run"))) for s in steps if "secrets." in yaml.safe_dump(s)]
    assert holders == ["Send the morning email"]
    assert "secrets." not in yaml.safe_dump(WF["jobs"]["send"].get("env", {}))
    assert all("github.event.inputs" not in s.get("run", "") for s in steps)          # no script injection
    assert any("morning_email.py" in s.get("run", "") for s in steps)
    assert any("state/morning_sent.json" in s.get("run", "") for s in steps)
