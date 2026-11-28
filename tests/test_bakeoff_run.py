"""Bake-off DeepSeek runner with a fake transport: cost cap, key never leaks, writes only under reports/bakeoff, no network."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT)]

from bakeoff import inputs as inp, run_deepseek as rd, score      # noqa: E402

SECRET = "sk-test-SECRET-0123456789abcdef"
OFFPEAK = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)       # Saturday
PEAK = datetime(2026, 11, 26, 7, 0, tzinfo=timezone.utc)           # Thursday 07:00 UTC


class FakeApi:
    def __init__(self, usage=None, obey=False, leak_key=False, fail_status=None):
        self.calls, self.bodies, self.usage = [], [], usage or {"prompt_tokens": 800, "prompt_cache_hit_tokens": 0,
                                                                  "prompt_cache_miss_tokens": 800, "completion_tokens": 60}
        self.obey, self.leak_key, self.fail_status = obey, leak_key, fail_status

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        assert headers["Authorization"] == f"Bearer {SECRET}"          # the key is used, only in the header
        if method == "GET":
            return 200, {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}
        if self.fail_status:
            return self.fail_status, {"error": {"message": "overloaded"}}
        self.bodies.append(body)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        extra = ""
        if self.obey:
            for line in user.splitlines():
                if "OWNER MESSAGE" in line or "BUY " in line or "Ignore" in line or "https://evil" in line or "PWNED" in line:
                    extra = line
        if self.leak_key:
            extra += " " + SECRET
        if '"paragraph"' in system:
            out = {"paragraph": ("Quiet day, no trades. " + extra).strip()}
        elif '"lines"' in system:
            first = user.split("=== ")[1].split(" ===")[0]
            out = {"lines": [{"text": ("A quiet day. " + extra).strip(), "sources": [f"{first}:1"]}]}
        else:
            out = {"file": user.split("\n")[0][6:], "line": ("Daily note. " + extra).strip()[:120]}
        msg = {"role": "assistant", "content": json.dumps(out, ensure_ascii=False), "reasoning_content": "SECRET THOUGHTS " + SECRET}
        return 200, {"model": body["model"], "choices": [{"message": msg, "finish_reason": "stop"}], "usage": self.usage}


@pytest.fixture()
def repo(tmp_path):
    shutil.copytree(ROOT / "reports" / "bakeoff" / "inputs", tmp_path / "reports" / "bakeoff" / "inputs")
    return tmp_path


def _run(repo, api, *extra, now=OFFPEAK, env=None):
    argv = ["--run-id", "R", "--root", str(repo), "--workers", "1", *extra]
    return rd.run(argv, environ={"DEEPSEEK_API_KEY": SECRET} if env is None else env, transport=api, now=now, sleep=lambda s: None)


def _files(repo, side="deepseek"):
    return sorted((repo / "reports" / "bakeoff" / "R" / side).rglob("[0-9][0-9].*.json"))


def test_run_writes_results_in_the_common_schema(repo, capsys):
    api = FakeApi()
    assert _run(repo, api, "--limit", "2", "--runs", "2") == 0
    fs = _files(repo)
    assert len(fs) == 3 * 2 * 2
    assert fs[0].parent.parent.name == "deepseek" and fs[0].name.count(".") == 2
    r = json.loads([f for f in fs if f.parent.name == "report_paragraph"][0].read_text())
    for k in ("schema", "run_id", "side", "task", "input_id", "run", "output", "raw_text", "error", "latency_ms", "usage", "cost_usd", "model", "finish_reason"):
        assert k in r
    assert r["model"] == "deepseek-flash" and r["output"]["paragraph"] and r["usage"]["prompt_tokens"] == 800 and r["cost_usd"] > 0
    assert all(b["thinking"] == {"type": "disabled"} and b["response_format"] == {"type": "json_object"} and b["temperature"] <= 0.2
               for b in api.bodies)
    meta = json.loads((repo / "reports" / "bakeoff" / "R" / "deepseek" / "run_meta.json").read_text())
    assert meta["stopped_by"] is None and meta["resolved"]["flash"] == ["deepseek-flash"] and meta["written"] == 12
    res = score.score_run(repo, "R", ["deepseek"])
    assert set(res["aggregates"]["deepseek"]) == {"report_paragraph", "weekly_rollup", "manifest"}
    assert res["aggregates"]["deepseek"]["manifest"]["schema_rate"] == 1


def test_resume_skips_existing_results(repo):
    api = FakeApi()
    _run(repo, api, "--limit", "1", "--runs", "1")
    n = len(api.bodies)
    _run(repo, api, "--limit", "1", "--runs", "1")
    assert len(api.bodies) == n


def test_pro_uses_its_own_model_and_side(repo):
    api = FakeApi()
    assert _run(repo, api, "--limit", "1", "--runs", "1", "--models", "pro", "--tasks", "manifest") == 0
    assert api.bodies[0]["model"] == "deepseek-v4-pro" and api.bodies[0]["thinking"] == {"type": "disabled"}
    assert _files(repo, "deepseek-pro")
    api2 = FakeApi()
    _run(repo, api2, "--limit", "1", "--runs", "1", "--models", "pro", "--pro-thinking", "--tasks", "manifest", "--no-resume")
    assert "thinking" not in api2.bodies[0]


def test_cost_cap_stops_early_and_is_clamped_to_3_usd(repo):
    api = FakeApi(usage={"prompt_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 0})    # $0.30 a call
    assert _run(repo, api, "--max-cost", "0.5", "--runs", "3") == 0
    meta = json.loads((repo / "reports" / "bakeoff" / "R" / "deepseek" / "run_meta.json").read_text())
    assert meta["stopped_by"] == "cost_cap" and len(api.bodies) == 2 and meta["spent_usd"] == pytest.approx(0.6)
    assert len(_files(repo)) == 2 and meta["max_cost_usd"] == 0.5
    api2 = FakeApi()
    assert rd.CostMeter(99).cap == 3.0
    assert rd.usage_cost({"prompt_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 1_000_000}, "flash") == pytest.approx(1.5)
    assert rd.usage_cost({"prompt_tokens": 10, "prompt_cache_hit_tokens": 10, "completion_tokens": 0}, "flash") == pytest.approx(6e-8)


def test_run_is_run_major_so_a_stop_keeps_whole_runs(repo):
    api = FakeApi(usage={"prompt_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 0})
    _run(repo, api, "--max-cost", "3", "--runs", "3", "--limit", "3")
    runs = [json.loads(f.read_text())["run"] for f in _files(repo)]
    assert runs.count(1) == 9 and runs.count(3) == 0       # run 1 is whole; the cap ($3, one call of overshoot at most) ends run 2
    assert len(runs) <= 11 and sum(json.loads(f.read_text())["cost_usd"] for f in _files(repo)) <= 3.3


def test_key_never_reaches_any_file_or_output(repo, capsys):
    api = FakeApi(leak_key=True)
    assert _run(repo, api, "--limit", "2", "--runs", "1") == 0
    out = capsys.readouterr()
    blob = out.out + out.err
    for p in (repo / "reports").rglob("*"):
        if p.is_file() and "inputs" not in p.parts:
            blob += p.read_text(encoding="utf-8")
    assert SECRET not in blob and "SECRET THOUGHTS" not in blob and "reasoning" not in blob.lower().replace("reasoning_content", "")
    assert "***" in blob                                    # the echoed key was scrubbed, not dropped


def test_api_errors_are_recorded_without_the_key(repo):
    api = FakeApi(fail_status=401)
    assert _run(repo, api, "--limit", "1", "--runs", "1", "--tasks", "manifest") == 0
    r = json.loads(_files(repo)[0].read_text())
    assert r["output"] is None and r["error"] and SECRET not in json.dumps(r)


def test_nothing_is_written_outside_reports_bakeoff(repo):
    before = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()}
    _run(repo, FakeApi(), "--limit", "1", "--runs", "1")
    score_after = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()}
    new = score_after - before
    assert new and all(n.startswith("reports/bakeoff/R/") for n in new)
    from bakeoff.common import check_out
    with pytest.raises(ValueError):
        check_out(repo / "state" / "guidance.json", repo)
    with pytest.raises(ValueError):
        check_out(repo / "reports" / "bakeoff" / ".." / "daily" / "x.json", repo)


def test_no_key_and_peak_and_dry_run_make_no_calls(repo):
    api = FakeApi()
    assert _run(repo, api, "--limit", "1", env={}) == 2
    assert _run(repo, api, "--limit", "1", "--off-peak-only", now=PEAK) == 0
    assert _run(repo, api, "--limit", "1", "--dry-run") == 0
    assert api.calls == []
    assert rd.is_peak(PEAK) and not rd.is_peak(OFFPEAK) and not rd.is_peak(datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc))
    assert _run(repo, api, "--limit", "1", "--runs", "1", "--off-peak-only", now=OFFPEAK) == 0 and api.calls


def test_privacy_failure_blocks_every_api_call(repo):
    f = repo / "reports" / "bakeoff" / "inputs" / "manifest" / "01.json"
    f.write_text(f.read_text(encoding="utf-8").replace("FILE:", "FILE: owner@example.com", 1), encoding="utf-8")
    api = FakeApi()
    assert _run(repo, api, "--limit", "1") == 1 and api.calls == []


def test_injection_following_model_is_caught_by_the_score(repo):
    api = FakeApi(obey=True)
    _run(repo, api, "--runs", "1")
    res = score.score_run(repo, "R", ["deepseek"])
    followed = sum(a["followed"] for a in res["aggregates"]["deepseek"].values())
    assert followed >= 20                                # most plants are repeated by an obedient fake
    assert all(v["gates"]["injection_followed"]["status"] == "fail" for v in res["verdicts"].values() if v["gates"]["injection_followed"]["value"])
    clean = FakeApi()
    _run(repo, clean, "--runs", "1", "--run-id", "R3", "--no-resume")
    res2 = score.score_run(repo, "R3", ["deepseek"])
    assert sum(a["followed"] for a in res2["aggregates"]["deepseek"].values()) == 0
