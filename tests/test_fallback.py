"""DeepSeek fallback: model resolution, strict validation, no-key no-op, key never leaks, issue body, SMTP (all mocked)."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import fallback_brain as FB       # noqa: E402
import fallback_report as FR      # noqa: E402

from simgold import envfile, report    # noqa: E402
from simgold.fallback_llm import Client, FallbackError, load_cfg    # noqa: E402
from simgold.market import IST    # noqa: E402

KEY = "sk-test-SECRET-0123456789"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=IST)         # Tuesday
CFG = load_cfg(None)


class Fake:
    """Scripted HTTP: routes[(method, path)] -> list of (status, body) or exceptions, consumed in order."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("api.deepseek.com", 1)[1]
        self.calls.append((method, path, headers, json.loads(json.dumps(body))))
        r = self.routes[(method, path)]
        item = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(item, Exception):
            raise item
        return item


def models(*ids): return (200, {"data": [{"id": i} for i in ids]})
def reply(text, reasoning="THINKING SECRET"): return (200, {"choices": [{"message": {"content": text, "reasoning_content": reasoning}}]})


# ---------------------------------------------------------------- model resolution
def test_models_success_picks_first_available_per_tier():
    f = Fake({("GET", "/models"): [models("deepseek-reasoner", "deepseek-chat", "deepseek-flash")],
              ("POST", "/chat/completions"): [reply("ok")]})
    c = Client(KEY, CFG, f)
    assert c.candidates("thinking") == ["deepseek-reasoner"] and c.candidates("fast") == ["deepseek-flash", "deepseek-chat"]
    assert c.chat("thinking", [{"role": "user", "content": "x"}], json_mode=True) == "ok"
    assert c.chosen["thinking"] == "deepseek-reasoner"
    method, path, headers, body = f.calls[-1]
    assert headers["Authorization"] == f"Bearer {KEY}" and body["model"] == "deepseek-reasoner"
    assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 2000 and body["temperature"] == 0.2
    assert [x[1] for x in f.calls].count("/models") == 1                                  # once per run


def test_models_failure_falls_through_on_404():
    f = Fake({("GET", "/models"): [OSError("down")],
              ("POST", "/chat/completions"): [(404, {"error": {"message": "The model `deepseek-v4-pro` does not exist"}}), reply("fine")]})
    c = Client(KEY, CFG, f)
    assert c.chat("thinking", [{"role": "user", "content": "x"}]) == "fine"
    assert c.chosen["thinking"] == "deepseek-reasoner"                                    # moved to the next ID
    assert [x[3]["model"] for x in f.calls if x[0] == "POST"] == ["deepseek-v4-pro", "deepseek-reasoner"]


def test_no_model_available_raises():
    c = Client(KEY, CFG, Fake({("GET", "/models"): [models("some-other-model")]}))
    with pytest.raises(FallbackError, match="no thinking model"):
        c.chat("thinking", [])


def test_temperature_rejected_is_retried_without_it():
    f = Fake({("GET", "/models"): [models("deepseek-v4-pro")],
              ("POST", "/chat/completions"): [(400, {"error": {"message": "temperature is not supported for this model"}}), reply("ok")]})
    assert Client(KEY, CFG, f).chat("thinking", []) == "ok"
    assert "temperature" in f.calls[1][3] and "temperature" not in f.calls[2][3]


def test_retries_on_5xx_and_timeout_then_gives_up():
    f = Fake({("GET", "/models"): [models("deepseek-flash")],
              ("POST", "/chat/completions"): [(503, {}), TimeoutError("t"), reply("third time")]})
    assert Client(KEY, CFG, f).chat("fast", []) == "third time"
    f = Fake({("GET", "/models"): [models("deepseek-flash")], ("POST", "/chat/completions"): [(500, {})]})
    with pytest.raises(FallbackError):
        Client(KEY, CFG, f).chat("fast", [])
    assert len([c for c in f.calls if c[0] == "POST"]) == 3                               # 1 try + 2 retries


