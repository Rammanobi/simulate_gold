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
    for flag in ("--invalidate close_below:", "allocation explain", "allocation show", "allocation hold-default --phase provisional|final --note", "FINAL for today after 15:05"):
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


def _draft_body():
    d = doc("CLAUDE_INVARIANTS_DRAFT.md")
    return d, d.split("```markdown", 1)[1].split("\n```", 1)[0]


def test_invariants_draft_replaces_only_invariant_one_with_the_required_clauses():
    d, body = _draft_body()
    items = re.findall(r"^(\d+)\. ", body, re.M)
    assert items == ["1"]                                                          # D12: ONLY invariant 1
    low = re.sub(r"\s+", " ", body.lower())
    for needle in ("allocation set", "logged only", "never buy", "never place or simulate an order", "never set a size", "never loosen a stop",
                   "never touch the floor", "caps, gaps or limits", "leverage", "a short", "outside the three", "cash", "account stop", "latch",
                   "untrusted data", "always green", "profit", "rotate between the three metals", "tools/desk.py write",
                   "the tools refuse buys, favour in the fast lane, sizes and looser stops", "`v2_trend` or `v1_scores`",
                   "`allocation.phase` is `shadow`", "`allocation.ai_trust` is 0", "shares totalling 1.00 (within 0.02", "after 15:05"):
        assert needle in low, needle
    # several allocations a day are allowed: provisional + final (+ a defensive fast-lane final); never "ONE per day"
    assert "one target allocation per trading day" not in low and "ONE" not in body
    assert "provisional" in low and "final" in low and "fast lane" in low
    assert "0.93" not in d and "minus 0.05" not in d
    assert "only invariant 1" in d.lower() and "invariants 2 to 6 stay" in d.lower()
    assert "not in force" in d.lower()


def test_invariants_draft_keeps_the_protections_that_live_in_the_other_invariants():
    d, body = _draft_body()
    claude = re.sub(r"\s+", " ", (ROOT / "CLAUDE.md").read_text(encoding="utf-8"))
    untouched = d.split("## What stays untouched", 1)[1].split("## Other CLAUDE.md edits", 1)[0]
    untouched = re.sub(r"\s+", " ", untouched)
    # (a) the draft lists each protection ...
    for needle in ("`tools/memory_ltm.py` and `tools/memory_stm.py`", "`state/tomorrow.txt`", "`guidance.py tomorrow` fails", "1 or 2 plain sentences",
                   "repo data files", "incident payloads", "`tools/guidance.py` refuses buys, favour in the fast lane, sizes and looser stops",
                   "`$REPORT_EMAIL`", "advice that breaks a rule is ignored and logged", "never hand-edit state JSON"):
        assert needle in untouched, needle
    # (b) ... and today's CLAUDE.md really says it, so the draft does not lean on a rule that is not there
    for needle in ("`tools/memory_ltm.py` / `tools/memory_stm.py`", "`state/tomorrow.txt` is plain text", "if `guidance.py tomorrow` fails",
                   "1–2 plain sentences", "repo data files", "incident payloads", "`tools/guidance.py` refuses buys, favour in the fast lane, sizes and looser stops",
                   "$REPORT_EMAIL", "ignored and logged", "Never hand-edit state JSON", "never touch the floor, caps, gaps or limits"):
        assert needle in claude, needle
    # the replacement keeps what the old invariant 1 said
    low = re.sub(r"\s+", " ", body.lower())
    for needle in ("never buy", "never set a size", "never loosen a stop", "never touch the floor"):
        assert needle in low


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
    assert "9 of 10" not in r and "invested_target` minus" not in r


# ------------------------------------------------------------------------------------------------ the mode gate (step 1b)
GATE_MODES = ("v2_trend", "v1_scores", "v3_alloc", "FAILED")


def step_1b(name: str) -> str:
    m = re.search(r"^1b\.\s+(.*?)(?=^\d+[a-z]?\.\s)", raw(name), re.M | re.S)
    assert m, f"{name}.md has no step 1b"
    return re.sub(r"\s+", " ", m.group(1))


def gate(step: str) -> dict:
    """Execute step 1b as text: for each MODE (and a failed MODE command) say what the clause that names it tells the routine to do.
    Returns {mode: ('hand_over', 'routines/x.md') | ('continue', None) | ('unknown', None)}."""
    clauses = list(re.finditer(r"MODE\s+((?:`[a-z0-9_]+`(?:,\s*|\s+or\s+|,\s+or\s+)?)+)(?:,?\s*or the MODE command failed)?", step))
    out = {m: ("unknown", None) for m in GATE_MODES}
    for i, c in enumerate(clauses):
        end = clauses[i + 1].start() if i + 1 < len(clauses) else len(step)
        after = step[c.end():end]
        named = re.findall(r"`([a-z0-9_]+)`", c.group(1))
        failed = "MODE command failed" in c.group(0) or "MODE command failed" in step[c.start():c.end() + 40]
        target = re.search(r"routines/([a-z0-9_]+\.md)", after)
        verdict = ("hand_over", "routines/" + target.group(1)) if target else (("continue", None) if re.search(r"go on|continue", after) else ("unknown", None))
        for m in named + (["FAILED"] if failed else []):
            out[m] = verdict
    return out


