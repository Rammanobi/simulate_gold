"""Engine relay: starts the engine without Claude routines and without GitHub 'schedule' timers.

12 Nov 2026 (a real trading day): every way the engine was started depended on a Claude routine or on a GitHub cron, the
routines were silent and the crons ran ~8 h late, so no session ran. `workflow_dispatch` made with the job's GITHUB_TOKEN starts
within seconds, so a chain of long-sleeping relay runs (`.github/workflows/engine-relay.yml`) carries the clock overnight and
at weekends, dispatches `market-hours.yml` at 08:55 IST and re-checks at ~09:30 and ~12:20 IST. It never trades and never
commits. `tools/ensure_engine.py` is the idempotent "start whatever is missing" used by other workflows as a restart source.

Everything with side effects takes an injected `api` (see `GhApi`), clock and sleep, so tests run without a network.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from simgold.market import IST, is_trading_day

log = logging.getLogger("relay")
ROOT = Path(__file__).resolve().parent.parent

ENGINE_WF = "market-hours.yml"
RELAY_WF = "engine-relay.yml"
MORNING_EMAIL_WF = "morning-email.yml"
START = dtime(8, 55)                     # first dispatch of the engine
# Re-checks after the 08:55 start. 12:20/12:30 cover the morning run ending and the afternoon hand-over; the hourly ones keep a
# relay alive until the last engine start (15:20), so there is always a relay or an engine run from 08:55 to the next morning.
CHECKS = (dtime(9, 30), dtime(12, 20), dtime(12, 30), dtime(13, 30), dtime(14, 30), dtime(15, 20))
HOP_MAX = timedelta(hours=5, minutes=45)  # one relay run lives at most this long (job timeout is 355 min)
ENGINE_LAST_START = dtime(15, 25)        # same cut-off as the market-hours hand-over step
TEST_MARK = "TEST"                       # run-name of dry_run / until runs (engine-relay.yml): they are never counted as a relay
MAX_UNTIL_AHEAD = timedelta(days=7)
CHUNK_S = 300                            # real sleeps are cut into chunks of this many seconds


# ---------------------------------------------------------------- calendar
def next_trading_start(now: datetime) -> datetime:
    """The next relay start (08:55 IST of a trading day). Today's counts while it is still before the last re-check (15:20)."""
    now = now.astimezone(IST)
    d = now.date()
    if is_trading_day(d) and now < datetime.combine(d, CHECKS[-1], IST):
        return datetime.combine(d, START, IST)
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, START, IST)


@dataclass(frozen=True)
class Event:
    at: datetime
    kind: str            # start | check1 .. checkN
    catch_up: bool = False   # an event already past when this run began: handled at once, never sends the morning email


def day_events(start: datetime, shift: bool = False) -> list[Event]:
    """The three moments of a relay day. With `shift` (the `until` test override) the re-checks keep their offsets from the start."""
    start = start.astimezone(IST)
    if shift:
        base = datetime.combine(start.date(), START, IST)
        offs = [start + (datetime.combine(start.date(), c, IST) - base) for c in CHECKS]
    else:
        offs = [datetime.combine(start.date(), c, IST) for c in CHECKS]
    return [Event(start, "start")] + [Event(o, f"check{i}") for i, o in enumerate(offs, 1)]


def pending_events(now: datetime, events: list[Event]) -> list[Event]:
    """Events still to handle. Events already passed collapse into ONE immediate catch-up (the latest passed one)."""
    now = now.astimezone(IST)
    passed = [e for e in events if e.at <= now]
    future = [e for e in events if e.at > now]
    if passed and not (len(passed) == len(events)):          # all passed = the day is over: nothing to do
        return [Event(now, passed[-1].kind, True)] + future
    return future


def plan_hop(now: datetime, target: datetime, budget: timedelta = HOP_MAX) -> tuple[float, bool]:
    """(seconds to sleep, reaches_target). Never sleeps past the target, never longer than the budget."""
    rem = (target - now).total_seconds()
    if rem <= 0:
        return 0.0, True
    if rem <= budget.total_seconds():
        return rem, True
    return budget.total_seconds(), False


# ---------------------------------------------------------------- GitHub API
Api = Callable[..., dict]