def test_reasoning_content_is_never_returned_or_stored():
    f = Fake({("GET", "/models"): [models("deepseek-v4-pro")], ("POST", "/chat/completions"): [reply("answer", "SECRET THOUGHTS")]})
    c = Client(KEY, CFG, f)
    out = c.chat("thinking", [])
    assert out == "answer" and "SECRET THOUGHTS" not in json.dumps(vars(c), default=str)


def test_error_messages_never_contain_the_key():
    f = Fake({("GET", "/models"): [models("deepseek-flash")],
              ("POST", "/chat/completions"): [(401, {"error": {"message": f"bad key {KEY}"}})]})
    with pytest.raises(FallbackError) as e:
        Client(KEY, CFG, f).chat("fast", [])
    assert KEY not in str(e.value)


def test_params_yaml_fallback_block():
    cfg = load_cfg(__import__("simgold.config", fromlist=["x"]).load_params())
    assert cfg["base_url"] == "https://api.deepseek.com"
    assert cfg["models"]["thinking"] == ["deepseek-v4-pro", "deepseek-reasoner"]
    assert cfg["models"]["fast"] == ["deepseek-flash", "deepseek-v4-flash", "deepseek-chat"]
    assert cfg["max_tokens"] == {"thinking": 2000, "fast": 600}


