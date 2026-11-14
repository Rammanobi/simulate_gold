# Engine relay

**Why:** on Thu 12 Nov 2026 (a trading day) the engine never ran in market hours. Every starter depended on a Claude routine
(premarket, hourly, guidance push) or on a GitHub `schedule` cron; the routines were silent and the crons ran ~8 h late.
`workflow_dispatch` made with the job's `GITHUB_TOKEN` starts within seconds, so the relay uses only that.

## What it does
- `.github/workflows/engine-relay.yml` runs `tools/engine_relay.py` (logic: `simgold/relay.py`). It never trades, never commits.
- It sleeps, at most 5h45m per run, until 08:55 IST of the next trading day (weekends and NSE holidays skipped via
  `simgold.market.is_trading_day`). If that is further away it dispatches the next relay run and ends (at most one pending relay;
  its own run id is excluded from the check). Own concurrency group `engine-relay`, never `paper-trader`.
- At 08:55 IST it dispatches `market-hours.yml` (`session=auto`) if no market-hours run is queued or in progress, then stays alive
  and re-checks at about 09:30 and 12:20 IST (dispatching again if none is active). It ends after the 12:20 check.
- At 08:55 it also dispatches `morning-email.yml`, only if that file exists and `report.morning_email` is true (default false).
- **Hand-over:** after 15:25 IST (or on a day with no more sessions) the last step of `market-hours.yml` dispatches the relay.

## Restart sources (one broken hop must not kill the chain)
- Overnight crons in `engine-relay.yml` (21:00, 23:00, 03:00, 07:00 IST); a run exits at once if another relay is in progress.
- `tools/ensure_engine.py` (idempotent, `--dry-run`) runs in `report-retry.yml`, `fallback.yml` and `reminders.yml`: relay or engine
  active -> nothing; trading day 08:55-15:25 IST with no run -> dispatch `market-hours.yml`; otherwise dispatch the relay.
- `watchdog.yml` dispatches the relay when `relay_missing` is found (and the engine on `engine_run_missing`, as before).

## Dry run
`gh api -X POST repos/Rammanobi/simulate_gold/actions/workflows/engine-relay.yml/dispatches -f ref=main -f 'inputs[dry_run]=true'`
Uses a virtual clock (no real sleeping) and prints every dispatch it would make; nothing is dispatched. Optional
`-f 'inputs[until]=2026-11-16T08:55'` replaces the next start (re-checks keep their offsets). Locally: `python tools/engine_relay.py --dry-run`,
`python tools/ensure_engine.py --dry-run` (needs `GH_TOKEN`).

## Alerts (tools/healthcheck.py; both are in `alerts.SAFE_MODE_EXEMPT`: reported and emailed, never switch safe mode on)
- `session_missed` (HIGH): trading day, after 15:40 IST, `logs/ticks-DATE.csv` missing or under 100 rows. The engine did not run
  (most of) today's session. Check the market-hours runs, dispatch `market-hours.yml` by hand if the day is still open.
- `relay_missing` (MEDIUM): between 16:30 IST and the next trading-day 08:50 IST no engine-relay run is queued or in progress, so
  nothing will start tomorrow's session. The watchdog re-dispatches the relay itself; if the alert stays, dispatch it by hand.

## Limits
Public-repo Actions minutes are free; a weekend is about 12 hops. If GitHub is down, nothing here helps.