class GhApi:
    """Minimal REST client: api(method, path, body=None) -> dict. Uses GITHUB_TOKEN / GH_TOKEN."""

    def __init__(self, environ: Mapping[str, str]):
        self.token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or ""
        self.base = (environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def __call__(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}/{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "simgold-relay", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:      # noqa: S310 - fixed https API host
            raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def list_runs(api: Api, repo: str, workflow: str) -> list[dict]:
    return list((api("GET", f"repos/{repo}/actions/workflows/{workflow}/runs?per_page=20") or {}).get("workflow_runs") or [])


def is_test_run(run: dict) -> bool:
    """A dry_run / until run of engine-relay.yml (its run-name carries TEST). It lives in its own concurrency group and must never be
    mistaken for the live chain: 'a pending relay exists' must not be satisfied by a dry run that dispatches nothing."""
    return TEST_MARK in str(run.get("display_title") or run.get("name") or "")


def active_runs(api: Api, repo: str, workflow: str, exclude_id: str | None = None) -> list[dict]:
    """Runs of `workflow` that are not completed (queued, pending, waiting, requested, in_progress), minus `exclude_id`;
    for engine-relay.yml only LIVE runs (test runs are ignored)."""
    return [r for r in list_runs(api, repo, workflow)
            if r.get("status") != "completed" and (not exclude_id or str(r.get("id")) != str(exclude_id))
            and not (workflow == RELAY_WF and is_test_run(r))]


def dispatch(api: Api, repo: str, workflow: str, inputs: dict | None = None) -> None:
    body: dict = {"ref": "main"}
    if inputs:
        body["inputs"] = inputs
    api("POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches", body)


def workflow_file_exists(root: Path, name: str) -> bool:
    return (root / ".github" / "workflows" / name).is_file()


def morning_email_enabled(root: Path = ROOT, params: dict | None = None) -> bool:
    """morning-email.yml exists AND report.morning_email is true (read with .get: the param may not exist; default false)."""
    if not workflow_file_exists(root, MORNING_EMAIL_WF):
        return False
    try:
        if params is None:
            from simgold import config
            params = config.load_params()
        return bool((params.get("report") or {}).get("morning_email"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- relay
class Clock:
    def now(self) -> datetime:
        return datetime.now(IST)

    def sleep(self, seconds: float) -> None:
        import time
        time.sleep(seconds)


class VirtualClock(Clock):
    """Dry run: time jumps instead of passing."""

    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def parse_until(text: str | None, now: datetime | None = None) -> datetime | None:
    """ISO time (IST when no zone). Raises ValueError for junk, a time more than 5 min in the past or more than 7 days ahead.
    `until` replaces the 08:55 start of a TEST run (its re-checks keep their offsets); it never applies to the live chain."""
    if not text or not str(text).strip():
        return None
    t = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    t = (t if t.tzinfo else t.replace(tzinfo=IST)).astimezone(IST)
    if now is not None:
        now = now.astimezone(IST)
        if t < now - timedelta(minutes=5) or t > now + MAX_UNTIL_AHEAD:
            raise ValueError(f"until {t:%Y-%m-%d %H:%M} IST is not within 5 minutes ago .. 7 days ahead")
    return t


class Relay:
    def __init__(self, api: Api, repo: str, run_id: str = "", clock: Clock | None = None, dry_run: bool = False,
                 until: datetime | None = None, root: Path = ROOT, params: dict | None = None,
                 out: Callable[[str], None] = print):
        self.api, self.repo, self.run_id, self.dry, self.until, self.root, self.params = api, repo, str(run_id or ""), dry_run, until, root, params
        self.clock = clock or Clock()
        self.say = out
        self.actions: list[str] = []          # what was (or, in a dry run, would be) dispatched
        self.timeline: list[tuple[datetime, str]] = []
        self.test = bool(dry_run or until is not None)      # a TEST run: own concurrency group, never hands over, never a duplicate

    # -- helpers
    def log(self, msg: str) -> None:
        self.say(f"[{self.clock.now():%Y-%m-%d %H:%M:%S} IST] {'[dry-run] ' if self.dry else ''}{msg}")

    def _dispatch(self, workflow: str, inputs: dict | None = None) -> bool:
        self.actions.append(workflow)
        self.timeline.append((self.clock.now(), workflow))
        if self.dry:
            self.log(f"would dispatch {workflow} {inputs or ''}".rstrip())
            return True
        try:
            dispatch(self.api, self.repo, workflow, inputs)
            self.log(f"dispatched {workflow} {inputs or ''}".rstrip())
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"dispatch of {workflow} FAILED: {e}")
            return False

    def _active(self, workflow: str, exclude_own: bool) -> list[dict] | None:
        try:
            return active_runs(self.api, self.repo, workflow, self.run_id if exclude_own else None)
        except Exception as e:  # noqa: BLE001
            self.log(f"could not list {workflow} runs ({e})")
            return None

    # -- steps
    def duplicate_running(self) -> bool:
        """Another relay already IN PROGRESS (own run id excluded). The concurrency group queues a second one, so this is a safety net."""
        if self.test:
            return False                      # a test run has its own group and is not part of the chain
        others = self._active(RELAY_WF, True)
        return bool([r for r in others or [] if r.get("status") == "in_progress"])

    def handover(self) -> str:
        """Dispatch the successor relay unless another LIVE relay (not this run) is queued or running. A test run never hands over."""
        if self.test:
            self.log("test run (dry_run/until): no successor relay is dispatched")
            return "test-run"
        others = self._active(RELAY_WF, True)
        if others:
            self.log(f"successor not dispatched: another relay run is already pending ({len(others)})")
            return "pending-exists"
        return "dispatched" if self._dispatch(RELAY_WF) else "failed"

    def check_engine(self, ev: Event) -> str:
        day = ev.at.date()
        if not is_trading_day(day):
            self.log(f"{ev.kind}: {day} is not a trading day, nothing to dispatch")
            return "not-trading-day"
        active = self._active(ENGINE_WF, False)
        if active:
            res = f"engine already running ({len(active)} market-hours run(s) active)"
            self.log(f"{ev.kind}: {res}, no dispatch")
        else:
            if active is None:
                self.log(f"{ev.kind}: run list unavailable, dispatching anyway (duplicates are harmless: runguard + concurrency)")
            res = "dispatched" if self._dispatch(ENGINE_WF, {"session": "auto"}) else "failed"
        # the morning email only goes out from the on-time 08:55 event of a run that began before 08:55; a catch-up start (a later hop
        # of the chain, or a restart) never sends it, so the chain cannot send it twice
        if ev.kind == "start" and not ev.catch_up and morning_email_enabled(self.root, self.params):
            self._dispatch(MORNING_EMAIL_WF)
        return res

    def run(self) -> str:
        """Returns a short outcome string (also printed)."""
        if self.duplicate_running():
            self.log("another engine-relay run is already in progress: exiting")
            return "duplicate"
        now0 = self.clock.now()
        deadline = now0 + HOP_MAX
        if self.until is not None:
            events = day_events(self.until, shift=True)
        else:
            events = day_events(next_trading_start(now0))
        events = pending_events(now0, events)
        if not events:
            events = day_events(next_trading_start(now0 + timedelta(minutes=1)))
        self.log(f"relay run {self.run_id or '-'}: next event {events[0].kind} at {events[0].at:%Y-%m-%d %H:%M} IST, budget until {deadline:%H:%M}")
        while events:
            ev = events[0]
            if ev.at > self.clock.now():
                if ev.at > deadline:
                    self._sleep_until(deadline)
                    self.log("hop budget used up before the next event: handing over")
                    return "handover:" + self.handover()
                self._sleep_until(ev.at)
            events.pop(0)
            self.check_engine(ev)
        self.log("last check done: handing the chain over to the next relay run")
        self.handover()
        return "done"

    def _sleep_until(self, target: datetime) -> None:
        while True:
            rem = (target - self.clock.now()).total_seconds()
            if rem <= 0:
                return
            self.clock.sleep(min(rem, CHUNK_S) if not self.dry else rem)


# ---------------------------------------------------------------- ensure_engine
def ensure(api: Api, repo: str, now: datetime, dry_run: bool = False, out: Callable[[str], None] = print) -> str:
    """Idempotent: start whatever is missing. Returns 'ok', 'relay', 'engine', or 'skip'.

    - a relay or market-hours run queued/in progress -> nothing to do
    - a trading day between 08:55 and 15:25 IST with no run -> dispatch market-hours (session=auto)
    - anything else (night, weekend, holiday, after 15:25, or before 08:55) -> dispatch the relay
    """
    now = now.astimezone(IST)
    try:
        relay_active = active_runs(api, repo, RELAY_WF)
        engine_active = active_runs(api, repo, ENGINE_WF)
    except Exception as e:  # noqa: BLE001
        out(f"ensure_engine: SKIP, could not list runs ({e})")
        return "skip"
    if relay_active or engine_active:
        out(f"ensure_engine: ok (relay active {len(relay_active)}, market-hours active {len(engine_active)}), nothing to do")
        return "ok"
    in_session = is_trading_day(now.date()) and START <= now.time() < ENGINE_LAST_START
    wf, inputs, label = (ENGINE_WF, {"session": "auto"}, "engine") if in_session else (RELAY_WF, None, "relay")
    if dry_run:
        out(f"ensure_engine: [dry-run] would dispatch {wf} {inputs or ''}".rstrip())
        return label
    try:
        dispatch(api, repo, wf, inputs)
    except Exception as e:  # noqa: BLE001
        out(f"ensure_engine: dispatch of {wf} FAILED: {e}")
        return "skip"
    out(f"ensure_engine: dispatched {wf} {inputs or ''} (IST {now:%H:%M})".rstrip())
    return label
