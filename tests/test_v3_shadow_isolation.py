"""While a v3 proposal is only logged (v2_trend, or v3_alloc with shadow true, any ai_trust) it must not change what the engine trades:
the stored minute ticks of 13, 16 and 17 Nov 2026 are replayed through the real tick with and without a proposal file written by the
tool, and the decisions and runtime state are compared byte for byte (only the ALLOC log records may differ). The allocation tool must also
leave state/guidance.json alone in that state; the only bias the engine sees is an explicit routine bias (`guidance.py set`)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from v3tools_fix import RAT, SC, mkroot, tree

import guidance as gd
import v3_replay as R
from simgold.market import IST

DAY1 = datetime(2026, 11, 16, 9, 0, tzinfo=IST)            # a replayed day (Monday)


def tool(root, *args, now=DAY1):
    return gd.main(["--root", str(root), "--now", now.isoformat(), *args])


@pytest.fixture
def proposal(tmp_path, monkeypatch):
    root = mkroot(tmp_path / "tool", monkeypatch)
    before = tree(root)
    assert tool(root, "allocation", "set", "--phase", "provisional", "--weights", "GOLDBEES=0.30", "SILVERBEES=0.50", "HINDCOPPER=0.20",
                "--confidence", "0.9", *SC, *RAT, "--invalidate", "close_below:SILVERBEES:150") == 0
    assert not (Path(root) / "state" / "guidance.json").exists()                  # shipped config is logged-only: no bias written
    return (Path(root) / "state" / "allocation.json").read_text(encoding="utf-8"), root, before


@pytest.mark.parametrize("label,params", [
    ("v2_trend", lambda: R.shipped_params()),
    ("v3 shadow, trust 0", lambda: R.shipped_params("v3_alloc")),
    ("v3 shadow flag, trust 0.5", lambda: R.shipped_params("v3_alloc", ai_trust=0.5)),
])
def test_v2_trades_are_identical_with_and_without_a_proposal_file(tmp_path, proposal, label, params):
    text, _root, _b = proposal
    p = params()
    assert p["allocation"]["shadow"] is True
    days = R.DAYS[:2]                                                            # 13 Nov (entries) and 16 Nov (the proposal's for_date)
    pf0, env0 = R.replay(tmp_path / "without", p, days=days)
    pf1, env1 = R.replay(tmp_path / "with", params(), days=days, state_files={"allocation.json": text})
    assert R.decisions_text(env1, days, drop_actions=("ALLOC",)) == R.decisions_text(env0, days, drop_actions=("ALLOC",)), label
    assert R.runtime_text(pf1, drop_keys=("v3",)) == R.runtime_text(pf0, drop_keys=("v3",)), label
    assert '"BUY"' in R.decisions_text(env0, days)                                # the comparison is about real trades


def test_the_tool_leaves_guidance_json_alone_in_shadow_for_set_and_hold_default(tmp_path, monkeypatch, capsys):
    guidance = {"updated": "2026-11-16T08:00:00+05:30", "bias": {"GOLD": "favour", "SILVER": "block", "COPPER": "neutral"}, "note": "explicit"}
    root = mkroot(tmp_path, monkeypatch, guidance=guidance)
    raw = (Path(root) / "state" / "guidance.json").read_bytes()
    assert tool(root, "allocation", "set", "--phase", "provisional", "--weights", "GOLDBEES=0.30", "SILVERBEES=0.50", "HINDCOPPER=0.20",
                "--confidence", "0.9", *SC, *RAT) == 0
    assert "guidance.json not touched" in capsys.readouterr().out
    assert (Path(root) / "state" / "guidance.json").read_bytes() == raw          # not even `updated` is refreshed
    assert tool(root, "allocation", "hold-default", "--phase", "provisional", "--note", "no overnight data") == 0
    assert (Path(root) / "state" / "guidance.json").read_bytes() == raw


def test_an_explicit_routine_bias_is_the_only_thing_that_moves_v2(tmp_path):
    """Positive control: the comparison above can fail. A fresh `block` bias written by `guidance.py set` changes the v2 trades."""
    day = R.DAYS[:1]
    pf0, env0 = R.replay(tmp_path / "plain", R.shipped_params(), days=day)
    g = json.dumps({"updated": "2026-11-13T15:05:00+05:30", "bias": {"GOLD": "block", "SILVER": "block", "COPPER": "block"}, "note": "explicit"})
    pf1, env1 = R.replay(tmp_path / "blocked", R.shipped_params(), days=day, state_files={"guidance.json": g})
    assert R.decisions_text(env1, days=day) != R.decisions_text(env0, days=day)
