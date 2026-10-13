#!/usr/bin/env python3
"""Watchdog health check. Exit 0 and print "OK ..." when healthy; otherwise exit 4 and print one JSON line
per problem {check, severity, detail}.   python tools/healthcheck.py [--pull] [--repo owner/name]
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from simgold.market import IST, is_trading_day  # noqa: E402

ENGINE_WORKFLOW = "market-hours.yml"
WINDOW = (dtime(9, 10), dtime(15, 40))
TICK_MAX_AGE_MIN = 5
GUIDANCE_MAX_AGE_H = 3
BRIEF_MAX = 8 * 1024
LESSONS_MAX = 60
CONTEXT_MAX = 25 * 1024
LOGS_MAX = 200 * 1024 * 1024
FLOOR_SLACK = 100
LIQUID_MIN = 0.30
REQUIRED = {"portfolio.json": ["cash", "positions"], "guidance.json": [], "fastlane.json": [], "events.json": []}


def problem(check, severity, detail):
    return {"check": check, "severity": severity, "detail": detail}


def real_gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=True).stdout


def in_market_window(now: datetime) -> bool:
    now = now.astimezone(IST)
    return is_trading_day(now.date()) and WINDOW[0] <= now.time() <= WINDOW[1]


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_ts(v, now):
    try:
        t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=IST)
    except ValueError:
        return None


def check_engine(root: Path, now: datetime, gh, repo: str) -> list[dict]:
    out = []
    try:
        data = json.loads(gh(["api", f"repos/{repo}/actions/workflows/{ENGINE_WORKFLOW}/runs?status=in_progress&per_page=5"]))
        if int(data.get("total_count", 0)) < 1:
            out.append(problem("engine_run_missing", "high", "no market-hours workflow run is in progress during NSE hours"))
    except Exception as e:  # noqa: BLE001
        out.append(problem("engine_run_unknown", "low", f"could not query workflow runs: {e}"))
    f = root / "logs" / f"ticks-{now.astimezone(IST).date().isoformat()}.csv"
    if not f.exists():
        out.append(problem("ticks_missing", "high", f"{f.name} does not exist yet today"))
    else:
        last = None
        try:
            rows = [r for r in csv.reader(f.read_text(encoding="utf-8").splitlines()) if r]
            last = _parse_ts(rows[-1][0], now) if rows else None
        except Exception:  # noqa: BLE001
            pass
        if last is None:
            last = datetime.fromtimestamp(f.stat().st_mtime, IST)
        age = (now - last).total_seconds() / 60
        if age > TICK_MAX_AGE_MIN:
            out.append(problem("ticks_stale", "high", f"last tick row is {age:.0f} min old (limit {TICK_MAX_AGE_MIN})"))
    return out


def check_state_files(root: Path) -> tuple[list[dict], dict]:
    out, loaded = [], {}
    for name, keys in REQUIRED.items():
        p = root / "state" / name
        if not p.exists():
            continue
        try:
            d = _load_json(p)
            if not isinstance(d, dict):
                raise ValueError("top level is not an object")
            miss = [k for k in keys if k not in d]
            if miss:
                out.append(problem("state_keys", "high", f"{name} is missing keys {miss}"))
            loaded[name] = d
        except Exception as e:  # noqa: BLE001
            out.append(problem("state_parse", "high", f"{name} does not parse: {e}"))
    return out, loaded


def _prev_floor(root: Path, today: str):
    p = root / "state" / "equity.csv"
    try:
        for r in reversed(list(csv.reader(p.read_text().splitlines()))):
            if len(r) >= 3 and r[0] < today:
                return float(r[2])
    except (OSError, ValueError):
        pass
    try:
        txt = subprocess.run(["git", "show", "HEAD~1:state/portfolio.json"], cwd=root, capture_output=True, text=True, timeout=20).stdout
        return float(json.loads(txt)["floor"])
    except Exception:  # noqa: BLE001
        return None


def check_invariants(root: Path, pf: dict | None, now: datetime) -> list[dict]:
    out = []
    if not pf:
        return out
    cash = pf.get("cash")
    if isinstance(cash, (int, float)) and cash < 0:
        out.append(problem("negative_cash", "high", f"cash is {cash}"))
    for a, p in (pf.get("positions") or {}).items():
        if isinstance(p, dict) and isinstance(p.get("qty"), (int, float)) and p["qty"] < 0:
            out.append(problem("negative_qty", "high", f"{a} quantity is {p['qty']}"))
    value, floor = pf.get("value"), pf.get("floor")
    if isinstance(value, (int, float)) and isinstance(floor, (int, float)):
        if value < floor - FLOOR_SLACK:
            out.append(problem("below_floor", "high", f"value {value:,.0f} is below floor {floor:,.0f} (slack {FLOOR_SLACK})"))
        prev = _prev_floor(root, now.astimezone(IST).date().isoformat())
        if prev is not None and floor < prev - 1e-6:
            out.append(problem("floor_lowered", "high", f"floor {floor:,.0f} is lower than the previous floor {prev:,.0f}"))
    liq = (pf.get("positions") or {}).get("LIQUIDBEES")
    if isinstance(value, (int, float)) and value > 0 and not pf.get("tripwire"):
        lv = liq.get("value") if isinstance(liq, dict) else None
        if isinstance(lv, (int, float)) and lv / value < LIQUID_MIN:
            out.append(problem("liquid_low", "medium", f"LIQUIDBEES is {lv / value:.0%} of value (min {LIQUID_MIN:.0%}) and the tripwire has not fired"))
    return out


def check_memory(root: Path) -> list[dict]:
    out = []
    b = root / "memory" / "brief.md"
    if b.exists() and b.stat().st_size > BRIEF_MAX:
        out.append(problem("memory_brief_size", "medium", f"memory/brief.md is {b.stat().st_size} bytes (max {BRIEF_MAX})"))
    les = root / "memory" / "lessons.md"
    if les.exists():
        n = sum(1 for ln in les.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith(("- ", "* ")))
        if n > LESSONS_MAX:
            out.append(problem("memory_lessons_size", "medium", f"memory/lessons.md has {n} bullets (max {LESSONS_MAX})"))
    ctx = root / "tools" / "context.py"
    if ctx.exists():
        try:
            r = subprocess.run([sys.executable, str(ctx)], cwd=root, capture_output=True, timeout=60)
            if len(r.stdout) > CONTEXT_MAX:
                out.append(problem("context_size", "medium", f"tools/context.py output is {len(r.stdout)} bytes (max {CONTEXT_MAX})"))
        except Exception as e:  # noqa: BLE001
            out.append(problem("context_run", "low", f"tools/context.py failed to run: {e}"))
    logs = root / "logs"
    if logs.exists():
        total = sum(f.stat().st_size for f in logs.rglob("*") if f.is_file())
        if total > LOGS_MAX:
            out.append(problem("logs_size", "medium", f"logs/ is {total // 2**20} MB (max {LOGS_MAX // 2**20})"))
    return out


def check_guidance_age(root: Path, guidance: dict | None, now: datetime) -> list[dict]:
    p = root / "state" / "guidance.json"
    if not p.exists():
        return []
    ts = None
    for k in ("updated_at", "ts", "generated_at"):
        if guidance and guidance.get(k):
            ts = _parse_ts(guidance[k], now)
            break
    ts = ts or datetime.fromtimestamp(p.stat().st_mtime, IST)
    age = (now - ts).total_seconds() / 3600
    return [problem("guidance_stale", "low", f"guidance is {age:.1f} h old (limit {GUIDANCE_MAX_AGE_H} h)")] if age > GUIDANCE_MAX_AGE_H else []


def check_incident_limits(root: Path, now: datetime) -> list[dict]:
    f = root / "logs" / f"incidents-{now.astimezone(IST).date().isoformat()}.jsonl"
    if not f.exists():
        return []
    sent = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(ln).get("notified_at"):
                sent.append(json.loads(ln)["notified_at"])
        except ValueError:
            pass
    sent.sort()
    if len(sent) > 3 or any(b - a < 30 * 60 - 1 for a, b in zip(sent, sent[1:])):
        return [problem("incident_rate_limit", "low", f"{len(sent)} incident posts today exceed the 3/day, 30 min apart limit")]
    return []


def run_checks(root: Path, now: datetime, gh=real_gh, repo: str = "") -> list[dict]:
    probs: list[dict] = []
    state_p, loaded = check_state_files(root)
    probs += state_p
    probs += check_invariants(root, loaded.get("portfolio.json"), now)
    probs += check_memory(root)
    probs += check_incident_limits(root, now)
    if in_market_window(now):
        probs += check_engine(root, now, gh, repo)
        probs += check_guidance_age(root, loaded.get("guidance.json"), now)
    return probs


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--pull" in args:
        subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], cwd=ROOT, capture_output=True)
    repo = os.getenv("GITHUB_REPOSITORY", "Rammanobi/simulate_gold")
    if "--repo" in args:
        repo = args[args.index("--repo") + 1]
    probs = run_checks(ROOT, datetime.now(IST), repo=repo)
    if not probs:
        print(f"OK all checks passed at {datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST")
        return 0
    for p in probs:
        print(json.dumps(p))
    return 4


if __name__ == "__main__":
    sys.exit(main())
