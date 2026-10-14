"""Only one engine run per session, checked on GitHub itself.

The `paper-trader` concurrency group serialises market-hours runs, and state/.engine.lock covers one checkout. Neither
stops a second run that GitHub starts on another runner (a duplicate dispatch, or a late cron next to a dispatch).
At start-up the engine asks the Actions REST API for in-progress runs of its own workflow; when an OLDER one is
running, this run exits at once, cleanly (exit 0), and says so in the log.

Fails open: outside Actions, without a token, or when the API cannot be reached, it returns None and the engine
starts as before (the concurrency group is still there).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from typing import Callable, Mapping

log = logging.getLogger(__name__)

Fetch = Callable[[str], dict]


def _fetcher(environ: Mapping[str, str]) -> Fetch | None:
    token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN")
    if not token:
        return None
    api = (environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def fetch(path: str) -> dict:
        req = urllib.request.Request(f"{api}/{path}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "simgold-engine"})
        with urllib.request.urlopen(req, timeout=20) as r:      # noqa: S310 - fixed https API host
            return json.loads(r.read().decode("utf-8"))
    return fetch


def _older(a: dict, b: dict) -> bool:
    """Is run a older than run b? created_at first (ISO strings sort), then the run id."""
    ka, kb = (str(a.get("created_at") or ""), int(a.get("id") or 0)), (str(b.get("created_at") or ""), int(b.get("id") or 0))
    return ka < kb


def older_engine_run(environ: Mapping[str, str], fetch: Fetch | None = None) -> dict | None:
    """The oldest OTHER in-progress run of this run's workflow that started before this one, or None."""
    run_id, repo = str(environ.get("GITHUB_RUN_ID") or ""), environ.get("GITHUB_REPOSITORY") or ""
    if not run_id.isdigit() or not repo:
        return None                                   # not on GitHub Actions
    fetch = fetch or _fetcher(environ)
    if fetch is None:
        log.info("run guard: no GH_TOKEN, cannot check for another engine run")
        return None
    try:
        me = fetch(f"repos/{repo}/actions/runs/{run_id}")
        wf = me.get("workflow_id")
        data = fetch(f"repos/{repo}/actions/workflows/{wf}/runs?status=in_progress&per_page=30")
        others = [r for r in data.get("workflow_runs") or []
                  if str(r.get("id")) != run_id and _older(r, me)]
    except Exception as e:      # noqa: BLE001 - fail open
        log.warning("run guard: could not check for another engine run (%s)", e)
        return None
    return min(others, key=lambda r: (str(r.get("created_at") or ""), int(r.get("id") or 0))) if others else None
