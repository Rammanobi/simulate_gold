"""Thrift mode: which billing source the Claude routines run on (state/budget.json).

Routine sessions learn it from the claude-code-remote `get_session` tool
(`external_metadata.rate_limit_info.rateLimitType`, `isUsingOverage`) and record it with `tools/budget.py mode`.
"ccr_promotional" (promotional cloud-session credits) is NORMAL. Anything else, an unknown or empty value, or any
overage use, is THRIFT: routines then spend less (see routines/*.md step 0) and the engine allows fewer fast-lane
wake-ups a day. Thrift mode never touches a trading rule. Nothing here raises.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
NORMAL, THRIFT = "NORMAL", "THRIFT"
PROMO = "ccr_promotional"
DEFAULTS = {"thrift_fastlane_max_per_day": 2, "thrift_hourly_opus": False, "thrift_weekly_short": True,
            "thrift_incident_opus_only_high": True}


def mode_for(rate_limit_type, overage=False) -> str:
    """NORMAL only for the promotional credits and no overage; everything else (also unknown/empty) is THRIFT."""
    if overage is True:
        return THRIFT
    return NORMAL if str(rate_limit_type or "").strip().lower() == PROMO else THRIFT


def path(state_dir) -> Path:
    return Path(state_dir) / "budget.json"


def write(state_dir, rate_limit_type, overage=False, now: datetime | None = None) -> dict:
    rec = {"mode": mode_for(rate_limit_type, overage), "rate_limit_type": str(rate_limit_type or ""),
           "overage": bool(overage), "checked_at": (now or datetime.now(IST)).isoformat(timespec="seconds")}
    p = path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)
    return rec


def read_mode(state_dir) -> str:
    """No file = NORMAL (nothing has asked for thrift). A file that cannot be read, or says anything but NORMAL = THRIFT."""
    p = path(state_dir)
    if not p.exists():
        return NORMAL
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return NORMAL if isinstance(d, dict) and d.get("mode") == NORMAL else THRIFT
    except (OSError, ValueError):
        return THRIFT


def is_thrift(state_dir) -> bool:
    return read_mode(state_dir) == THRIFT


def settings(p: dict | None) -> dict:
    out = dict(DEFAULTS)
    b = (p or {}).get("budget")
    if isinstance(b, dict):
        out.update({k: v for k, v in b.items() if k in DEFAULTS})
    return out


def fastlane_cap(p: dict, state_dir) -> int:
    """Fast-lane routine wake-ups allowed per day: the normal cap, or the thrift cap when the mode is THRIFT."""
    normal = int(p["fast_lane"]["max_runs_per_day"])
    if not is_thrift(state_dir):
        return normal
    try:
        return max(0, min(normal, int(settings(p)["thrift_fastlane_max_per_day"])))
    except (TypeError, ValueError):
        return normal
