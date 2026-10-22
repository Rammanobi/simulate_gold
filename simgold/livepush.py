"""Publish the live P&L file (now.json) to the `live` branch with git plumbing. View-only convenience for the dashboard.

Plumbing only: hash-object -w, mktree, commit-tree, then a push of the commit sha. It never touches the working tree,
the index, HEAD or `main`, and it never forces: a rejected push is answered by fetching the new tip and trying once more.
Every call is wrapped in try/except with timeouts, so trading can never be blocked. Not wired to IncidentMonitor.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import gitsync

log = logging.getLogger(__name__)

LIVE_BRANCH = "live"
LIVE_FILE = "now.json"
FETCH_TIMEOUT, PUSH_TIMEOUT, LOCAL_TIMEOUT = 20, 15, 10
FAIL_LIMIT, BACKOFF_S = 3, 300


def enabled(environ) -> bool:
    """Kill switch: SIMGOLD_LIVE_PUSH=1 and the git sync is on."""
    return str(environ.get("SIMGOLD_LIVE_PUSH", "")).strip() == "1" and gitsync.enabled(environ)


class LivePublisher:
    def __init__(self, root: Path, runner=subprocess.run, remote: str = "origin", min_interval: float = 50,
                 environ=None, clock=time.monotonic):
        self.root, self.runner, self.remote = Path(root), runner, remote
        self.min_interval, self.clock = min_interval, clock
        self.environ = os.environ if environ is None else environ
        self.parent: str | None = None
        self._fetched = False
        self.fails = 0
        self.skip_until = 0.0
        self.last_try = None

    # ---- git -------------------------------------------------------------
    def _env(self) -> dict:
        env = dict(os.environ)
        for k, v in (("GIT_AUTHOR_NAME", "simgold-engine"), ("GIT_AUTHOR_EMAIL", "actions@users.noreply.github.com")):
            env.setdefault(k, v)
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _git(self, *args: str, input: bytes | None = None, timeout: float = LOCAL_TIMEOUT):
        return self.runner(["git", *args], cwd=self.root, capture_output=True, input=input, timeout=timeout,
                           env=self._env())

    @staticmethod
    def _text(b) -> str:
        return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else (b or "")

    def start(self) -> None:
        """Fetch the tip of `live` (once at engine start, and again after a rejected push). Never raises."""
        try:
            if not enabled(self.environ):
                return
            f = self._git("fetch", "--depth=1", "--no-tags", self.remote, f"refs/heads/{LIVE_BRANCH}", timeout=FETCH_TIMEOUT)
            if f.returncode == 0:
                r = self._git("rev-parse", "FETCH_HEAD^{commit}")
                sha = self._text(r.stdout).strip()
                if r.returncode == 0 and len(sha) >= 40:
                    self.parent, self._fetched = sha, True
                    return
            err = self._text(f.stderr)
            if "couldn't find remote ref" in err:        # first ever run: the branch does not exist yet
                self.parent, self._fetched = None, True
            else:
                self._fetched = False
                log.warning("live branch fetch failed: %s", err.strip()[:200])
        except Exception as e:      # noqa: BLE001
            self._fetched = False
            log.warning("live branch fetch error: %s", e)

    def _commit(self, payload: bytes, now: datetime) -> str | None:
        h = self._git("hash-object", "-w", "--stdin", input=payload)
        blob = self._text(h.stdout).strip()
        if h.returncode != 0 or not blob:
            return None
        t = self._git("mktree", input=f"100644 blob {blob}\t{LIVE_FILE}\n".encode())
        tree = self._text(t.stdout).strip()
        if t.returncode != 0 or not tree:
            return None
        args = ["commit-tree", tree] + (["-p", self.parent] if self.parent else []) + ["-m", f"live {now:%H:%M} IST"]
        c = self._git(*args)
        sha = self._text(c.stdout).strip()
        return sha if c.returncode == 0 and sha else None

    def _push(self, sha: str):
        return self._git("push", "--no-verify", self.remote, f"{sha}:refs/heads/{LIVE_BRANCH}", timeout=PUSH_TIMEOUT)

    def _attempt(self, payload: bytes, now: datetime) -> bool:
        if not self._fetched:
            self.start()
            if not self._fetched:
                return False
        sha = self._commit(payload, now)
        if not sha:
            return False
        p = self._push(sha)
        if p.returncode != 0:
            # only the engine writes `live`; if the tip moved, take the new tip and rebuild once. Never a force.
            old = self.parent
            self.start()
            if not self._fetched or self.parent == old:
                log.debug("live push failed: %s", self._text(p.stderr).strip()[:200])
                return False
            sha = self._commit(payload, now)
            if not sha or self._push(sha).returncode != 0:
                return False
        self.parent = sha
        return True

    # ---- public ----------------------------------------------------------
    def publish(self, payload: bytes, now: datetime) -> bool:
        """Push `payload` as now.json on the live branch. True if pushed. Never raises."""
        try:
            if not enabled(self.environ):
                return False
            t = self.clock()
            if t < self.skip_until or (self.last_try is not None and t - self.last_try < self.min_interval):
                return False
            self.last_try = t
            ok = self._attempt(payload, now)
        except Exception as e:      # noqa: BLE001 - includes timeouts and a missing git
            log.debug("live publish error: %s", e)
            ok = False
        try:
            if ok:
                self.fails, self.skip_until = 0, 0.0
            else:
                self.fails += 1
                if self.fails >= FAIL_LIMIT:
                    if self.clock() >= self.skip_until:       # one warning per back-off, not one per tick
                        log.warning("live push failed %d times in a row; backing off %d min (trading is not affected)",
                                    self.fails, BACKOFF_S // 60)
                    self.skip_until = self.clock() + BACKOFF_S
        except Exception:       # noqa: BLE001
            pass
        return ok
