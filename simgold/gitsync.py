"""In-day git sync so Claude routines can see live state and the engine picks up their guidance.

Only active when SIMGOLD_GIT_SYNC=1. Never raises: any failure is logged and trading goes on.
The engine also commits memory/brief.md. Routines write only state/guidance.json, fastlane.json, events.json, market_today.json and tomorrow.txt;
the engine never writes those, so rebases should not conflict. If one still fails it is aborted.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .market import IST

log = logging.getLogger(__name__)


def enabled(environ) -> bool:
    return str(environ.get("SIMGOLD_GIT_SYNC", "")).strip() == "1"


def _git(root: Path, *args: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return runner(["git", *args], cwd=root, capture_output=True, text=True, timeout=90)


def sync(root: Path, runner=subprocess.run, now: datetime | None = None, retries: int = 3, monitor=None) -> bool:
    """add -> commit -> pull --rebase -> push. Returns True if the push succeeded (or nothing to push).
    `monitor` (simgold.incidents.IncidentMonitor) is told the result so repeated failures raise an incident."""
    ok = _sync(root, runner, now, retries)
    if monitor is not None:
        try:
            monitor.sync_result(ok, "" if ok else "see the engine log for the git error")
        except Exception:      # noqa: BLE001 - never let alerting break trading
            log.exception("incident monitor failed")
    return ok


SYNC_DIRS = ("state", "logs", "reports", "memory")
MERGE_DRIVER = "simgold-alerts"        # .gitattributes: state/alerts.json merge=simgold-alerts


def configure_merge_driver(root: Path, runner=subprocess.run) -> None:
    """state/alerts.json is written by the engine AND the watchdog/reminders/incident workflows. A line merge of two
    pretty-printed JSON files can be invalid or attach a hunk to the wrong alert, so it is merged as JSON instead."""
    _git(root, "config", f"merge.{MERGE_DRIVER}.name", "union merge of simgold alert state", runner=runner)
    _git(root, "config", f"merge.{MERGE_DRIVER}.driver",
         f'"{sys.executable}" -m simgold.alerts merge-file %O %A %B', runner=runner)


def _unmerged(root: Path, runner) -> list[str]:
    r = _git(root, "diff", "--name-only", "--diff-filter=U", runner=runner)
    return [x for x in (r.stdout or "").splitlines() if x.strip()] if r.returncode == 0 else []


def _merge_fallback(root: Path, runner, paths: list[str]) -> bool:
    """The rebase could not be finished (e.g. a modify/delete conflict). Never force, never rewrite history:
    merge instead, keep the engine's working-tree version of the sync folders, and commit the merge."""
    m = _git(root, "pull", "--no-rebase", "--no-edit", "-X", "ours", runner=runner)
    if m.returncode == 0:
        return True
    left = _unmerged(root, runner)
    if not left or any(not f.startswith(tuple(d + "/" for d in SYNC_DIRS)) for f in left):
        _git(root, "merge", "--abort", runner=runner)
        return False
    _git(root, "add", "-A", "--", *paths, runner=runner)
    c = _git(root, "commit", "-q", "--no-edit", runner=runner)
    if c.returncode != 0:
        _git(root, "merge", "--abort", runner=runner)
        return False
    log.warning("git rebase was impossible; merged instead, keeping the engine's files for %s", ", ".join(left))
    return True


def _sync(root: Path, runner, now, retries) -> bool:
    stamp = (now or datetime.now(IST)).strftime("%H:%M")
    try:
        try:
            configure_merge_driver(root, runner)
        except Exception:  # noqa: BLE001 - a plain line merge is the fallback
            pass
        paths = [d for d in SYNC_DIRS if (Path(root) / d).exists()]      # a missing folder made `git add` add nothing
        a = _git(root, "add", "-A", "--", *paths, runner=runner)
        if a.returncode != 0:
            log.warning("git add failed: %s", (a.stderr or a.stdout).strip()[:200])
            return False
        if _git(root, "diff", "--cached", "--quiet", runner=runner).returncode != 0:
            c = _git(root, "commit", "-q", "-m", f"paper-trade: {stamp} IST", runner=runner)
            if c.returncode != 0:     # do NOT pull now: autostash would stash the uncommitted state and could
                log.warning("git commit failed: %s", (c.stderr or c.stdout).strip()[:200])   # pop it back with
                return False                                                                 # conflict markers
        for attempt in range(1, retries + 1):
            p = _git(root, "pull", "--rebase", "--autostash", "-X", "theirs", runner=runner)
            if p.returncode != 0:
                log.warning("git pull --rebase failed (%s); aborting the rebase and merging instead",
                            (p.stderr or p.stdout).strip()[:200])
                _git(root, "rebase", "--abort", runner=runner)
                if not _merge_fallback(root, runner, paths):
                    return False
            bad = _unmerged(root, runner)
            if bad:        # an autostash that popped back with conflicts: restore those files, the stash is kept
                log.warning("autostash left conflicts in %s; restoring them from HEAD (stash kept)", ", ".join(bad))
                _git(root, "reset", "-q", "--", *bad, runner=runner)
                _git(root, "checkout", "HEAD", "--", *bad, runner=runner)
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
