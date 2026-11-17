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
  (handing over to a fresh run when its 5h45m budget is used up) and re-checks at 09:30, 12:20, 12:30, 13:30, 14:30 and 15:20 IST,
  dispatching the engine again if none is active. After the 15:20 check it dispatches the next relay, so a relay or an engine run
  exists from 08:55 to the next morning. A run that starts late catches up with one immediate check.
- At the on-time 08:55 event it also dispatches `morning-email.yml`, only if that file exists and `report.morning_email` is true
  (default false). A catch-up start (a later hop, a restart) never sends it, so the chain cannot mail twice.
- **Hand-over:** after 15:25 IST (or on a day with no more sessions) the last step of `market-hours.yml` dispatches the relay.

## Restart sources (one broken hop must not kill the chain)
- Overnight crons in `engine-relay.yml` (21:00, 23:00, 03:00, 07:00 IST); a run exits at once if another relay is in progress.
- `tools/ensure_engine.py` (idempotent, `--dry-run`) runs in `report-retry.yml`, `fallback.yml` and `reminders.yml`: relay or engine
  active -> nothing; trading day 08:55-15:25 IST with no run -> dispatch `market-hours.yml`; otherwise dispatch the relay.
- `watchdog.yml` dispatches the relay when `relay_missing` is found (and the engine on `engine_run_missing`, as before).

## Dry run
Local and read-only (lists runs, dispatches nothing, virtual clock, no sleeping):
`GH_TOKEN=$(gh auth token) GITHUB_REPOSITORY=Rammanobi/simulate_gold python tools/engine_relay.py --dry-run [--until 2026-11-16T08:55]`
`--until` (IST if no zone; 5 min ago to 7 days ahead, else exit 2) replaces the 08:55 start; re-checks keep their offsets.
Same for `python tools/ensure_engine.py --dry-run`. The Actions form is
`gh api -X POST repos/Rammanobi/simulate_gold/actions/workflows/engine-relay.yml/dispatches -f ref=main -f 'inputs[dry_run]=true'`;
a `dry_run` or `until` run is a TEST run: own concurrency group `engine-relay-test`, run-name `engine-relay TEST`, never counted as the
live chain, never hands over. It lists runs for real, and an `until` run that is not a dry run does dispatch the engine.

## Alerts (tools/healthcheck.py; both are in `alerts.SAFE_MODE_EXEMPT`: reported and emailed, never switch safe mode on)
- `session_missed` (HIGH): trading day, after 15:40 IST, `logs/ticks-DATE.csv` missing or under 100 rows. The engine did not run
  (most of) today's session. Check the market-hours runs, dispatch `market-hours.yml` by hand if the day is still open.
  Checked for the last trading day that should be over, so a run after midnight or at a weekend still sees it. Claude-free path (the
  routines were the silent party): the watchdog opens a GitHub issue (label `session-missed`) and `tools/alert_email.py` sends one plain
  email per day to `REPORT_EMAIL` ("Metals Daybook · ALERT · no trading session today"; also in `reminders.yml`).
- `relay_missing` (MEDIUM): between 16:30 IST and the next trading-day 08:50 IST no engine-relay run is queued or in progress, so
  nothing will start tomorrow's session (the detail names the last run's conclusion, e.g. failure). The watchdog re-dispatches the relay itself; if the alert stays, dispatch it by hand.

## Limits
Public-repo Actions minutes are free; a weekend is about 12 hops. If GitHub is down, nothing here helps.
- `relay_check_unknown` (LOW): the relay run list could not be fetched; safe-mode exempt like the two above.
