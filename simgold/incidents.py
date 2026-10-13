"""Engine-side incident detection: thresholds, JSONL log, rate-limited webhook to the incident routine.

Nothing in here may raise into the trading loop; every public method swallows its own errors.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from .market import IST

log = logging.getLogger("incidents")

TICK_EXC_THRESHOLD = 3          # consecutive tick exceptions
NO_PRICES_MIN = 10              # minutes without fresh prices during market hours
SYNC_FAIL_THRESHOLD = 3         # consecutive git sync failures
MAX_PER_DAY = 3                 # webhook posts per IST day
MIN_GAP_MIN = 30                # minutes between webhook posts
WORKFLOW = "market-hours paper trading"
MAX_TEXT = 12_000


def _day(now: float) -> str:
    return datetime.fromtimestamp(now, IST).date().isoformat()


def build_text(facts: dict) -> str:
    """Compact JSON string, hard-capped so the POST body stays small."""
    text = json.dumps(facts, separators=(",", ":"), default=str)
    if len(text) > MAX_TEXT:
        facts = {**facts, "detail": str(facts.get("detail", ""))[:2000]}
        text = json.dumps(facts, separators=(",", ":"), default=str)[:MAX_TEXT]
    return text


def post_routine(text: str, url: str, token: str, timeout: float = 10.0) -> bool:
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "experimental-cc-routine-2026-04-01",
        "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001 - must never crash the engine
        log.warning("incident POST failed: %s", e)
        return False


class IncidentMonitor:
    def __init__(self, logs_dir: Path, poster=post_routine, clock=time.time):
        self.logs_dir, self.poster, self.clock = Path(logs_dir), poster, clock
        self.tick_failures = 0
        self.sync_failures = 0
        self.last_fresh = clock()
        self._fired: set[str] = set()      # kinds already fired in the current streak

    # ---- log + notify ------------------------------------------------------------
    def _file(self, now: float) -> Path:
        return self.logs_dir / f"incidents-{_day(now)}.jsonl"

    def _entries(self, now: float) -> list[dict]:
        out = []
        try:
            for line in self._file(now).read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def can_notify(self, now: float) -> bool:
        sent = [e["notified_at"] for e in self._entries(now) if e.get("notified_at")]
        return len(sent) < MAX_PER_DAY and (not sent or now - max(sent) >= MIN_GAP_MIN * 60)

    def report(self, kind: str, severity: str, detail: str, step: str | None = None, now: float | None = None) -> dict:
        """Always append to the JSONL; POST only if the rate limit allows. Never raises."""
        now = self.clock() if now is None else now
        rec = {"ts": datetime.fromtimestamp(now, IST).isoformat(timespec="seconds"), "kind": kind,
               "severity": severity, "workflow": WORKFLOW, "step": step or kind, "detail": str(detail)[:4000]}
        try:
            notified = False
            url, token = os.getenv("ROUTINE_INCIDENT_URL", ""), os.getenv("ROUTINE_INCIDENT_TOKEN", "")
            if url and token and self.can_notify(now):
                notified = bool(self.poster(build_text({**rec, "source": "engine"}), url, token))
            if notified:
                rec["notified_at"] = now
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            with open(self._file(now), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except Exception:  # noqa: BLE001
            log.exception("could not record incident")
        return rec

    def _once(self, kind: str, severity: str, detail: str, now: float | None) -> None:
        if kind not in self._fired:
            self._fired.add(kind)
            self.report(kind, severity, detail, now=now)

    # ---- detectors ---------------------------------------------------------------
    def tick_ok(self) -> None:
        self.tick_failures = 0
        self._fired.discard("tick_exceptions")

    def tick_failed(self, exc: BaseException, now: float | None = None) -> None:
        self.tick_failures += 1
        if self.tick_failures >= TICK_EXC_THRESHOLD:
            self._once("tick_exceptions", "high",
                       f"{self.tick_failures} consecutive tick exceptions; last: {type(exc).__name__}: {exc}", now)

    def mark_fresh(self, now: float | None = None) -> None:
        self.last_fresh = self.clock() if now is None else now
        self._fired.discard("no_prices")

    def check_feed(self, market_open: bool, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if not market_open:
            self.last_fresh = now           # the clock only runs while the market is open
            return
        gap = (now - self.last_fresh) / 60
        if gap > NO_PRICES_MIN:
            self._once("no_prices", "medium", f"no fresh prices for {gap:.0f} min during market hours", now)

    def sync_result(self, ok: bool, detail: str = "", now: float | None = None) -> None:
        if ok:
            self.sync_failures = 0
            self._fired.discard("git_sync")
            return
        self.sync_failures += 1
        if self.sync_failures >= SYNC_FAIL_THRESHOLD:
            self._once("git_sync", "medium", f"git sync failed {self.sync_failures} times in a row: {detail}", now)

    def state_corrupt(self, detail: str, now: float | None = None) -> None:
        self._once("state_corrupt", "high", detail, now)


# ---- robust portfolio loading ------------------------------------------------------
def _try_parse(text: str):
    from .broker import Portfolio, Position
    d = json.loads(text)
    d["positions"] = {k: Position(**v) for k, v in d.get("positions", {}).items()}
    return Portfolio(**d)


def git_show(repo: Path, rev_path: str) -> str | None:
    try:
        r = subprocess.run(["git", "show", rev_path], cwd=repo, capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def load_portfolio_safe(path: Path, backup: Path, repo: Path, git_show_fn=git_show):
    """Return (portfolio, recovery) where recovery is None if the file was fine, else a description.
    A recovery (or fresh start) means the caller must run in safe mode (no new buys)."""
    from .broker import Portfolio
    path = Path(path)
    if not path.exists():
        return Portfolio(), None
    try:
        return _try_parse(path.read_text()), None
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    sources = [("state/portfolio.backup.json", lambda: Path(backup).read_text() if Path(backup).exists() else None),
              ("git HEAD~1:state/portfolio.json", lambda: git_show_fn(repo, "HEAD~1:state/portfolio.json"))]
    try:
        path.replace(path.with_name(path.name + ".corrupt"))
    except OSError:
        pass
    for name, getter in sources:
        try:
            text = getter()
            if text:
                return _try_parse(text), f"portfolio.json unreadable ({err}); restored from {name}"
        except Exception:  # noqa: BLE001
            continue
    return Portfolio(), f"portfolio.json unreadable ({err}); NO good copy found, started from empty defaults"