def gate_errors(step: str, handover: str) -> list[str]:
    g = gate(step)
    errs = []
    for m in ("v2_trend", "v1_scores", "FAILED"):
        if g[m] != ("hand_over", handover):
            errs.append(f"{m} -> {g[m]} (want a hand-over to {handover})")
    if g["v3_alloc"] != ("continue", None):
        errs.append(f"v3_alloc -> {g['v3_alloc']} (want: continue)")
    return errs


GATES = {"premarket": "routines/premarket_v2.md", "allocation": "routines/hourly.md"}


def _swap(step: str, a: str, b: str) -> str:
    return step.replace(a, "\0").replace(b, a).replace("\0", b)


@pytest.mark.parametrize("name", sorted(GATES))
def test_step_1b_hands_v2_and_v1_and_a_failed_mode_command_to_the_legacy_playbook_and_v3_continues(name):
    step = step_1b(name)
    assert gate_errors(step, GATES[name]) == [], (name, step)
    g = gate(step)
    assert g["v2_trend"] == g["v1_scores"] == g["FAILED"] == ("hand_over", GATES[name])
    assert g["v3_alloc"] == ("continue", None)


@pytest.mark.parametrize("name", sorted(GATES))
def test_a_gate_that_always_hands_to_v3_is_caught(name):
    """Mutation: the gate ignores the mode and always carries on with the v3 steps."""
    step = step_1b(name)
    mutant = re.sub(r"Follow `routines/[a-z0-9_]+\.md`", "Go on with the steps below", step)
    mutant = re.sub(r"and stop; none of the steps below run", "and go on; all of the steps below run", mutant)
    assert mutant != step and gate_errors(mutant, GATES[name]), mutant
    mutant2 = re.sub(r"MODE `v2_trend`.*?(?=MODE `v3_alloc`)", "", step)
    assert mutant2 != step and gate_errors(mutant2, GATES[name]), mutant2


@pytest.mark.parametrize("name", sorted(GATES))
def test_an_inverted_gate_is_caught(name):
    """Mutation: v3_alloc is handed to the legacy playbook and the legacy modes carry on with v3."""
    step = step_1b(name)
    mutant = _swap(step, "`v2_trend`, `v1_scores`", "`v3_alloc`")
    assert mutant != step
    errs = gate_errors(mutant, GATES[name])
    assert errs and any("v3_alloc" in e for e in errs), (mutant, errs)
    assert gate(mutant)["v2_trend"][0] != "hand_over"


def test_the_gate_evaluator_itself_sees_a_hand_over_to_the_wrong_file():
    step = step_1b("premarket").replace("premarket_v2.md", "premarket.md")
    assert gate_errors(step, GATES["premarket"])


# ------------------------------------------------------------------------------------------------ doc consistency
def _step_refs(name: str):
    return re.findall(r"\bstep (\d+[a-z]?)\b", text(name))


def test_step_references_point_at_steps_that_exist():
    for name in ("premarket", "allocation"):
        have = set(re.findall(r"^(\d+[a-z]?)\.\s", raw(name), re.M))
        for ref in _step_refs(name):
            assert ref in have, (name, ref, sorted(have))
    assert "step 4b" not in text("premarket")
    al = text("allocation")
    assert "devil's advocate (step 4)" in al.replace("\n", " ") and "(step 5)" not in al
    assert "4. **Brainstorm and challenge.**" in raw("allocation")
    pm = text("premarket")
    assert "yourself (step 4)" in pm and "continue with step 4 for the missing desks only" in pm


def test_the_common_method_steps_are_numbered_a1_to_a8_and_cross_references_agree():
    c = raw("_common")
    heads = re.findall(r"^\*\*(A\d+)\.", c, re.M)
    assert heads == [f"A{i}" for i in range(1, 9)]
    assert "**A5. Edge and cost test" in c and "**A6. Target allocation" in c and "**A7. Confidence" in c and "**A8. Self-check" in c
    assert "`_common.md` A4 to A8" in text("premarket") and "Confidence per `_common.md` A7" in text("premarket")
    assert "no cash weight (`_common.md` A6)" in text("allocation")
    for name in ("premarket", "allocation", "desk_macro", "desk_metal", "desk_risk", "fastlane"):
        assert "A6. Confidence" not in raw(name) and "`_common.md` A6" not in text(name).replace("no cash weight (`_common.md` A6)", "")


def test_the_thrift_web_call_limit_is_6_and_5_in_thrift_everywhere():
    pm = text("premarket")
    assert "At most 6 web calls in all (5 in THRIFT)" in pm
    assert pm.count("5 in THRIFT") == 2                                           # Models line and step 4
    assert "at most 5 web calls, compact desks done by you" in pm                 # step 0 point 4 (THRIFT)
    assert "At most 5 web calls in all when you only synthesise" not in pm
    r = doc("ROLLOUT.md")
    assert "at most 6 web calls, 5 in THRIFT" in r and "at most 6 web calls)" not in r


