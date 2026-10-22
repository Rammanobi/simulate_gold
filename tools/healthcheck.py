#!/usr/bin/env python3
"""Watchdog health check. Exit 0 and print "OK ..." when healthy; otherwise exit 4 and print one JSON line per problem
{check, severity, category, detail}.     python tools/healthcheck.py [--pull] [--repo owner/name] [--no-write]

Every run (unless --no-write) writes state/health.json {last_run, results[], all_green} and appends one line to
logs/health-DATE.jsonl; a run that finds state/health.json older than watchdog_stale_min (75 min) in market hours reports `watchdog_stale`.
Schemas are the real ones: state/portfolio.json (cash, positions, floor, liquidbees), logs/ticks-DATE.csv
(time,value,floor,cash,liquidbees; time = HH:MM IST), state/guidance.json ("updated"), fastlane.json, events.json.
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
import copy  # noqa: E402

from simgold import alerts, config, trend  # noqa: E402
from simgold.broker import validate_portfolio_dict  # noqa: E402
from simgold.market import IST, is_trading_day  # noqa: E402

ENGINE_WORKFLOW = "market-hours.yml"
WINDOW = (dtime(9, 10), dtime(15, 40))
# The engine itself stops at about 15:31 IST. "no workflow run in progress" is a fault only while the market is open; between 15:31 and
# the end of WINDOW it is normal (raised a false engine_run_missing alert after the close on 22 Oct 2026).
ENGINE_OPEN = (dtime(9, 15), dtime(15, 30))
# ticks reach main with the engine's git sync, every 5 min (run_live --sync-every), so a row 5-7 min old is normal
# on main. 5 raised a false ticks_stale at 15:16 IST on 14 Oct 2026 (row 6 min old) and a duplicate dispatch.
TICK_MAX_AGE_MIN = 12
BRIEF_MAX, LESSONS_MAX, CONTEXT_MAX, LOGS_MAX = 8 * 1024, 60, 25 * 1024, 200 * 1024 * 1024
FLOOR_SLACK = 100
LIQUID_MIN = 0.30


def problem(check: str, category: str, detail: str) -> dict:
    return {"check": check, "severity": alerts.severity_of(category), "category": category, "detail": detail}


def real_gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=True).stdout


def real_git(root: Path, args: list[str]) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=True).stdout


def in_market_window(now: datetime) -> bool:
    now = now.astimezone(IST)
    return is_trading_day(now.date()) and WINDOW[0] <= now.time() <= WINDOW[1]


def engine_should_run(now: datetime) -> bool:
    """True only while NSE is open (09:15-15:30 IST on a trading day)."""
    now = now.astimezone(IST)
    return is_trading_day(now.date()) and ENGINE_OPEN[0] <= now.time() <= ENGINE_OPEN[1]


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_ts(v):
    try:
        t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=IST)
    except ValueError:
        return None


def last_tick(root: Path, now: datetime) -> dict | None:
    """The last row of logs/ticks-TODAY.csv as {time(datetime), value, floor, cash, liquidbees}."""
    f = root / "logs" / f"ticks-{now.astimezone(IST).date().isoformat()}.csv"
    if not f.exists():
        return None
    try:
        rows = list(csv.DictReader(f.read_text(encoding="utf-8").splitlines()))
        r = rows[-1]
        hh, mm = r["time"].strip().split(":")[:2]
        t = now.astimezone(IST).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return {"time": t, "value": float(r["value"]), "floor": float(r["floor"]), "cash": float(r["cash"]),
                "liquidbees": float(r["liquidbees"])}
    except (IndexError, KeyError, ValueError, OSError):
        return {"time": datetime.fromtimestamp(f.stat().st_mtime, IST), "value": None, "floor": None}


def check_engine(root: Path, now: datetime, gh, repo: str) -> list[dict]:
    out = []
    try:
        data = json.loads(gh(["api", f"repos/{repo}/actions/workflows/{ENGINE_WORKFLOW}/runs?status=in_progress&per_page=5"]))
        if int(data.get("total_count", 0)) < 1 and engine_should_run(now):
            out.append(problem("engine_run_missing", "engine_not_running", "no market-hours workflow run is in progress during NSE hours"))
    except Exception as e:  # noqa: BLE001
        out.append(problem("engine_run_unknown", "warning", f"could not query workflow runs: {e}"))
    t = last_tick(root, now)
    if t is None:
        out.append(problem("ticks_missing", "engine_not_running", f"logs/ticks-{now.astimezone(IST).date().isoformat()}.csv does not exist yet today"))
    else:
        age = (now - t["time"]).total_seconds() / 60
        if age > TICK_MAX_AGE_MIN:
            out.append(problem("ticks_stale", "engine_not_running", f"last tick row is {age:.0f} min old (limit {TICK_MAX_AGE_MIN})"))
    return out


def check_state_files(root: Path) -> tuple[list[dict], dict]:
    out, loaded = [], {}
    spec = {"portfolio.json": ("cash", "positions", "floor", "liquidbees"), "guidance.json": ("updated",),
            "fastlane.json": (), "events.json": (), "alerts.json": (), "safe_mode.json": ()}
    for name, keys in spec.items():
        p = root / "state" / name
        if not p.exists():
            continue
        try:
            d = _load_json(p)
            if name == "events.json":
                if not isinstance(d, (list, dict)):
                    raise ValueError("expected a list (or {events: [...]})")
            elif not isinstance(d, dict):
                raise ValueError("top level is not an object")
            miss = [k for k in keys if k not in d]
            if miss:
                out.append(problem("state_keys", "state_corrupt", f"{name} is missing keys {miss}"))
            elif name == "portfolio.json":
                try:
                    validate_portfolio_dict(copy.deepcopy(d))
                except ValueError as e:
                    out.append(problem("state_values", "state_corrupt", f"portfolio.json: {e}"))
            loaded[name] = d
        except Exception as e:  # noqa: BLE001
            out.append(problem("state_parse", "state_corrupt", f"{name} does not parse: {e}"))
    return out, loaded


def prev_floor(root: Path, today: str, git=real_git):
    """The floor in the newest state/portfolio.json commit made before today (git history)."""
    try:
        log = git(root, ["log", "-n", "40", "--format=%H %cI", "--", "state/portfolio.json"]).splitlines()
        for ln in log:
            sha, iso = ln.split(" ", 1)
            when = _parse_ts(iso)
            if when and when.astimezone(IST).date().isoformat() < today:
                return float(json.loads(git(root, ["show", f"{sha}:state/portfolio.json"]))["floor"])
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_v2() -> bool:
    """Strategy v2 (trend core): the "floor" in portfolio.json is the fixed hard account stop, and LIQUIDBEES is whatever is not
    invested, so the floor-never-lowered and the 30 percent LIQUIDBEES checks (Green Guard rules) do not apply."""
    try:
        return trend.is_v2(config.load_params())
    except Exception:  # noqa: BLE001
        return False


def check_invariants(root: Path, pf: dict | None, now: datetime, git=real_git, cfg: dict | None = None) -> list[dict]:
    out = []
    if not pf:
        return out
    cfg = cfg or alerts.params()
    cash, lb = pf.get("cash"), pf.get("liquidbees")
    positions = pf.get("positions") if isinstance(pf.get("positions"), dict) else {}
    for name, v in (("cash", cash), ("liquidbees", lb)):
        if isinstance(v, (int, float)) and v < 0:
            out.append(problem(f"negative_{name}", "state_corrupt", f"{name} is {v}"))
    for a, p in positions.items():
        if isinstance(p, dict) and isinstance(p.get("qty"), (int, float)) and p["qty"] < 0:
            out.append(problem("negative_qty", "state_corrupt", f"{a} quantity is {p['qty']}"))
    t = last_tick(root, now)
    value = t["value"] if t and t.get("value") is not None else None
    if value is None and isinstance(cash, (int, float)) and isinstance(lb, (int, float)):
        value = cash + lb + sum((p.get("qty", 0) * (p.get("last_price") or p.get("avg_cost") or 0))
                                for p in positions.values() if isinstance(p, dict))
    floor = pf.get("floor")
    if isinstance(value, (int, float)) and isinstance(floor, (int, float)):
        if value < floor - FLOOR_SLACK:
            out.append(problem("below_floor", "floor_broken", f"value {value:,.0f} is more than {FLOOR_SLACK} below the floor {floor:,.0f}"))
        elif value <= floor + cfg["near_floor_inr"]:
            out.append(problem("near_floor", "near_floor", f"value {value:,.0f} is within {cfg['near_floor_inr']} of the floor {floor:,.0f}"))
        v2 = _is_v2()
        prev = None if v2 else prev_floor(root, now.astimezone(IST).date().isoformat(), git)
        if prev is not None and floor < prev - 1e-6:
            out.append(problem("floor_lowered", "floor_broken", f"floor {floor:,.0f} is lower than the previous day's floor {prev:,.0f}"))
        cd = pf.get("cooldowns") if isinstance(pf.get("cooldowns"), dict) else {}
        tripped = cd.get("no_buys_until")
        if not v2 and not config.idle_is_cash() and value > 0 and isinstance(lb, (int, float)) and not tripped and lb / value < LIQUID_MIN - 0.005 and in_market_window(now) \
                and positions:
            out.append(problem("liquid_low", "invariant_warning",
                               f"LIQUIDBEES is {lb / value:.0%} of value (min {LIQUID_MIN:.0%}) and the tripwire has not fired"))
    return out


def check_memory(root: Path) -> list[dict]:
    out = []
    b = root / "memory" / "brief.md"
    if b.exists() and b.stat().st_size > BRIEF_MAX:
        out.append(problem("memory_brief_size", "memory_cap", f"memory/brief.md is {b.stat().st_size} bytes (max {BRIEF_MAX})"))
    les = root / "memory" / "lessons.md"
    if les.exists():
        n = sum(1 for ln in les.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith(("- ", "* ")))
        if n > LESSONS_MAX:
            out.append(problem("memory_lessons_size", "memory_cap", f"memory/lessons.md has {n} bullets (max {LESSONS_MAX})"))
    ctx = root / "tools" / "context.py"
    if ctx.exists():
        try:
            r = subprocess.run([sys.executable, str(ctx)], cwd=root, capture_output=True, timeout=60)
            if len(r.stdout) > CONTEXT_MAX:
                out.append(problem("context_size", "memory_cap", f"tools/context.py output is {len(r.stdout)} bytes (max {CONTEXT_MAX})"))
        except Exception as e:  # noqa: BLE001
            out.append(problem("context_run", "warning", f"tools/context.py failed to run: {e}"))
    logs = root / "logs"
    if logs.exists():
        total = sum(f.stat().st_size for f in logs.rglob("*") if f.is_file())
        if total > LOGS_MAX:
            out.append(problem("logs_size", "memory_cap", f"logs/ is {total // 2**20} MB (max {LOGS_MAX // 2**20})"))
    return out


def check_calendar(now: datetime) -> list[dict]:
    """The NSE holiday list is typed in per year (simgold/market.py). Without next year's list, holidays look like
    trading days: month-end protection and the last-3-days window would count phantom sessions."""
    from simgold import market as _m
    d = now.astimezone(IST).date()
    years = [d.year] + ([d.year + 1] if d.month == 12 else [])
    miss = [y for y in years if not _m.holidays_known(y)]
    if miss:
        return [problem("holiday_calendar_missing", "invariant_warning",
                        f"simgold/market.py has no NSE holiday list for {', '.join(map(str, miss))}; add the official "
                        f"NSE circular dates (month-end and last-3-days rules count holidays as trading days without it)")]
    return []


def check_guidance_age(root: Path, guidance: dict | None, now: datetime, cfg: dict | None = None) -> list[dict]:
    p = root / "state" / "guidance.json"
    if not p.exists():
        return []
    cfg = cfg or alerts.params()
    ts = _parse_ts(guidance.get("updated")) if guidance and guidance.get("updated") else None
    ts = ts or datetime.fromtimestamp(p.stat().st_mtime, IST)
    age = (now - ts).total_seconds() / 3600
    lim = cfg["stale_guidance_hours"]
    return [problem("guidance_stale", "stale_guidance", f"guidance is {age:.1f} h old (limit {lim} h)")] if age > lim else []


SILENT_AFTER = dtime(11, 30)
REPORT_DUE = dtime(16, 30)
SILENT_GUIDANCE_HOURS = 3


def check_routines_silent(root: Path, guidance: dict | None, now: datetime) -> list[dict]:
    """The Claude routines (usage limit or credits used up?) have stopped. Free, no AI:
    (a) trading day, 11:30-15:40 IST, a guidance.json exists and was last updated over 3 h ago; or
    (b) trading day, after 16:30 IST, and there is no Claude-written reports/daily/<today>.json
        (a report with "fallback": true is the DeepSeek fallback's, so Claude is still silent)."""
    now = now.astimezone(IST)
    if not is_trading_day(now.date()):
        return []
    t = now.time()
    if SILENT_AFTER <= t <= WINDOW[1]:
        p = root / "state" / "guidance.json"
        if p.exists():
            ts = _parse_ts(guidance.get("updated")) if isinstance(guidance, dict) and guidance.get("updated") else None
            ts = ts or datetime.fromtimestamp(p.stat().st_mtime, IST)
            age = (now - ts).total_seconds() / 3600
            by_fallback = isinstance(guidance, dict) and str(guidance.get("note", "")).startswith("[fallback")
            if age > SILENT_GUIDANCE_HOURS or by_fallback:     # a fallback-written guidance is not Claude's
                return [problem("routines_silent", "routines_silent",
                                f"guidance.json was last written {'by the fallback' if by_fallback else f'{age:.1f} h ago'} "
                                f"(limit {SILENT_GUIDANCE_HOURS} h): the Claude routines are not running (usage limit or credits?)")]
    if t >= REPORT_DUE:
        rep = root / "reports" / "daily" / f"{now.date().isoformat()}.json"
        claude_wrote = False
        if rep.exists():
            try:
                d = _load_json(rep)
                claude_wrote = not (isinstance(d, dict) and d.get("fallback"))
            except (OSError, ValueError):
                claude_wrote = False
        if not claude_wrote:
            return [problem("routines_silent", "routines_silent",
                            f"no daily report reports/daily/{now.date().isoformat()}.json after {REPORT_DUE.strftime('%H:%M')} IST: "
                            f"the Claude routines are not running (usage limit or credits?)")]
    return []


def check_incident_limits(root: Path, now: datetime, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or alerts.params()
    st = alerts.load_state(root / "state")
    n = alerts.fires_today(st, now.timestamp())
    if n > cfg["max_fires_per_day"]:
        return [problem("incident_budget", "warning", f"{n} routine fires today exceed the budget of {cfg['max_fires_per_day']}")]
    return []


def check_watchdog(root: Path, now: datetime, cfg: dict | None = None) -> list[dict]:
    """Dead-man's switch: state/health.json (written by every run) must not be older than watchdog_stale_min in market hours."""
    cfg = cfg or alerts.params()
    p = root / "state" / "health.json"
    try:
        last = _parse_ts(_load_json(p)["last_run"])
    except FileNotFoundError:
        return []           # first ever run: nothing to compare with yet
    except (OSError, ValueError, KeyError, TypeError):
        return [problem("watchdog_stale", "watchdog_stale", "state/health.json is unreadable during market hours")]
    age = (now - last).total_seconds() / 60 if last else 9999
    if age > cfg["watchdog_stale_min"]:
        return [problem("watchdog_stale", "watchdog_stale", f"the last healthcheck ran {age:.0f} min ago (limit {cfg['watchdog_stale_min']})")]
    return []


def _guarded(name: str, fn, *args) -> list[dict]:
    """A check that crashes is itself a problem (MEDIUM), never a silent exit 1 that the workflow would not alert on."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return [problem(f"{name}_crashed", "workflow_failed", f"the {name} check crashed: {type(e).__name__}: {e}")]


def run_checks(root: Path, now: datetime, gh=real_gh, repo: str = "", git=real_git, cfg: dict | None = None) -> list[dict]:
    cfg = cfg or alerts.params()
    probs: list[dict] = []
    try:
        state_p, loaded = check_state_files(root)
    except Exception as e:  # noqa: BLE001
        state_p, loaded = [problem("state_files_crashed", "workflow_failed", f"{type(e).__name__}: {e}")], {}
    probs += state_p
    probs += _guarded("invariants", check_invariants, root, loaded.get("portfolio.json"), now, git, cfg)
    probs += _guarded("memory", check_memory, root)
    probs += _guarded("incident_limits", check_incident_limits, root, now, cfg)
    probs += _guarded("calendar", check_calendar, now)
    probs += _guarded("routines_silent", check_routines_silent, root, loaded.get("guidance.json"), now)
    if in_market_window(now):
        probs += _guarded("watchdog", check_watchdog, root, now, cfg)
        probs += _guarded("engine", check_engine, root, now, gh, repo)
        probs += _guarded("guidance_age", check_guidance_age, root, loaded.get("guidance.json"), now, cfg)
    return probs


def write_health(root: Path, now: datetime, probs: list[dict]) -> None:
    """state/health.json (latest run) and logs/health-DATE.jsonl (one line per run, used by the daily email)."""
    rec = {"last_run": now.isoformat(timespec="seconds"), "results": probs, "all_green": not probs}
    try:
        alerts.atomic_write(root / "state" / "health.json", json.dumps(rec, indent=1))
        (root / "logs").mkdir(parents=True, exist_ok=True)
        with open(root / "logs" / f"health-{now.astimezone(IST).date().isoformat()}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--pull" in args:
        subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], cwd=ROOT, capture_output=True)
    repo = os.getenv("GITHUB_REPOSITORY", "Rammanobi/simulate_gold")
    if "--repo" in args:
        repo = args[args.index("--repo") + 1]
    now = datetime.now(IST)
    probs = run_checks(ROOT, now, repo=repo)
    if "--no-write" not in args:
        write_health(ROOT, now, probs)
        if not probs:       # a clean pre-market run is what lets safe mode clear itself the next day
            try:
                alerts.auto_clear_safe_mode(ROOT / "state", now.timestamp(), True)
            except Exception:  # noqa: BLE001 - fail closed: safe mode simply stays on
                pass
    if not probs:
        print(f"OK all checks passed at {now.strftime('%Y-%m-%d %H:%M')} IST")
        return 0
    for p in probs:
        print(json.dumps(p))
    return 4


if __name__ == "__main__":
    sys.exit(main())
