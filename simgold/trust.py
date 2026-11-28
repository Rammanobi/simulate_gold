"""Trust recommendation for the AI allocation tilt (strategy v3; paper trading only; code only, read-only).

It READS the calibration ledger (memory/ltm/calibration.json) and the ALLOC records of logs/decisions-D.jsonl and RECOMMENDS a value of
`allocation.ai_trust` from {0, 0.25, 0.5, 0.75, 1.0} with the reasons. It never writes a file and never applies anything: the owner
commits `allocation.ai_trust` to config/params.yaml. Nothing here can change what the engine trades.

Evidence (all numbers are plain arithmetic on the ledger; the resampling is seeded, so the same ledger gives the same text):
  (a) Brier skill against a base-rate forecaster. A scored call is a (date, metal) 5-day outcome up/flat/down with a Brier score
      B = ((p_up - 1[up])^2 + (p_down - 1[down])^2) / 2. The base-rate forecaster always says p_up, p_down = the share of up and down
      outcomes of that metal in the ledger (in-sample, which flatters the baseline, so the test is conservative). Per date we average
      the model's B and the baseline's B over that date's observations; skill = 1 - sum(model B) / sum(baseline B). Groups: each
      desk, `synthesis` (the proposal's scenario set) and `overall` (all of them pooled per date). The interval is a moving-block
      bootstrap over dates (block length min(5, n // 5), at least 1; 2000 resamples; 2.5 and 97.5 percent; seed 20261128).
  (b) Paired AI-vs-default: per scored day pp = (what the full-trust tilt earned) - (what the default earned), percentage points of
      the book. mean, sample std, t = mean / (std / sqrt(n)), the same block bootstrap for the mean.
  (c) Validity rate = days whose proposal the engine accepted / days with a proposal it read at all (ALLOC clamp rule
      invalid_proposal counts against; no_proposal and stale_proposal do not). Fact-check mismatch rate = share of ledger rows
      (proposals and desk calls) whose `fc` field says a stated number did not match the data.
Table (config `trust:` may override; defaults here). A level needs ALL of its row; the recommendation is the highest level whose row passes:

  level   scored days   P&L mean CI low   Brier skill (synthesis or overall)        validity   mismatch rate
  0.25    >= 20         > 0               > 0                                       >= 95%     <= 5%
  0.50    >= 40         > 0               > 0 and CI low > 0                        >= 95%     <= 5%
  0.75    >= 60         > 0               > 0 and CI low > 0                        >= 95%     <= 5%
  1.00    >= 120        > 0               > 0 and CI low > 0                        >= 95%     <= 5%

Unknown evidence (no ALLOC records, no `fc` field yet) is NOT counted as passing. Step-down: the tilt lost more than 2 pp in the latest or the
previous calendar month with data, or there were 5 or more invalid proposals in the last 31 days -> recommend 0 whatever else is true.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from simgold import memstore

LEVELS = (0.0, 0.25, 0.5, 0.75, 1.0)
DEFAULTS = {"min_days": [20, 40, 60, 120], "min_validity": 0.95, "max_mismatch": 0.05,
            "step_down_month_pp": -2.0, "step_down_breaches": 5, "bootstrap": 2000, "seed": 20261128}
METALS = ("GOLD", "SILVER", "COPPER")
DESKS = ("macro", "gold", "silver", "copper", "risk")


def settings(p: dict | None = None) -> dict:
    """Thresholds: the `trust:` block of the params over DEFAULTS; a garbage value falls back to its default."""
    raw = (p or {}).get("trust") if isinstance(p, dict) else None
    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    md = raw.get("min_days")
    if isinstance(md, list) and len(md) == 4 and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in md) and md == sorted(md):
        out["min_days"] = list(md)
    for k, lo, hi in (("min_validity", 0.0, 1.0), ("max_mismatch", 0.0, 1.0), ("step_down_month_pp", -100.0, 0.0)):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            out[k] = float(v)
    v = raw.get("step_down_breaches")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        out["step_down_breaches"] = v
    return out


# ------------------------------------------------------------------ maths
def _mean(x):
    return sum(x) / len(x)


def _std(x):
    n = len(x)
    if n < 2:
        return None
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))


def block_len(n: int) -> int:
    return max(1, min(5, n // 5))


def bootstrap(series: list[tuple], stat, B: int, seed: int):
    """Moving-block bootstrap of a statistic of a time-ordered list of tuples; (lo, hi) at 2.5 / 97.5 percent, or None when n < 3."""
    n = len(series)
    if n < 3 or B < 1:
        return None
    L = block_len(n)
    rng = random.Random(seed)
    out = []
    for _ in range(B):
        s: list = []
        while len(s) < n:
            i = rng.randrange(0, n - L + 1)
            s.extend(series[i:i + L])
        v = stat(s[:n])
        if v is not None:
            out.append(v)
    if not out:
        return None
    out.sort()
    return out[int(0.025 * (len(out) - 1))], out[int(round(0.975 * (len(out) - 1)))]


def paired(pp: list[float], B: int, seed: int) -> dict:
    n = len(pp)
    sd = _std(pp)
    mean = _mean(pp) if n else None
    ci = bootstrap([(v,) for v in pp], lambda s: _mean([x[0] for x in s]), B, seed) if n else None
    return {"n": n, "mean_pp": None if mean is None else round(mean, 4), "std_pp": None if sd is None else round(sd, 4),
            "t": None if not sd or n < 2 else round(mean / (sd / math.sqrt(n)), 3),
            "ci95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)]}


def _brier(pu: float, pd: float, o: str) -> float:
    return ((pu - (o == "u")) ** 2 + (pd - (o == "d")) ** 2) / 2


def skill(rows: list[tuple[str, float, float]], B: int, seed: int) -> dict:
    """rows: (date, model Brier, baseline Brier) per observation. Averaged per date, then skill = 1 - sum(model) / sum(baseline)."""
    by: dict[str, list] = {}
    for d, m, b in rows:
        by.setdefault(d, []).append((m, b))
    ser = [(_mean([x[0] for x in v]), _mean([x[1] for x in v])) for _, v in sorted(by.items())]
    f = lambda s: (None if sum(x[1] for x in s) <= 0 else 1 - sum(x[0] for x in s) / sum(x[1] for x in s))        # noqa: E731
    sk = f(ser) if ser else None
    ci = bootstrap(ser, f, B, seed) if ser else None
    return {"n": len(ser), "n_obs": len(rows), "skill": None if sk is None else round(sk, 4),
            "ci95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)],
            "brier": None if not ser else round(_mean([x[0] for x in ser]), 4), "base": None if not ser else round(_mean([x[1] for x in ser]), 4)}


# ------------------------------------------------------------------ reading (never writes)
def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _isday(x) -> bool:
    try:
        date.fromisoformat(x)
        return isinstance(x, str) and len(x) == 10
    except (TypeError, ValueError):
        return False


def read_ledger(root) -> dict | None:
    f = memstore.ltm_path(root, "calibration.json")
    if not Path(f).exists():
        return None
    try:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def alloc_validity(root, since: str, until: str) -> dict:
    """From logs/decisions-D.jsonl ALLOC records (last of each day) in [since, until]: days with a proposal read, days invalid."""
    valid = invalid = 0
    inv_days: list[str] = []
    try:
        files = sorted(p.name[10:20] for p in (Path(root) / "logs").glob("decisions-????-??-??.jsonl"))
    except OSError:
        files = []
    for iso in [x for x in files if since <= x <= until]:
        last = None
        try:
            for ln in (Path(root) / "logs" / f"decisions-{iso}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
                if '"ALLOC"' in ln:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("action") == "ALLOC" and isinstance(r.get("alloc"), dict):
                        last = r["alloc"]
        except OSError:
            continue
        if last is None:
            continue
        rules = {c.get("rule") for c in last.get("clamps") or [] if isinstance(c, dict)}
        if "invalid_proposal" in rules:
            invalid += 1
            inv_days.append(iso)
        elif last.get("advice") in ("final", "provisional", "carry", "hold_default") and "stale_proposal" not in rules:
            valid += 1
    n = valid + invalid
    return {"n": n, "invalid": invalid, "rate": None if n == 0 else round(valid / n, 4), "invalid_days": inv_days}


def factcheck_rate(led: dict, until: str) -> dict:
    n = bad = 0
    for k in ("sets", "calls"):
        for r in led.get(k) or []:
            fc = r.get("fc") if isinstance(r, dict) else None
            if isinstance(fc, list) and len(fc) == 2 and _isnum(fc[1]) and _isday(r.get("d")) and r["d"] <= until:
                n += 1
                bad += 0 if fc[0] == 1 else 1
    return {"n": n, "bad": bad, "rate": None if n == 0 else round(bad / n, 4)}


def _week_end(week: str) -> date:
    try:
        y, w = week.upper().split("-W")
        return date.fromisocalendar(int(y), int(w), 5)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"--week must look like 2026-W47, got {str(week)[:20]!r}")


# ------------------------------------------------------------------ the recommendation
def compute(root, now: datetime | None = None, week: str | None = None, params: dict | None = None,
            validity: dict | None = None) -> dict:
    """Never raises (a ledger of the wrong shape reads as insufficient data); a bad `week` is a ValueError, as in tools/calibration.py."""
    if week:
        _week_end(week)
    try:
        return _compute(root, now, week, params, validity)
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, OverflowError, RecursionError):
        return {"asof": None, "week": week, "recommended": 0.0, "table": [], "insufficient": True,
                "reasons": ["insufficient data, recommend 0: the calibration ledger could not be read"]}


def _compute(root, now, week, params, validity) -> dict:
    """The full evidence and recommendation as a dict. `validity` may be given (tests); else it is read from the ALLOC records.
    Read-only; never raises on a damaged ledger (it reads as 'insufficient data')."""
    if params is None:
        try:
            from simgold import config
            params = config.load_params()
        except Exception:                                      # noqa: BLE001
            params = {}
    cfg = settings(params)
    until_d = _week_end(week) if week else ((now or datetime.now()).date())
    until = until_d.isoformat()
    led = read_ledger(root)
    out = {"asof": until, "week": week.upper() if week else None, "recommended": 0.0, "reasons": [], "table": [], "config": cfg}
    if led is None:
        out["reasons"].append("insufficient data, recommend 0: no calibration ledger")
        out["insufficient"] = True
        return out
    B, seed = int(cfg["bootstrap"]), int(cfg["seed"])
    pairs = sorted(((r["d"], float(r["pp"])) for r in (led.get("pairs") or [])
                    if isinstance(r, dict) and _isday(r.get("d")) and _isnum(r.get("pp")) and r["d"] <= until), key=lambda x: x[0])
    oc = {d: v for d, v in (led.get("oc") or {}).items() if _isday(d) and isinstance(v, str) and len(v) == 3 and d <= until}
    # base rates per metal from the stored outcomes
    base = {}
    for j, m in enumerate(METALS):
        o = [v[j] for v in oc.values() if v[j] in "ufd"]
        base[m] = (sum(1 for x in o if x == "u") / len(o), sum(1 for x in o if x == "d") / len(o)) if o else None

    def bb(d: str, m: str):
        j = METALS.index(m)
        b = base.get(m)
        o = oc.get(d, "---")[j]
        return None if b is None or o not in "ufd" else _brier(b[0], b[1], o)

    groups: dict[str, list] = {k: [] for k in ("synthesis",) + DESKS}
    sc = led.get("scores") if isinstance(led.get("scores"), dict) else {}
    for r in sc.get("synthesis") or []:
        if isinstance(r, list) and len(r) == 2 and _isday(r[0]) and _isnum(r[1]) and r[0] <= until:
            parts = [bb(r[0], m) for m in METALS]
            if all(x is not None for x in parts):
                groups["synthesis"].append((r[0], float(r[1]), _mean(parts)))
    for k in DESKS:
        for r in sc.get(k) or []:
            if isinstance(r, list) and len(r) == 3 and _isday(r[0]) and r[1] in METALS and _isnum(r[2]) and r[0] <= until:
                b = bb(r[0], r[1])
                if b is not None:
                    groups[k].append((r[0], float(r[2]), b))
    out["brier_skill"] = {k: skill(v, B, seed) for k, v in groups.items()}
    out["brier_skill"]["overall"] = skill([x for v in groups.values() for x in v], B, seed)
    out["paired"] = paired([v for _, v in pairs], B, seed)
    since = (until_d - timedelta(days=92)).isoformat()
    out["validity"] = validity if validity is not None else alloc_validity(root, since, until)
    out["factcheck"] = factcheck_rate(led, until)
    n_days = out["paired"]["n"]
    if n_days == 0 and not any(g["n"] for g in out["brier_skill"].values()):
        out["reasons"].append("insufficient data, recommend 0: the ledger has no scored days yet")
        out["insufficient"] = True
        return out
    # ---- step-down
    months: dict[str, float] = {}
    for d, v in pairs:
        months[d[:7]] = months.get(d[:7], 0.0) + v
    last2 = sorted(months)[-2:]
    out["by_month_pp"] = {m: round(months[m], 3) for m in sorted(months)[-3:]}
    down = [m for m in last2 if months[m] < cfg["step_down_month_pp"]]
    cut31 = (until_d - timedelta(days=31)).isoformat()
    breaches = [d for d in (out["validity"].get("invalid_days") or []) if cut31 <= d <= until]
    out["breaches_31d"] = len(breaches) if out["validity"].get("invalid_days") is not None else out["validity"].get("invalid", 0)
    step = []
    if down:
        step.append(f"STEP DOWN: the tilt lost {', '.join(f'{months[m]:+.2f} pp in {m}' for m in down)} (limit {cfg['step_down_month_pp']:+g} pp)")
    if out["breaches_31d"] >= cfg["step_down_breaches"]:
        step.append(f"STEP DOWN: {out['breaches_31d']} rule breaches (invalid proposals) in 31 days (limit {cfg['step_down_breaches']})")
    # ---- the table
    pc, bs = out["paired"], out["brier_skill"]
    sk = bs["synthesis"] if bs["synthesis"]["n"] else bs["overall"]
    out["brier_basis"] = "synthesis" if bs["synthesis"]["n"] else "overall"
    val, mis = out["validity"].get("rate"), out["factcheck"]["rate"]
    best = 0.0
    for lv, need in zip(LEVELS[1:], cfg["min_days"]):
        chk = [("scored days >= %d" % need, n_days >= need, f"n={n_days}"),
               ("P&L CI low > 0", bool(pc["ci95"]) and pc["ci95"][0] > 0, f"CI {pc['ci95']}"),
               ("Brier skill > 0", sk["skill"] is not None and sk["skill"] > 0, f"skill {sk['skill']}"),
               ("Brier skill CI low > 0 (from 0.5)", lv < 0.5 or bool(sk["ci95"]) and sk["ci95"][0] > 0, f"CI {sk['ci95']}"),
               ("validity >= %g%%" % (cfg["min_validity"] * 100), val is not None and val >= cfg["min_validity"], f"{val}"),
               ("fact-check mismatch <= %g%%" % (cfg["max_mismatch"] * 100), mis is not None and mis <= cfg["max_mismatch"], f"{mis}")]
        out["table"].append({"level": lv, "pass": all(c[1] for c in chk), "checks": [{"test": t, "ok": ok, "data": d} for t, ok, d in chk]})
        if all(c[1] for c in chk):
            best = lv
    if step:
        out["recommended"] = 0.0
        out["reasons"] += step + [f"(without the step-down the table would give {best:g})"]
    else:
        out["recommended"] = best
        if best < 1.0:
            nxt = next(t for t in out["table"] if t["level"] == (best + 0.25 if best else 0.25))
            out["reasons"].append(f"{nxt['level']:g} not reached: " + "; ".join(f"{c['test']} ({c['data']})" for c in nxt["checks"] if not c["ok"]))
        else:
            out["reasons"].append("every row of the table passes")
    if n_days < 20:
        out["power_note"] = f"{n_days} days too few to rank (need at least {cfg['min_days'][0]} scored days before any trust)"
    elif n_days < cfg["min_days"][-1]:
        out["power_note"] = f"{n_days} days is a thin sample: the intervals are wide, read this as a first look, not a ranking"
    else:
        out["power_note"] = None
    return out


def _f(x, nd=2, sign=False):
    return "n/a" if x is None else format(x, f"{'+' if sign else ''}.{nd}f")


def summary_line(t: dict) -> str:
    """One line for the weekly outputs."""
    if t.get("insufficient"):
        return "trust: insufficient data, recommend 0 (advice only; the owner sets allocation.ai_trust)"
    pc, sk = t["paired"], t["brier_skill"][t.get("brier_basis", "overall")]
    ci = pc["ci95"]
    s = (f"trust: recommend {t['recommended']:g} (advice only) | n={pc['n']}d AI-vs-default {_f(pc['mean_pp'], 3, True)} pp/day"
         f" CI {'n/a' if not ci else f'[{ci[0]:+.3f}, {ci[1]:+.3f}]'} t {_f(pc['t'])} | Brier skill {_f(sk['skill'], 3, True)} (n={sk['n']})"
         f" | validity {_f(t['validity'].get('rate'))} | misquote rate {_f(t['factcheck']['rate'])}")
    if t.get("power_note"):
        s += f" | {t['power_note']}"
    return s


def text(t: dict) -> str:
    L = [summary_line(t), f"as of {t['asof']}" + (f" (week {t['week']})" if t.get("week") else "")]
    if t.get("insufficient"):
        return "\n".join(L + ["why: " + r for r in t["reasons"]])
    pc = t["paired"]
    L.append(f"paired AI-vs-default: n={pc['n']} mean {_f(pc['mean_pp'], 4, True)} pp std {_f(pc['std_pp'], 4)} t {_f(pc['t'])} CI95 {pc['ci95']}")
    L.append("Brier skill vs base-rate forecaster (1 = perfect, 0 = no better than base rates):")
    for k, v in t["brier_skill"].items():
        L.append(f"  {k:<10} n={v['n']:<3} obs={v['n_obs']:<3} skill {_f(v['skill'], 3, True)} CI95 {v['ci95']}")
    L.append(f"proposal validity {t['validity']} | fact-check {t['factcheck']} | by month {t.get('by_month_pp')}")
    for r in t["reasons"]:
        L.append("why: " + r)
    L.append("table (a level needs every check):")
    for row in t["table"]:
        L.append(f"  {row['level']:<5} {'PASS' if row['pass'] else 'no  '} " + ("" if row["pass"] else "failing: " + "; ".join(
            c["test"] for c in row["checks"] if not c["ok"])))
    return "\n".join(L)
