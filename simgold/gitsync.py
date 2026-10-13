"""In-day git sync so Claude routines can see live state and the engine picks up their guidance.

Only active when SIMGOLD_GIT_SYNC=1. Never raises: any failure is logged and trading goes on.
The engine also commits memory/brief.md. Routines write only state/guidance.json, fastlane.json, events.json, market_today.json and tomorrow.txt;
the engine never writes those, so rebases should not conflict. If one still fails it is aborted.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from .market import IST

log = logging.getLogger(__name__)


def enabled(environ) -> bool:
    return str(environ.get("SIMGOLD_GIT_SYNC", "")).strip() == "1"


def _git(root: Path, *args: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return runner(["git", *args], cwd=root, capture_output=True, text=True, timeout=90)


def sync(root: Path, runner=subprocess.run, now: datetime | None = None, retries: int = 3) -> bool:
    """add -> commit -> pull --rebase -> push. Returns True if the push succeeded (or nothing to push)."""
    stamp = (now or datetime.now(IST)).strftime("%H:%M")
    try:
        _git(root, "add", "state", "logs", "reports", "memory", runner=runner)
        if _git(root, "diff", "--cached", "--quiet", runner=runner).returncode != 0:
            c = _git(root, "commit", "-q", "-m", f"paper-trade: {stamp} IST", runner=runner)
            if c.returncode != 0:
                log.warning("git commit failed: %s", (c.stderr or c.stdout).strip()[:200])
        for attempt in range(1, retries + 1):
            p = _git(root, "pull", "--rebase", "--autostash", "-X", "theirs", runner=runner)
            if p.returncode != 0:
                log.warning("git pull --rebase failed (%s); aborting the rebase and carrying on",
                            (p.stderr or p.stdout).strip()[:200])
                _git(root, "rebase", "--abort", runner=runner)
                return False
            q = _git(root, "push", runner=runner)
            if q.returncode == 0:
                return True
            log.warning("git push failed (attempt %d/%d): %s", attempt, retries, (q.stderr or q.stdout).strip()[:200])
        return False
    except Exception as e:      # timeouts, git missing, ...
        log.warning("git sync error: %s", e)
        try:
            _git(root, "rebase", "--abort", runner=runner)
        except Exception:
            pass
        return False