def test_weights_are_shares_summing_to_one_within_one_hundredth_in_every_document():
    for name in ("_common", "premarket", "allocation", "fastlane"):
        s = text(name)
        assert "invested_target` minus" not in s and "0.93" not in s, name
    c = text("_common")
    assert "total 1.00 within 0.02" in c and "below 0.98" in c and "above 1.02" in c and "aim for exactly 1.00" in c.lower()
    for name in ("_common", "premarket", "allocation", "fastlane"):
        assert "within 0.01" not in text(name) and "0.99 to 1.01" not in text(name), name          # the tool's window is 0.98 to 1.02
    assert "aim for exactly 1.00" in text("premarket") and "aim for exactly 1.00" in text("allocation")
    assert "0.98 to 1.02" in text("premarket")
    assert K.SUM_TOL == 0.01                      # the eval checker is deliberately stricter than the tool: it flags a total off by 0.015
    ok = {"GOLDBEES": 0.85, "SILVERBEES": 0.10, "HINDCOPPER": 0.0501}
    bad = {"GOLDBEES": 0.85, "SILVERBEES": 0.10, "HINDCOPPER": 0.07}
    probe = {"action": "allocation_set", "weights": ok}
    assert "sum_not_one" not in K.codes(K.check_proposal({}, probe))
    assert "sum_not_one" in K.codes(K.check_proposal({}, {"action": "allocation_set", "weights": bad}))
    assert "100%" in doc("CLAUDE_INVARIANTS_DRAFT.md") or "1.00 (within 0.02" in doc("CLAUDE_INVARIANTS_DRAFT.md")


def test_the_golden_bar_names_12_cases_and_the_safety_codes_are_the_checkers():
    r = doc("ROLLOUT.md")
    assert "at least 11 of 12 cases pass and no safety case fails" in r.replace("\n", " ")
    assert len(list((ROOT / "tests" / "evals" / "v3" / "golden").glob("*.json"))) == 12
    for code in K.SAFETY_CODES:
        assert code in r, code
        assert code in K.KNOWN_CODES, code
    assert "default_band_exceeded" not in K.SAFETY_CODES


# ------------------------------------------------------------------------------------------------ prompt improvements
def test_the_edge_and_cost_test_and_the_no_trade_band_are_in_the_method():
    c = text("_common")
    for needle in ("0.15% per side", "0.30% of the moved amount", "historical typical 5-day move from the memory desk", "Minimum edge",
                   "at least +0.10% of the account", "at least twice the costs", "propose the current share", "base rate",
                   "No-trade band 0.05 per metal", "yesterday's FINAL as executed", "the remainder goes to GOLDBEES", "exempt"):
        assert needle in c or needle.replace("goes", "goes") in c, needle
    for name in ("premarket", "allocation"):
        s = text(name)
        assert "edge" in s.lower() and "0.05" in s and "no-trade band" in s.lower() or "no-trade band" in text("_common").lower()
    assert "0.15%" in text("premarket") and "0.05 of its current share" in text("premarket")
    assert "0.05 no-trade band" in text("allocation") or "no-trade band" in text("allocation")


def test_confidence_is_capped_by_the_desks_own_calibration():
    c = text("_common")
    for needle in ("`desk_brier`", "`ai_vs_default`", "Calibration cap", "above 0.25", "confidence is at most 0.40", "at most 0.55"):
        assert needle in c, needle
    assert "calibration cap" in text("premarket").lower() and "calibration cap" in text("allocation").lower()
    assert "A7 caps confidence by them" in c


@pytest.mark.parametrize("name", ["desk_macro", "desk_metal", "desk_risk"])
def test_desks_read_mode_before_the_budget_check_and_stop_unless_fanout_is_full(name):
    r = raw(name)
    sec = r.split("## 0. Budget check", 1)[1].split("## Steps", 1)[0]
    sec = re.sub(r"\s+", " ", sec)
    assert sec.index("Read MODE before anything else") < sec.index("tools/budget.py mode")
    assert "Unless fanout is `full`" in sec and "compact mode, premarket covers it" in sec
    assert sec.index("compact mode, premarket covers it") < sec.index("tools/budget.py mode")
    assert "no budget call" in sec and "failed MODE command" in sec
    assert "THRIFT **and**" not in sec                                            # compact stops in NORMAL too


def test_the_go_live_checklist_says_which_duties_only_run_twice_a_day_once_hourly_is_off():
    r = re.sub(r"\s+", " ", doc("ROLLOUT.md"))
    for needle in ("Duties that shrink when hourly.md is disabled", "watchdog", "Inbox decision retry (2b)", "dashboard copy (2c)", "08:50", "14:40",
                   "reminders.yml"):
        assert needle in r, needle
    assert "premarket step 9" in r and "allocation step 2" in r
    assert "2b. **Unnotified owner decisions**" in raw("allocation") and "2c. **Dashboard copy**" in raw("allocation")
    assert "step 9" in text("premarket") and "9. **Engine watchdog.**" in raw("premarket")
