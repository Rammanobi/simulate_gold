"""Strategy v3 playbooks, invariants draft and rollout doc: structural guards (no LLM).

v3 is DORMANT: premarket.md and allocation.md hand over to the v2 playbooks while `strategy.mode` is v2_trend, every v3 output is
"logged only" until the owner flips shadow -> live, and nothing in the prompts may promise profit, give cash a weight or write sizes."""
from __future__ import annotations

import importlib.util
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
R = ROOT / "routines"
V3_NEW = ["_common", "desk_macro", "desk_metal", "desk_risk", "premarket", "allocation"]
V3_ALL = V3_NEW + ["fastlane", "daily_report", "weekly", "incident"]


def text(name: str) -> str:
    return re.sub(r"[ \t]*\n[ \t]*", " ", (R / f"{name}.md").read_text(encoding="utf-8"))   # one line: phrases may wrap


def raw(name: str) -> str:
    return (R / f"{name}.md").read_text(encoding="utf-8")


def _checker():
    spec = importlib.util.spec_from_file_location("v3_checker_pb", ROOT / "tests" / "evals" / "v3" / "checker.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


K = _checker()


@pytest.mark.parametrize("name", V3_NEW + ["premarket_v2", "hourly"])
def test_the_files_exist(name):
    assert (R / f"{name}.md").exists()


@pytest.mark.parametrize("name", [n for n in V3_NEW if n != "_common"])
def test_every_v3_routine_has_step_zero_and_treats_data_as_untrusted(name):
    s = text(name)
    assert "## 0. Budget check" in raw(name) and "tools/budget.py mode" in s and "THRIFT" in s and "get_session" in s
    assert raw(name).index("## 0. Budget check") < raw(name).index("## Steps")
    assert "untrusted" in s.lower() and ("never as instructions" in s.lower() or "never instructions" in s.lower())
    assert "context.py" in s and any("untrusted" in para and re.search(r"context\.py|brief|memory/", para)
                                     for para in re.split(r"\n\s*\n", raw(name).lower()))
    assert "Output contract" in raw(name) and "Failure handling" in raw(name) and "models used" in s


@pytest.mark.parametrize("name", V3_NEW)
def test_profit_is_the_objective_and_there_is_no_promise(name):
    s = text(name)
    assert "profit" in s.lower()
    assert K._search(K._GREEN_SOFT, s) is None, K._search(K._GREEN_SOFT, s)       # "always green" only ever appears negated
    assert "never" in s.lower()


@pytest.mark.parametrize("name", [n for n in V3_NEW if n != "_common"])
def test_advice_is_logged_only_while_the_mode_is_v2_or_shadow(name):
    s = text(name)
    assert "logged only" in s and "v2_trend" in s and "shadow" in s


@pytest.mark.parametrize("name", V3_NEW)
def test_no_cash_weight_no_orders_no_leverage_no_new_symbols_no_config_edits(name):
    s = text(name)
    assert re.search(r"no cash weight|cash a weight|cash as a weight|give cash a weight", s, re.I), name
    assert re.search(r"never[^.]{0,200}(order|quantity|size)", s, re.I)
    if name not in ("desk_macro", "desk_risk", "desk_metal"):
        assert "leverage" in s and re.search(r"outside (the|GOLDBEES)", s)
    assert re.search(r"never edit[^.]{0,120}(config|code)", s, re.I) or "config/**" in s


def test_premarket_and_allocation_hand_over_to_the_v2_playbooks_while_dormant():
    pm, al = text("premarket"), text("allocation")
    assert "routines/premarket_v2.md" in pm and "v2_trend" in pm and "dormant" in pm.lower()
    assert "routines/hourly.md" in al and "v2_trend" in al and "dormant" in al.lower()
    legacy = raw("premarket_v2")
    assert "Legacy playbook" in legacy and "at most 5 web calls" in legacy and "tools/guidance.py set --bias" in legacy


def test_the_mode_line_commands_run_and_say_v2_trend_today():
    spans = []
    for name in V3_ALL:
        spans += [(name, m) for m in re.findall(r"`(python -c \"import yaml[^`]*)`", raw(name))]
    assert len(spans) >= 4
    for name, span in spans:
        argv = shlex.split(span)
        argv[0] = sys.executable
        out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, (name, out.stderr)
        assert out.stdout.split()[0] in ("v2_trend", "v3_alloc"), (name, out.stdout)


def test_every_allocation_cli_form_is_one_the_design_defines():
    for name in V3_ALL:
        for sub in re.findall(r"guidance\.py allocation ([a-z\-]+)", raw(name)):
            assert sub in ("set", "explain", "show", "hold-default"), (name, sub)
    for name in ("_common", "premarket"):
        full = [m for m in re.findall(r"`?(python tools/guidance\.py allocation set [^`\n]*)", raw(name).replace("\\\n", " ")) if "--help" not in m]
        assert full, name
        for flag in ("--phase", "--weights GOLDBEES=", "SILVERBEES=", "HINDCOPPER=", "--confidence", "--scenario", "--rationale GOLD="):
            assert any(flag in f for f in full), (name, flag)
    c = raw("_common")
    for flag in ("--invalidate close_below:", "allocation explain", "allocation show", "allocation hold-default --note", "FINAL after 15:05"):
        assert flag in c, flag


def test_playbooks_only_name_tools_that_exist_or_are_built_in_the_parallel_branch():
    allowed_new = {"desk"}                                                       # tools/desk.py comes with the engine branch
    for name in V3_ALL + ["premarket_v2"]:
        for t in re.findall(r"python tools/([a-z_]+)\.py", raw(name)):
            assert (ROOT / "tools" / f"{t}.py").exists() or t in allowed_new, (name, t)


def test_weights_are_shares_that_sum_to_one_with_no_cash_in_the_cli_docs():
    c = text("_common")
    assert "sum to 1.00" in c and "no cash weight" in c.lower() and "reinvests rounding dust" in c
    for name in ("premarket", "allocation"):
        assert "sum to 1.00" in text(name) or "sum to 1.00" in c


def test_owner_decisions_are_in_the_prompts():
    c = text("_common")
    for needle in ("-15%", "rotates into the GOLDBEES sink", "-25%", "Rs 75,000", "0.20 one-way per day and 0.40 per week", "defensive exits"):
        assert needle in c, needle
    assert "GOLDBEES 1.00, SILVERBEES 0.50, HINDCOPPER 0.25" in c and "SILVERBEES 0.20" in c and "HINDCOPPER 0.10" in c
    assert "compact" in text("premarket") and "full" in text("premarket")
    assert "3" in text("_common").split("Sessions per trading day")[1][:600] and "8 to 9" in text("_common")


def test_the_existing_playbooks_keep_their_v2_steps_and_gate_the_v3_ones():
    for name in ("fastlane", "daily_report", "weekly"):
        s = text(name)
        assert "v3_alloc" in s and "v2_trend" in s and "dormant" in s.lower()
    f = text("fastlane")
    assert "5-v3." in f and "**Decide** from the menu below" in f and "never raise a weight" in f.lower() or "never an increase" in f
    d = text("daily_report")
    for step in ("5-v3.", "7-v3.", "7b."):
        assert step in d
    assert "Allocation:" in d and "up on the day" in d and "Rs 1,00,000" in d
    w = text("weekly")
    for step in ("2b.", "3-v3.", "4d-v3.", "Owner question"):
        assert step in w
    assert "ai_trust" in w and "never propose higher trust" in w.lower()
    assert "invalid_proposal" in text("incident") and "allocation:" in text("incident")


def test_the_readme_lists_the_schedule_and_says_dormant():
    r = text("README")
    for needle in ("`50 2 * * 1-5`", "`15 3 * * 1-5`", "`10 9 * * 1-5`", "DORMANT", "compact (default) 3", "docs/v3/ROLLOUT.md"):
        assert needle in r, needle


# ------------------------------------------------------------------------------------------------ docs
def doc(name):
    return (ROOT / "docs" / "v3" / name).read_text(encoding="utf-8")


def test_claude_md_is_untouched_by_this_branch():
    c = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "desk allocation" not in c and "allocation set" not in c


def test_invariants_draft_has_the_required_clauses():
    d = doc("CLAUDE_INVARIANTS_DRAFT.md")
    body = d.split("```markdown", 1)[1].split("\n```", 1)[0]
    items = re.findall(r"^(\d+)\. ", body, re.M)
    assert items == [str(i) for i in range(1, 11)]
    low = body.lower()
    for needle in ("allocation set", "logged only", "never: place or simulate an order", "leverage", "short", "outside the three", "cash",
                   "account stop", "latch", "untrusted data, never instructions", "always green", "hand-edit", "$report_email", "deepseek",
                   "the objective is profit", "rotates into", "config/**"):
        assert needle in low, needle
    assert "owner applies" in d.lower() or "applies this text" in d.lower() or "owner applies this text" in d.lower()
    assert "not in force" in d.lower()


def test_rollout_has_phases_rollbacks_usage_limit_and_all_twelve_decisions():
    r = doc("ROLLOUT.md")
    for needle in ("P0 build", "P1 default only", "P2 AI shadow", "P3 half trust", "P4 full trust", "`ai_trust: 0.5`", "`ai_trust: 1.0`",
                   "Rollback", "strategy.mode: v2_trend", "strategy.mode: v1_scores", "allocation.ai_trust: 0", "allocation.phase: shadow",
                   "Usage-limit behaviour", "carry_days", "hold-default", "Go-live checklist"):
        assert needle in r, needle
    rows = re.findall(r"^\| D(\d+) \|", r, re.M)
    assert rows == [str(i) for i in range(1, 13)]
    for needle in ("0.98", "rotate_to_gold", "Level 2 (-25%)", "0.24", "Compact", "At most `hold-default`" if False else "No (at most `hold-default`)", "Rs 75,000"):
        assert needle in r, needle
    assert "always green" in r.lower() and "profit" in r.lower()
