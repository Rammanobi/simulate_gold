"""Safety net for the v3 routine tools (dormant): they refuse buys, cash, sizes, LIQUIDBEES, unknown symbols and instruction-like text, refuse a
same-day FINAL from 15:05, never touch config/**, and write only routine-owned paths. Whatever they write the engine's own filter accepts."""
from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from v3tools_fix import NOW, RAT, ROOT, SC, TODAY, changed, good_desk, mkroot, tree

import calibration
import desk
import guidance as gd
import memory_ltm
import memory_stm
from simgold import allocation, guard
from simgold.market import IST

# every path a v3 routine tool may create or change (CLAUDE.md: routine-owned, via tools), relative to the repo root
ROUTINE_OWNED = {"state/allocation.json", "state/guidance.json", "state/budget.json", "memory/ltm/calibration.json", "memory/ltm/performance.json",
                 "memory/stm/today.jsonl", f"analysis/{TODAY}/memory.json"} | {f"analysis/{TODAY}/{d}.json" for d in desk.DESKS}
FORBIDDEN_PREFIX = ("config/", "docs/", "simgold/", "tools/", "tests/", ".github/", "routines/", "CLAUDE.md", "run_", "state/portfolio", "logs/")


def g(root, *a, now=NOW):
    return gd.main(["--root", str(root), "--now", now.isoformat(), "allocation", *a])


def test_a_full_routine_day_writes_only_routine_owned_paths(tmp_path, monkeypatch):
    root = mkroot(tmp_path / "r", monkeypatch)
    cfg = hashlib.sha1((ROOT / "config" / "params.yaml").read_bytes()).hexdigest()
    before = tree(root)
    scratch = Path(tmp_path) / "s.json"
    assert desk.main(["--root", str(root), "--now", NOW.isoformat(), "memory", "--write"]) == 0
    for name in ("macro", "gold", "silver", "copper", "risk"):
        scratch.write_text(json.dumps(good_desk(name)))
        assert desk.main(["--root", str(root), "--now", NOW.isoformat(), "write", "--desk", name, "--file", str(scratch)]) == 0, name
    assert g(root, "set", "--phase", "provisional", "--weights", "GOLDBEES=0.80", "SILVERBEES=0.10", "HINDCOPPER=0.10", "--confidence", "0.5", *SC, *RAT) == 0
    late = datetime(2026, 11, 18, 14, 40, tzinfo=IST)
    assert g(root, "set", "--phase", "final", "--weights", "GOLDBEES=0.80", "SILVERBEES=0.10", "HINDCOPPER=0.10", "--confidence", "0.5", *SC, *RAT, now=late) == 0
    assert g(root, "hold-default", "--note", "no usable research today", now=late) == 0
    assert g(root, "show") == 0 and g(root, "explain") == 0
    assert memory_stm.main(["--root", str(root), "--now", NOW.isoformat(), "note", "--who", "allocation", "--text", "SILVERBEES -0.9 percent", "--source", "reuters.com"]) == 0
    assert calibration.main(["--root", str(root), "--now", late.isoformat(), "score", "--write"]) == 0
    assert memory_ltm.main(["--root", str(root), "--now", late.isoformat(), "perf", "--write"]) in (0, 1)
    touched = changed(before, tree(root))
    assert touched <= ROUTINE_OWNED, touched - ROUTINE_OWNED
    assert not [t for t in touched if t.startswith(FORBIDDEN_PREFIX)]
    assert "state/portfolio.json" not in touched and "state/last_prices.json" not in touched
    assert hashlib.sha1((ROOT / "config" / "params.yaml").read_bytes()).hexdigest() == cfg


@pytest.mark.parametrize("mod", ["desk", "alloc_ctx", "calibration"])
def test_the_new_tool_sources_have_no_raw_file_writes_and_never_name_config_paths(mod):
    src = (ROOT / "tools" / f"{mod}.py").read_text(encoding="utf-8")
    assert not re.search(r"\.write_text\(|open\([^)]*['\"][wa]b?['\"]|\.unlink\(|shutil|os\.remove|subprocess", src), mod
    assert "params.yaml" not in src and "/ \"config\"" not in src and "'config'" not in src


def test_guidance_set_runs_the_engines_filter_with_for_write(tmp_path, monkeypatch):
    root = mkroot(tmp_path, monkeypatch)
    calls = []
    real = allocation.sanitize_allocation
    monkeypatch.setattr(allocation, "sanitize_allocation", lambda *a, **k: (calls.append(k), real(*a, **k))[1])
    assert g(root, "set", "--phase", "final", "--weights", "GOLDBEES=0.8", "SILVERBEES=0.1", "HINDCOPPER=0.1", "--confidence", "0.5", *SC, *RAT) == 0
    assert g(root, "hold-default", "--note", "x 1") == 0
    assert len(calls) == 2 and all(c.get("for_write") is True for c in calls)