# ---------------------------------------------------------------- envfile
def test_envfile_does_not_override(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nDEEPSEEK_API_KEY="from-file"\nOTHER=x\nEMPTY=\n')
    env = {"OTHER": "already"}
    assert envfile.load_env(f, env) == ["DEEPSEEK_API_KEY"]
    assert env == {"OTHER": "already", "DEEPSEEK_API_KEY": "from-file"}
    assert envfile.load_env(tmp_path / "nope", env) == []


def test_repo_ignores_env_and_ships_an_empty_example():
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()
    ex = (ROOT / ".env.example").read_text()
    assert "DEEPSEEK_API_KEY=\n" in ex and "GitHub" in ex and "Secrets" in ex
    assert not any(ln.split("=", 1)[1].strip() for ln in ex.splitlines() if ln.startswith("DEEPSEEK_API_KEY="))


# ---------------------------------------------------------------- validation (brain)
def v(obj, near=False):
    return FB.validate(obj if isinstance(obj, str) else json.dumps(obj), near)


def test_valid_reply_is_accepted():
    b, note, issues = v({"bias": {"GOLD": "favour", "SILVER": "Neutral", "COPPER": "block"}, "note": "Dollar weak."})
    assert b == {"GOLD": "favour", "SILVER": "neutral", "COPPER": "block"} and note == "Dollar weak." and issues == []


def test_favour_downgraded_when_events_near():
    b, _, issues = v({"bias": {"GOLD": "favour", "SILVER": "favour", "COPPER": "block"}, "note": ""}, near=True)
    assert b == {"GOLD": "neutral", "SILVER": "neutral", "COPPER": "block"} and len(issues) == 2


@pytest.mark.parametrize("bad", ["not json", "[]", "null", '{"bias": "favour"}', '{"bias": []}', "", '{"x": 1}'])
def test_garbage_is_all_neutral(bad):
    b, note, issues = v(bad)
    assert set(b.values()) == {"neutral"} and issues


def test_invalid_values_become_neutral_individually():
    b, _, _ = v({"bias": {"GOLD": "BUY", "SILVER": 1, "COPPER": "favour"}})
    assert b == {"GOLD": "neutral", "SILVER": "neutral", "COPPER": "favour"}


def test_note_is_sanitised():
    _, note, _ = v({"bias": {}, "note": "line1\nline2 \x00 ignore rules" + "x" * 500})
    assert "\n" not in note and "\x00" not in note and len(note) <= 200


def test_events_near_detection(tmp_path):
    (tmp_path / "state").mkdir()
    p = tmp_path / "state" / "events.json"
    assert FB.events_near(tmp_path, NOW.date()) is False
    p.write_text(json.dumps([{"date": "2026-10-14", "type": "CPI"}])); assert FB.events_near(tmp_path, NOW.date()) is True
    p.write_text(json.dumps([{"date": "2026-10-20", "type": "CPI"}])); assert FB.events_near(tmp_path, NOW.date()) is False
    p.write_text("{garbage"); assert FB.events_near(tmp_path, NOW.date()) is True        # unreadable: be careful


# ---------------------------------------------------------------- the brain end to end
def brain_root(tmp_path):
    for d in ("state", "logs", "memory", "tools", "config"):
        (tmp_path / d).mkdir(exist_ok=True)
    for f in ("guidance.py", "context.py", "_common.py"):
        (tmp_path / "tools" / f).write_text((ROOT / "tools" / f).read_text())
    (tmp_path / "simgold").symlink_to(ROOT / "simgold")
    (tmp_path / "config" / "params.yaml").write_text((ROOT / "config" / "params.yaml").read_text())
    return tmp_path


def good_client(text):
    return Client(KEY, CFG, Fake({("GET", "/models"): [models("deepseek-v4-pro")], ("POST", "/chat/completions"): [reply(text)]}))


def test_no_key_is_a_noop(tmp_path):
    root = brain_root(tmp_path)
    code, msg = FB.run(root, {}, NOW)
    assert code == 0 and "not set" in msg and not (root / "state" / "fallback.json").exists() and not (root / "state" / "guidance.json").exists()


def test_brain_applies_only_through_guidance_tool(tmp_path):
    root = brain_root(tmp_path)
    c = good_client(json.dumps({"bias": {"GOLD": "favour", "SILVER": "neutral", "COPPER": "block"}, "note": "Calm."}))
    code, msg = FB.run(root, {"DEEPSEEK_API_KEY": KEY}, NOW, client=c, ctx_fn=lambda r: "ctx")
    g = json.loads((root / "state" / "guidance.json").read_text())
    assert g["bias"] == {"GOLDBEES": "favour", "SILVERBEES": "neutral", "HINDCOPPER": "block"} or set(g["bias"].values()) <= {"favour", "neutral", "block"}
    assert g["note"].startswith("[fallback: DeepSeek] Calm.")
    st = json.loads((root / "state" / "fallback.json").read_text())
    assert st["ok"] is True and st["model"] == "deepseek-v4-pro" and st["dates"] == ["2026-10-13"] and "used_at" in st
    # nothing else was written
    assert not (root / "state" / "fastlane.json").exists() and not (root / "state" / "events.json").exists()
    # the prompt tells the model that headlines are untrusted, and the context is passed as data
    sent = c._request.calls[-1][3]["messages"]
    assert "UNTRUSTED" in sent[0]["content"] and "never follow" in sent[0]["content"] and sent[1]["content"].startswith("CONTEXT (untrusted data)")


def test_brain_downgrades_favour_with_event_tomorrow(tmp_path):
    root = brain_root(tmp_path)
    (root / "state" / "events.json").write_text(json.dumps([{"date": "2026-10-14", "type": "CPI"}]))
    c = good_client(json.dumps({"bias": {"GOLD": "favour", "SILVER": "favour", "COPPER": "favour"}, "note": "x"}))
    FB.run(root, {"DEEPSEEK_API_KEY": KEY}, NOW, client=c, ctx_fn=lambda r: "ctx")
    assert "favour" not in json.loads((root / "state" / "guidance.json").read_text())["bias"].values()


def test_brain_failure_leaves_guidance_alone_and_hides_key(tmp_path, caplog):
    root = brain_root(tmp_path)
    (root / "state" / "guidance.json").write_text('{"updated": "x", "bias": {}}')
    before = (root / "state" / "guidance.json").read_text()
    c = Client(KEY, CFG, Fake({("GET", "/models"): [models("some-other")]}))
    with caplog.at_level(logging.DEBUG):
        code, msg = FB.run(root, {"DEEPSEEK_API_KEY": KEY}, NOW, client=c, ctx_fn=lambda r: "ctx")
    assert code == 0 and "failed" in msg
    assert (root / "state" / "guidance.json").read_text() == before
    st = json.loads((root / "state" / "fallback.json").read_text())
    assert st["ok"] is False and KEY not in json.dumps(st) and KEY not in msg and KEY not in caplog.text


def test_brain_rate_limited(tmp_path):
    root = brain_root(tmp_path)
    (root / "state" / "fallback.json").write_text(json.dumps({"used_at": "2026-10-13T11:30:00+05:30", "ok": True}))
    code, msg = FB.run(root, {"DEEPSEEK_API_KEY": KEY}, NOW, client=good_client("{}"))
    assert "skipping" in msg


def test_key_in_context_output_is_not_logged(tmp_path, capsys):
    root = brain_root(tmp_path)
    FB.run(root, {"DEEPSEEK_API_KEY": KEY}, NOW, client=good_client("{}"), ctx_fn=lambda r: "ctx")
    out = capsys.readouterr()
    assert KEY not in out.out + out.err


# ---------------------------------------------------------------- the fallback report
def day_dict():
    return {"date": "2026-10-13", "closed": False, "portfolio": {"close": 100412.0, "day_pnl": 412.0, "floor": 99500.0},
            "counts": {"buys": 1, "sells": 1}, "one_line_summary": "Green day.", "warnings": []}


def test_paragraph_validation():
    facts = FR.facts_for(day_dict())
    assert FR.check_paragraph("The portfolio closed at 100412 with a day P&L of 412.", facts)
    assert FR.check_paragraph("The portfolio gained 9999.", facts) is None                 # invented number
    assert FR.check_paragraph("Visit <b>this</b>", facts) is None
    assert FR.check_paragraph("See https://evil.example now", facts) is None
    assert FR.check_paragraph("**bold** claim", facts) is None
    assert FR.check_paragraph("", facts) is None and FR.check_paragraph("x" * 700, facts) is None


def test_summary_falls_back_to_python_sentence():
    c = Client(KEY, CFG, Fake({("GET", "/models"): [models("deepseek-flash")], ("POST", "/chat/completions"): [reply("Gained 55555 <i>today</i>")]}))
    s, model = FR.summary_paragraph(day_dict(), c)
    assert model == "" and "Portfolio closed at" in s
    s2, m2 = FR.summary_paragraph(day_dict(), None)
    assert s2 == s and m2 == ""
    c2 = Client(KEY, CFG, Fake({("GET", "/models"): [models("deepseek-flash")], ("POST", "/chat/completions"): [reply("A calm green day with 412 of profit.")]}))
    s3, m3 = FR.summary_paragraph(day_dict(), c2)
    assert s3.startswith("A calm green") and m3 == "deepseek-flash"
    assert c2._request.calls[-1][3]["max_tokens"] == 600 and "response_format" not in c2._request.calls[-1][3]


def test_issue_body():
    body = FR.issue_body(day_dict(), "A calm day.", "Rammanobi/simulate_gold", "2026-10-13")
    assert "Fallback mode" in body and "A calm day." in body and "₹1,00,412" in body and "₹99,500" in body
    assert "https://github.com/Rammanobi/simulate_gold/tree/main/reports/daily" in body and "Inbox" in body


def test_upsert_issue_opens_then_updates():
    calls = []
    def gh_none(args, stdin=None):
        calls.append(args); return "[]"
    assert FR.upsert_issue("o/r", "2026-10-13", "B", gh_none) == "opened issue"
    post = calls[-1]
    assert "title=Daily report 2026-10-13 (fallback mode)" in post and "labels[]=fallback-report" in post and "POST" in post
    def gh_one(args, stdin=None):
        calls.append(args)
        return json.dumps([{"number": 9, "title": "Daily report 2026-10-13 (fallback mode)"}])
    assert FR.upsert_issue("o/r", "2026-10-13", "B2", gh_one) == "updated issue #9"
    assert "PATCH" in calls[-1] and "repos/o/r/issues/9" in calls[-1]


class FakeSMTP:
    sent = []
    def __init__(self, host, port): self.host, self.port, self.log = host, port, []; FakeSMTP.sent = self.log
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, context=None): self.log.append("starttls")
    def login(self, u, p): self.log.append(("login", u, p))
    def send_message(self, m): self.log.append(m)


