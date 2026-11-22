"""Fixtures for the v3 tool tests (tools/guidance.py allocation, tools/desk.py, tools/calibration.py, context views). No network."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alloc_ctx  # noqa: E402
from memfix import seed_memory, series  # noqa: E402

from simgold.market import IST  # noqa: E402

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=IST)          # a Wednesday, NSE open
TODAY = NOW.date().isoformat()
SC = ["--scenario", "base:0.50:GOLD=flat,SILVER=flat,COPPER=down:DXY 104, no Fed event",
      "--scenario", "bull:0.25:GOLD=up,SILVER=up,COPPER=flat:soft CPI 12 Dec",
      "--scenario", "bear:0.25:GOLD=down,SILVER=down,COPPER=down:US10Y above 4.6 percent"]
RAT = ["--rationale", "GOLD=DXY 104, real yield 1.9 (investing.com)", "SILVER=gold/silver ratio 85 (investing.com)",
       "COPPER=LME stocks 140k t (lme.com)"]


def fake_daily(tickers, period):
    cols = {}
    for tk, kind, base, seed in (("GOLDBEES.NS", "up", 100, 3), ("SILVERBEES.NS", "up", 150, 4), ("HINDCOPPER.NS", "down", 400, 5)):
        cols[tk] = series(kind, n=800, end="2026-11-17", seed=seed, base=base)
    c = pd.DataFrame(cols)
    return {"close": c, "high": c * 1.005, "low": c * 0.995}


def no_network(tickers, period):
    raise OSError("no network in tests")


def mkroot(tmp_path, monkeypatch, fetch=fake_daily, level=0, positions=None, guidance=None):
    root = Path(tmp_path)
    (root / "state").mkdir(parents=True, exist_ok=True)
    pos = positions if positions is not None else {"GOLDBEES": {"qty": 437, "avg_cost": 113.0, "last_price": 107.91}}
    (root / "state" / "portfolio.json").write_text(json.dumps({
        "cash": 50000.0, "positions": pos, "peak_equity": 100000.0, "cooldowns": {},
        "runtime": {"v2": {"brake": {"level": level, "peak": 101197.5, "reconfirm": []}}}}))
    (root / "state" / "last_prices.json").write_text(json.dumps({
        "GOLDBEES": {"price": 107.91}, "SILVERBEES": {"price": 155.86}, "HINDCOPPER": {"price": 419.35}}))
    (root / "state" / "budget.json").write_text(json.dumps({"mode": "THRIFT"}))
    if guidance is not None:
        (root / "state" / "guidance.json").write_text(json.dumps(guidance))
    seed_memory(root, ltm="UP", stm="UP", asof="2026-11-17")
    monkeypatch.setattr(alloc_ctx, "fetch_daily", fetch)
    return root


def go_live(monkeypatch, **alloc):
    """Make every tool see strategy.mode v3_alloc with the AI tilt trading (shadow false, phase live, ai_trust 0.5)."""
    import copy

    from simgold import config
    p = copy.deepcopy(config.load_params())
    p["strategy"]["mode"] = "v3_alloc"
    p["allocation"].update(dict({"shadow": False, "phase": "live", "ai_trust": 0.5}, **alloc))
    monkeypatch.setattr(config, "load_params", lambda path=None: p)
    return p


def tree(root: Path) -> dict[str, str]:
    """{relative path: sha1} of every file under root (to prove what a tool did and did not write)."""
    return {str(f.relative_to(root)): hashlib.sha1(f.read_bytes()).hexdigest() for f in sorted(Path(root).rglob("*")) if f.is_file()}


def changed(before: dict, after: dict) -> set[str]:
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}


def good_desk(desk="gold", day=TODAY, **kw):
    metal = {"gold": "GOLD", "silver": "SILVER", "copper": "COPPER"}.get(desk, "GOLD")
    d = {"desk": desk, "date": day, "written": "2026-11-18T08:25:00+05:30", "model": "sonnet",
         "facts": [{"metric": "COMEX gold", "value": 4012.5, "unit": "USD", "source": "investing.com", "asof": "2026-11-18 08:20"},
                   {"metric": "DXY", "value": 104.2, "unit": "index", "source": "marketwatch.com", "asof": "2026-11-18 08:20"}],
         "view": {metal: {"dir_5d": "up", "p_up": 0.5, "p_down": 0.2, "score": 1, "why": "DXY 104.2 easing, COMEX +0.6 percent"}},
         "risks": ["US CPI 12 Dec at 8.30 ET could reverse the dollar move"], "events": [{"date": "2026-12-12", "type": "CPI", "confirmed": True}],
         "confidence": 0.55, "abstain": False}
    d.update(kw)
    return d
