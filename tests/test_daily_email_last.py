"""routines/daily_report.md: the email (the 40+ KB HTML body) is read and sent in the LAST substantive step.

Every other step runs before it, so the body is read once and never sits in the context of later steps. The sent marker
(`reports/daily/D.sent.json`, which tools/report_retry.py counts as SENT) is written by a small step 11b right after the send."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAY = (ROOT / "routines" / "daily_report.md").read_text(encoding="utf-8")
BODY_READS = ("html_path", "text_path", "email.html", "reports/daily/D.txt")


def steps() -> list[tuple[str, str]]:
    start = PLAY.index("\n## Steps")
    end = PLAY.index("\n## Output contract")
    block = PLAY[start:end]
    parts = re.split(r"(?m)^(\d+[a-z]?(?:-v3)?)\. \*\*", block)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def order() -> list[str]:
    return [n for n, _ in steps()]


def body(n: str) -> str:
    return dict(steps())[n]


def test_email_is_step_11_and_the_last_step_that_needs_the_body():
    o = order()
    assert o.index("11") > o.index("10") > o.index("9d") > o.index("9b")
    assert o[-2:] == ["11", "11b"]
    for n in o:
        if n in ("11", "11b"):
            continue
        txt = body(n)
        if n == "6":      # the JSON this step prints carries the paths; nothing reads them yet
            assert "Read" not in txt
            continue
        assert not any(t in txt for t in BODY_READS) or n == "9", (n, "reads the report body before the email step")
    assert "send_message" in body("11") and "Read tool, all lines" in body("11")
    assert "send_message" not in "".join(body(n) for n in o if n not in ("11", "11b", "9"))


def test_step_9_is_only_a_pointer_and_sends_nothing():
    s = body("9")
    assert "step 11" in s and "Nothing is sent here" in s
    assert "Read tool" not in s and "send_message" not in s


def test_no_step_after_the_email_reads_the_html_again():
    o = order()
    for n in o[o.index("11") + 1:]:
        txt = body(n)
        assert not any(t in txt for t in BODY_READS), (n, txt)
        assert "Read tool" not in txt and "cat " not in txt
    assert "never re-read" in body("11") or "never re-read" in PLAY or "do not read them again after the send" in body("11")


def test_the_marker_is_written_only_after_a_confirmed_send_in_small_calls():
    assert "--mark-sent" not in "".join(body(n) for n in order() if n != "11b")
    m = body("11b")
    assert "only after `send_message` confirmed success" in m.lower() or "Only after `send_message` confirmed success" in m
    calls = re.findall(r"`((?:python|git) [^`]+)`", m)
    assert calls == ["python tools/send_report.py --date D --mark-sent", "git add -- reports/daily/D.sent.json",
                     'git commit -qm "routine(daily_report): D"', "git pull --rebase --autostash -q", "git push origin HEAD:main"]
    assert not any(c in x for c in ("&&", ";", "$(", "|") for x in calls)
    assert "report_retry" not in m


def test_everything_else_is_committed_before_the_send():
    s10 = body("10")
    assert "git push origin HEAD:main" in s10 and 'routine(daily_report): D' in s10
    assert "D.sent.json" not in s10
    assert PLAY.index("\n10. **") < PLAY.index("\n11. **")


def test_the_send_rules_are_unchanged():
    s = body("11")
    for frag in ("`to` = that `to` value", "starts \"Metals Daybook\"", "Verbatim, no exceptions", "One send only",
                 "do not retry with changes", "Never write\n    the address anywhere", "Never say \"sent\" without",
                 "REPORT_EMAIL not set", "Never run `echo`, `printenv`"):
        assert frag in s, frag


def test_retry_workflow_and_the_incident_playbook_still_fit():
    inc = (ROOT / "routines" / "incident.md").read_text(encoding="utf-8")
    assert "steps 2–11" in inc and "step 9b/9d" in inc and "step 10 must be exactly `routine(daily_report): D`" in inc.replace("in step 10", "step 10")
    retry = (ROOT / "tools" / "report_retry.py").read_text(encoding="utf-8")
    assert '.sent.json").is_file()' in retry