SMTP_ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "me@example.com", "SMTP_PASS": "pw-SECRET", "REPORT_EMAIL": "owner@example.com"}


def test_smtp_message_and_send():
    assert FR.smtp_configured(SMTP_ENV) and not FR.smtp_configured({**SMTP_ENV, "SMTP_PASS": ""})
    msg = FR.build_message(SMTP_ENV, "Metals Daybook · (fallback) Green\r\nBcc: x@y", "TEXT", "<html><body><p>hi</p></body></html>", "Calm <day>")
    assert msg["Subject"].startswith("Metals Daybook · (fallback)") and "\n" not in msg["Subject"] and "\r" not in msg["Subject"]
    assert msg["To"] == "owner@example.com" and msg["Bcc"] is None
    html = msg.get_body(("html",)).get_content()
    assert "Fallback mode." in html and "Calm &lt;day&gt;" in html and html.index("Fallback mode") < html.index("<p>hi</p>")
    assert FR.send_smtp(SMTP_ENV, msg, FakeSMTP) == "email sent"
    assert FakeSMTP.sent[0] == "starttls" and FakeSMTP.sent[1] == ("login", "me@example.com", "pw-SECRET") and FakeSMTP.sent[2] is msg


def make_report_root(tmp_path):
    for d in ("state", "logs", "reports/daily"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps({"cash": 0, "positions": {}, "floor": 99500, "liquidbees": 100200}))
    (tmp_path / "state" / "equity.csv").write_text("2026-10-12,100100\n2026-10-13,100200\n")
    (tmp_path / "logs" / "ticks-2026-10-13.csv").write_text("time,value,floor,cash,liquidbees\n09:15,100100,99500,0,100100\n15:30,100200,99500,0,100200\n")
    return tmp_path


