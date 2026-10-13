"""Engine-side incident detection. Thresholds and the wake budget come from simgold/alerts.py (config alerts:).

Every detector appends to logs/incidents-DATE.jsonl and records the alert in state/alerts.json; the routine webhook
is POSTed only when alerts.py says so (new fingerprint, within the 6-fires/day budget). Nothing here may raise
into the trading loop.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import alerts
from .market import IST

log = logging.getLogger("incidents")
WORKFLOW = "market-hours paper trading"

# kept for callers that imported them
build_text = alerts.build_text
post_routine = alerts.post_routine


class IncidentMonitor:
    def __init__(self, logs_dir: Path, state_dir: Path | None = None, poster=alerts.post_routine,
                 clock=time.time, cfg: dict | None = None, environ=None):
        self.logs_dir = Path(logs_dir)
        self.state_dir = Path(state_dir) if state_dir else self.logs_dir.parent / "state"
        self.poster, self.clock = poster, clock
        self.cfg = cfg or alerts.params()
        self.environ = os.environ if environ is None else environ
        self.tick_failures = 0
        self.sync_failures = 0
        self.first_sync_fail: float | None = None
        self.last_fresh = clock()
        self._open: dict[str, str] = {}      # kind -> fingerprint, while its streak is active

    # ---- plumbing ----------------------------------------------------------------
    def _log(self, rec: dict, now: float) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        f = self.logs_dir / f"incidents-{alerts.day_of(now)}.jsonl"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")

    def _post(self, facts: dict) -> bool:
        url, token = self.environ.get("ROUTINE_INCIDENT_URL", ""), self.environ.get("ROUTINE_INCIDENT_TOKEN", "")
        if not (url and token):
            return False
        return bool(self.poster(alerts.build_text(facts), url, token))

    def report(self, kind: str, category: str, step: str, detail: str, error: str = "", now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        d: dict = {}
        try:
            d = alerts.raise_alert(self.state_dir, "engine", step, category, detail, error, now=now, cfg=self.cfg)
            rec = {"ts": datetime.fromtimestamp(now, IST).isoformat(timespec="seconds"), "kind": kind, "category": category,
                   "severity": d.get("severity"), "workflow": WORKFLOW, "step": step, "detail": str(detail)[:4000],
                   "fingerprint": d.get("fingerprint"), "fired": bool(d.get("fire")), "decision": d.get("reason")}
            self._log(rec, now)
            if d.get("fire"):
                self._post({**{k: rec[k] for k in ("kind", "category", "severity", "workflow", "step", "detail", "fingerprint")},
                            "source": "engine", "component": "engine"})
            if d.get("new"):
                self._open[kind] = d["fingerprint"]
            alerts.evaluate_safe_mode(self.state_dir, now, self.cfg)
        except Exception:  # noqa: BLE001
            log.exception("could not record incident")
        return d

    def _clear(self, kind: str, now: float | None = None) -> None:
        fp = self._open.pop(kind, None)
        if not fp:
            return
        try:
            now = self.clock() if now is None else now
            e = alerts.resolve(self.state_dir, fp, now)
            if e and e.get("emails") and not e.get("resolved_emailed"):
                self._post({"mode": "resolved", "incident_id": e.get("incident_id") or fp, "fingerprint": fp, "source": "engine"})
        except Exception:  # noqa: BLE001
            log.exception("could not clear incident")

    def housekeeping(self, now: float | None = None) -> None:
        """Cheap per-iteration check of the safe-mode triggers (distinct incidents, HIGH unfixed for 30 min)."""
        try:
            alerts.evaluate_safe_mode(self.state_dir, self.clock() if now is None else now, self.cfg)
        except Exception:  # noqa: BLE001
            log.exception("safe-mode evaluation failed")

    # ---- detectors ---------------------------------------------------------------
    def tick_ok(self, now: float | None = None) -> None:
        self.tick_failures = 0
        self._clear("tick_exceptions", now)
        self._clear("tick_transient", now)

    def tick_failed(self, exc: BaseException, now: float | None = None) -> None:
        self.tick_failures += 1
        name = type(exc).__name__
        if self.tick_failures == 1:
            self.report("tick_transient", "transient_fetch", "tick", f"tick failed once: {name}: {exc}", name, now)
        if self.tick_failures >= self.cfg["tick_exceptions"] and "tick_exceptions" not in self._open:
            self.report("tick_exceptions", "tick_exceptions", "tick-exceptions",
                        f"{self.tick_failures} consecutive tick exceptions; last: {name}: {exc}", name, now)

    def mark_fresh(self, now: float | None = None) -> None:
        self.last_fresh = self.clock() if now is None else now
        self._clear("no_prices", now)

    def check_feed(self, market_open: bool, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if not market_open:
            self.last_fresh = now           # the clock only runs while the market is open
            return
        gap = (now - self.last_fresh) / 60
        if gap > self.cfg["no_prices_min"] and "no_prices" not in self._open:
            self.report("no_prices", "data_source_down", "price-feed", f"no fresh prices for {gap:.0f} min during market hours", "", now)

    def sync_result(self, ok: bool, detail: str = "", now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if ok:
            self.sync_failures, self.first_sync_fail = 0, None
            self._clear("git_sync", now)
            return
        self.sync_failures += 1
        self.first_sync_fail = self.first_sync_fail or now
        mins = (now - self.first_sync_fail) / 60
        if mins >= self.cfg["git_push_fail_min"] and "git_sync" not in self._open:
            self.report("git_sync", "git_push_failing", "git-sync",
                        f"git sync failing for {mins:.0f} min ({self.sync_failures} attempts): {detail}", "", now)

    def state_corrupt(self, detail: str, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self.report("state_corrupt", "state_corrupt", "portfolio-state", detail, "", now)
        alerts.set_safe_mode(self.state_dir, "portfolio state was corrupted: " + detail[:160], now, notify=False)


# ---- robust portfolio loading ------------------------------------------------------
def _try_parse(text: str):
    from .broker import Portfolio
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "p.json"
        p.write_text(text)
        return Portfolio.load(p)


def git_show(repo: Path, rev_path: str) -> str | None:
    try:
        r = subprocess.run(["git", "show", rev_path], cwd=repo, capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def load_portfolio_safe(path: Path, backup: Path, repo: Path, git_show_fn=git_show):
    """Return (portfolio, recovery); recovery is None if the file was fine, else a description (caller enters safe mode)."""
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
