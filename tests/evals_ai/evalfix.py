"""Layer 1 of the AI evals: deterministic guardrail evals for everything a Claude routine can write.

Every case is an adversarial or wrong routine output (input) and the refusal or neutralisation the code must produce
(expected). No network, no LLM: they run in CI with the rest of `python -m pytest -q`. Every test module in tests/evals_ai
sets `pytestmark = pytest.mark.ai_guardrail` (`python -m pytest -q -m ai_guardrail` runs only these). Layer 2 (LLM behaviour
evals, opt-in, costs Claude usage) is tools/evals/run_routine_evals.py; see docs/AI_EVALS.md.

Shared fixtures for the Layer 1 AI guardrail evals."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from simgold.broker import Portfolio, Position
from simgold.market import IST

ROOT = Path(__file__).resolve().parents[2]

# a quiet Wednesday morning, not in the month-end window, used by the tool cases
NOW = datetime(2026, 10, 14, 11, 0, tzinfo=IST)


def make_root(tmp: Path, positions: dict | None = None, guidance: dict | None = None) -> Path:
    """A minimal repo root for the tools: state/portfolio.json (with the given positions) and optional guidance.json."""
    root = tmp / "repo"
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(exist_ok=True)
    pf = Portfolio()
    pf.cash, pf.liquidbees, pf.floor = 100000.0, 0.0, 75000.0      # v2: the floor is the fixed account stop
    for sym, (qty, px, stop) in (positions or {}).items():
        pf.positions[sym] = Position(qty=qty, avg_cost=px, high_water=px, opened="2026-10-13", stop=stop, last_price=px)
    pf.save(root / "state" / "portfolio.json")
    if guidance is not None:
        (root / "state" / "guidance.json").write_text(json.dumps(guidance), encoding="utf-8")
    return root


def run_tool(tool: str, root: Path, *args, now: datetime = NOW, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run tools/<tool> exactly as a routine does (shipped config/params.yaml), against `root`."""
    e = dict(os.environ)
    e.pop("SIMGOLD_GIT_SYNC", None)
    e.update(env or {})
    return subprocess.run([sys.executable, str(ROOT / "tools" / tool), "--root", str(root), "--now", now.isoformat(), *args],
                          capture_output=True, text=True, env=e, timeout=120)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