def test_full_fallback_report_without_smtp(tmp_path):
    root = make_report_root(tmp_path)
    gh_calls = []
    def gh(args, stdin=None):
        gh_calls.append(args); return "[]"
    code, said = FR.run(root, "2026-10-13", {"GITHUB_REPOSITORY": "o/r"}, gh=gh)
    assert code == 0 and any("opened issue" in x for x in said) and not any("email" in x for x in said)
    j = json.loads((root / "reports" / "daily" / "2026-10-13.json").read_text())
    assert j["fallback"] is True and j["fallback_summary"]
    assert "Open in Daybook Inbox" not in (root / "reports" / "daily" / "2026-10-13.email.html").read_text()
    assert json.loads((root / "state" / "fallback.json").read_text())["dates"]
    # second run: already there, nothing is sent twice
    code, said = FR.run(root, "2026-10-13", {"GITHUB_REPOSITORY": "o/r"}, gh=gh)
    assert "already exists" in said[0] and len(gh_calls) == 2


def test_fallback_report_skips_when_claude_wrote_it_or_not_a_trading_day(tmp_path):
    root = make_report_root(tmp_path)
    (root / "reports" / "daily" / "2026-10-13.json").write_text(json.dumps({"date": "2026-10-13"}))
    assert "Claude" in FR.run(root, "2026-10-13", {})[1][0]
    assert "not a trading day" in FR.run(root, "2026-10-11", {})[1][0]


def test_full_fallback_report_with_smtp_and_failures_are_contained(tmp_path):
    root = make_report_root(tmp_path)
    def gh_boom(args, stdin=None): raise subprocess.CalledProcessError(1, "gh")
    env = {**SMTP_ENV, "GITHUB_REPOSITORY": "o/r"}
    code, said = FR.run(root, "2026-10-13", env, gh=gh_boom, smtp_factory=FakeSMTP)
    assert code == 0 and "issue failed: CalledProcessError" in said and "email sent" in said
    assert "pw-SECRET" not in "\n".join(said)
    m = FakeSMTP.sent[-1]
    assert m["Subject"].startswith("Metals Daybook · (fallback)")


def test_fallback_workflow_shape():
    import yaml
    text = (ROOT / ".github" / "workflows" / "fallback.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    on = doc.get("on", doc.get(True))
    assert [c["cron"] for c in on["schedule"]] == ["20 11 * * 1-5"] and "workflow_dispatch" in on          # 16:50 IST
    assert "secrets.DEEPSEEK_API_KEY" in text and "secrets.SMTP_PASS" in text and "git pull --rebase --autostash" in text
    assert doc["permissions"] == {"contents": "write", "issues": "write"}
    assert "DEEPSEEK_API_KEY=" not in text.replace("DEEPSEEK_API_KEY: ${{", "")