def test_whatever_the_tool_writes_passes_the_engines_own_filter(tmp_path, monkeypatch):
    """Seeded fuzz: random weights, confidence, times and dates. Exit 2 writes nothing; exit 0 writes a file the engine accepts."""
    root = mkroot(tmp_path, monkeypatch)
    assert desk.main(["--root", str(root), "--now", NOW.isoformat(), "memory", "--write"]) == 0         # cache: no history download per run
    rng = random.Random(20261118)
    f = Path(root) / "state" / "allocation.json"
    n_ok = n_bad = 0
    for _ in range(150):
        if f.exists():
            f.unlink()
        vals = [rng.choice([-0.2, 0.0, 0.05, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0, 1.4, "nan", "inf", "x"]) for _ in range(3)]
        if rng.random() < 0.5:                       # near-valid shares (some a little off 1.00) so both outcomes are well covered
            a, b = sorted(rng.random() for _ in range(2))
            vals = [round(a, 2), round(b - a, 2), round(1 - b + rng.choice([0, 0, 0.01, -0.03, 0.05]), 2)]
        syms = ["GOLDBEES", "SILVERBEES", rng.choice(["HINDCOPPER", "HINDCOPPER", "HINDCOPPER", "LIQUIDBEES", "CASH"])]
        t = NOW.replace(hour=rng.choice([8, 12, 14, 15, 16]), minute=rng.choice([0, 4, 5, 6, 30]))
        phase = rng.choice(["provisional", "final"])
        rc = g(root, "set", "--phase", phase, "--weights", *[f"{s}={v}" for s, v in zip(syms, vals)], "--confidence", str(rng.choice([0, 0.3, 0.9, 1.0, 1.2])),
               *SC, *RAT, now=t)
        if rc == 0:
            n_ok += 1
            obj = json.loads(f.read_text())
            fd = datetime.fromisoformat(obj["for_date"])
            at = max(t, datetime(fd.year, fd.month, fd.day, 10, 0, tzinfo=IST))          # the engine reads it on its for_date
            adv, why = guard.sanitize_allocation(obj, at)
            assert adv is not None, why
            assert set(obj["weights"]) == {"GOLDBEES", "SILVERBEES", "HINDCOPPER"} and abs(sum(obj["weights"].values()) - 1.0) < 1e-9
            assert all(0 <= v <= 1 for v in obj["weights"].values()) and 0 <= obj["confidence"] <= 1
            assert not (phase == "final" and obj["for_date"] == t.date().isoformat() and (t.hour, t.minute) >= (15, 5))
        else:
            n_bad += 1
            assert rc == 2 and not f.exists()
    assert n_ok > 5 and n_bad > 50


@pytest.mark.parametrize("hh,mm,ok", [(14, 59, True), (15, 4, True), (15, 5, False), (15, 29, False), (16, 30, True)])
def test_final_cutoff_edges(tmp_path, monkeypatch, hh, mm, ok):
    root = mkroot(tmp_path, monkeypatch)
    t = NOW.replace(hour=hh, minute=mm)
    extra = ["--for-date", TODAY] if hh == 15 else []
    rc = g(root, "set", "--phase", "final", "--weights", "GOLDBEES=0.8", "SILVERBEES=0.1", "HINDCOPPER=0.1", "--confidence", "0.5", *SC, *RAT, *extra, now=t)
    assert (rc == 0) == ok
    # 16:30: the default for_date has rolled to the next trading day, so a FINAL is a proposal for tomorrow
    if hh == 16:
        assert json.loads((Path(root) / "state" / "allocation.json").read_text())["for_date"] == "2026-11-19"


def test_the_exact_command_shapes_of_the_playbooks_work(tmp_path, monkeypatch, capsys):
    """routines/_common.md and premarket.md (PR 65) call these forms."""
    root = mkroot(tmp_path, monkeypatch)
    rc = g(root, "set", "--phase", "provisional", "--weights", "GOLDBEES=0.72", "SILVERBEES=0.20", "HINDCOPPER=0.08", "--confidence", "0.55",
           "--scenario", "base:0.50:GOLD=flat,SILVER=flat,COPPER=down:DXY 104, no Fed event 18 Nov",
           "--scenario", "bull:0.25:GOLD=up,SILVER=up,COPPER=flat:soft CPI 12 Dec",
           "--scenario", "bear:0.25:GOLD=down,SILVER=down,COPPER=down:US10Y above 4.6",
           "--rationale", "GOLD=DXY 104 real yield 1.9 (investing.com)", "SILVER=ratio 85 (investing.com)", "COPPER=LME stocks 140k t (lme.com)",
           "--invalidate", "close_below:SILVERBEES:150", "close_below:HINDCOPPER:400", "--invalidate", "close_below:GOLDBEES:100", "--horizon-days", "5")
    assert rc == 0
    assert json.loads((Path(root) / "state/allocation.json").read_text())["invalidate"] == [
        "close_below:SILVERBEES:150", "close_below:HINDCOPPER:400", "close_below:GOLDBEES:100"]
    assert g(root, "explain") == 0 and g(root, "show") == 0 and g(root, "hold-default", "--note", "no overnight data") == 0
    capsys.readouterr()
    assert memory_stm.main(["--root", str(root), "--now", NOW.isoformat(), "note", "--who", "desk-GOLD", "--metal", "GOLD", "--text", "COMEX gold +0.6%, DXY -0.2%", "--source", "investing.com"]) == 0


def test_desk_files_cannot_carry_orders_even_when_the_text_looks_like_data(tmp_path, monkeypatch, capsys):
    root = mkroot(tmp_path / "r", monkeypatch)
    scratch = Path(tmp_path) / "s.json"
    for bad_key in ("weights", "cash", "qty", "size", "order", "allocation", "symbol", "bias"):
        scratch.write_text(json.dumps(good_desk("gold", **{bad_key: 1})))
        assert desk.main(["--root", str(root), "--now", NOW.isoformat(), "write", "--desk", "gold", "--file", str(scratch)]) == 2
    assert not (Path(root) / "analysis").exists()
