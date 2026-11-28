"""Bake-off safety: workflow lints, dormancy, Python 3.11-safe syntax, no engine/routine reach."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "bakeoff.yml"
DOC = yaml.safe_load(WF.read_text(encoding="utf-8"))
TEXT = re.sub(r"(?m)^\s*#.*$", "", WF.read_text(encoding="utf-8"))      # comments dropped


def test_workflow_is_dispatch_only_with_least_privilege():
    on = DOC.get("on", DOC.get(True))
    assert list(on) == ["workflow_dispatch"]
    assert set(on["workflow_dispatch"]["inputs"]) >= {"tasks", "runs", "max_cost_usd", "off_peak_only"}
    assert on["workflow_dispatch"]["inputs"]["max_cost_usd"]["default"] == "3"
    assert on["workflow_dispatch"]["inputs"]["off_peak_only"]["default"] is False
    assert DOC["permissions"] == {"contents": "write"}
    assert DOC["concurrency"]["group"] == "bakeoff"
    job = DOC["jobs"]["bakeoff"]
    assert job["timeout-minutes"] == 30
    for bad in ("push:", "schedule:", "pull_request", "workflow_run", "cron"):
        assert bad not in TEXT


def test_workflow_has_only_the_deepseek_secret_and_never_prints_it():
    assert set(re.findall(r"secrets\.([A-Z_]+)", TEXT)) == {"DEEPSEEK_API_KEY"}
    for w in ("SMTP", "REPORT_EMAIL", "ANTHROPIC", "ROUTINE_", "GH_TOKEN"):
        assert w not in TEXT
    for line in TEXT.splitlines():
        if "secrets." in line:
            assert line.strip().split(":")[0].isupper()                       # NAME: ${{ secrets.X }} only
        if "echo" in line:
            assert "secrets." not in line and "set -x" not in line
    assert "add-mask::$DEEPSEEK_API_KEY" in TEXT
    steps = DOC["jobs"]["bakeoff"]["steps"]
    holders = [s["name"] for s in steps if "DEEPSEEK_API_KEY" in (s.get("env") or {})]
    assert holders == ["Run DeepSeek and score"]                              # only that step sees the key


def test_workflow_verifies_inputs_before_the_api_call_and_commits_only_bakeoff():
    steps = DOC["jobs"]["bakeoff"]["steps"]
    runs = [s.get("run", "") for s in steps]
    i_priv = next(i for i, r in enumerate(runs) if "privacy.py" in r and "inputs.py verify" in r)
    i_api = next(i for i, r in enumerate(runs) if "run_deepseek.py" in r)
    assert i_priv < i_api and "DEEPSEEK_API_KEY" not in (steps[i_priv].get("env") or {})
    commit = [r for r in runs if "git commit" in r][0]
    adds = re.findall(r"git add [^\n]*", commit)
    assert adds and all(a.strip().endswith("-- reports/bakeoff") for a in adds)
    assert not re.search(r"git add (?!-A -- reports/bakeoff)", commit) and "git push" in commit
    assert "--force" not in commit and "-f " not in commit


def test_nothing_outside_the_bakeoff_imports_it():
    needle = re.compile(r"\bbakeoff\b")
    allowed = {WF.resolve(), (ROOT / "tools" / "bakeoff").resolve()}
    hits = []
    for base in ("simgold", "tools", "routines", ".github", "config"):
        for p in (ROOT / base).rglob("*"):
            if not p.is_file() or p.suffix in (".pyc",) or any(a == p.resolve() or a in p.resolve().parents for a in allowed):
                continue
            if "__pycache__" in p.parts:
                continue
            try:
                if needle.search(p.read_text(encoding="utf-8")):
                    hits.append(p.relative_to(ROOT).as_posix())
            except (UnicodeDecodeError, OSError):
                pass
    for p in ROOT.glob("*.py"):
        if needle.search(p.read_text(encoding="utf-8")):
            hits.append(p.name)
    assert hits == []


def test_bakeoff_code_does_not_touch_state_memory_config_or_routines_paths():
    for p in (ROOT / "tools" / "bakeoff").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"(?:write_text|jwrite|open\([^)]*[\"']w|rename|replace|copyfile|rmtree)\(([^\n]*)", src):
            assert not re.search(r"""["'](?:state|memory|config|routines|logs|docs|simgold|analysis)[/"']""", m.group(1)), (p.name, m.group(0))
    assert "subprocess" not in (ROOT / "tools" / "bakeoff" / "run_deepseek.py").read_text(encoding="utf-8")


def test_python_311_safe_syntax_and_no_key_printing():
    for p in (ROOT / "tools" / "bakeoff").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        ast.parse(src, feature_version=(3, 11))
        assert not re.search(r"^\s*type\s+\w+\s*=", src, re.M) and "itertools.batched" not in src
        for node in ast.walk(ast.parse(src)):                 # PEP 701: the same quote inside an f-string replacement field
            if isinstance(node, ast.JoinedStr):
                seg = ast.get_source_segment(src, node) or ""
                q = seg[0] if seg and seg[0] in "\"'" else (seg[1] if len(seg) > 1 else "")
                if seg.startswith(("f\"", "f'")):
                    q = seg[1]
                    inner = re.findall(r"\{[^{}]*\}", seg)
                    assert not any(q in i for i in inner), (p.name, seg[:80])
    runner = (ROOT / "tools" / "bakeoff" / "run_deepseek.py").read_text(encoding="utf-8")
    assert not re.search(r"print\([^)]*\bkey\b", runner) and "reasoning_content" in runner
