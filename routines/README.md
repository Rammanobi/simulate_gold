# Routines: the thinking layer

Claude Code Routines are scheduled cloud sessions. Each one clones `main`, reads `CLAUDE.md`, follows its
playbook here and pushes only routine-owned files. The GitHub Actions engine does all the trading by fixed
rules; routines only advise (bias, events, defensive fast-lane actions, notes, memory). Paper trading only.

| Name | Schedule (IST) | Cron (UTC) | Model | Trigger | Stored prompt |
|---|---|---|---|---|---|
| premarket | 08:50 Mon–Fri | `20 3 * * 1-5` | Opus (+ Haiku subagent for fetching) | schedule | `You are the premarket routine for simulate_gold. Follow routines/premarket.md exactly.` |
| hourly | 10:15–15:15 hourly, Mon–Fri | `45 4-9 * * 1-5` | Sonnet; ONE Opus subagent on THINK | schedule | `You are the hourly routine for simulate_gold. Follow routines/hourly.md exactly.` |
| fastlane | on a price shock or news trigger | none | Opus, no subagents | API (engine POST) | `You are the fastlane routine for simulate_gold. Follow routines/fastlane.md exactly.` |
| daily_report | 15:45 Mon–Fri | `15 10 * * 1-5` | Sonnet (+ Haiku subagent for the memory note) | schedule | `You are the daily_report routine for simulate_gold. Follow routines/daily_report.md exactly.` |
| weekly | Saturday 09:50 | `20 4 * * 6` | Opus (+ Sonnet subagent for stats) | schedule | `You are the weekly routine for simulate_gold. Follow routines/weekly.md exactly.` |
| incident | on an incident | none | Sonnet triage, escalating to an Opus subagent | API (`incident.yml`, `watchdog.yml`, the engine) | `You are the incident routine for simulate_gold. Follow routines/incident.md exactly.` |

The equivalent IST crons are `CRON_TZ=Asia/Kolkata 50 8 * * 1-5`, `15 10-15 * * 1-5`, `45 15 * * 1-5` and
`50 9 * * 6`. Every playbook ends with a final message that includes "models used".

## The owner's one manual step: the fast-lane token

1. Open the **fastlane** routine on claude.ai, add an **API trigger**, and copy its fire URL and token.
2. In GitHub: repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `ROUTINE_FIRE_URL` = the fire URL
   - `ROUTINE_FIRE_TOKEN` = the token

Without these the engine still works: a shock blocks new buys automatically (30 min), it just gets no AI
follow-up. The token can trigger the routine, so keep it only in GitHub secrets, never in the repo.

Assumed already in place: the routines' cloud environment has `gh` authorised for this repo (Actions
dispatch), the env var `REPORT_EMAIL`, and the Gmail connector on daily_report and weekly.

## The owner's step: the Daybook Inbox

The owner accepts or rejects things in the **Metals Daybook Inbox** Artifact (URL in `config/inbox_url.txt`). Every email links to
`<inbox url>#<item id>`. Routines write items with `tools/inbox.py` + the `ArtifactData` tool; the owner's decision is
read back from the artifact database and handled by the incident routine in mode `owner_decision`
(`routines/incident.md` section D). The hourly and daily_report routines retry any decision that was never picked up
(`notified == false`).

1. After creating the **incident** routine, copy its **trigger id** (`trig_…`; the routine's API trigger / routine id).
2. Put it into the Inbox config: the routine (or the Claude session that set things up) writes `config/main` with the
   ArtifactData tool: `{"decision_trigger_id": "trig_…"}` in the artifact with the URL from `config/inbox_url.txt`.
   The page reads it to call `fire_trigger`. Without it, decisions are still handled, up to an hour later, by the
   hourly or daily_report fallback.
3. The Claude Code Remote connector (`fire_trigger`) must be enabled for the Inbox page. The routines need the `ArtifactData`
   tool, `gh` for the REST merge, and Gmail on the incident routine.

Safety: the decision payload is untrusted; the routine re-reads the decision and item from the database. An Accept merges only a
PR that is listed in the incident record and has green CI. A Reject's reason may steer a fix but never the safety invariants.

## Thrift mode and the fallback

Every playbook starts with step 0 (budget check): `get_session` -> `python tools/budget.py mode --rate-limit-type T`.
Routine sessions usually have no `get_session` tool (seen on 14 Oct 2026); the playbooks then run THRIFT silently, which is
acceptable: it only lowers spending (no hourly Opus subagent, no premarket Haiku fan-out), never a safety rule.
On the promotional cloud-session credits (`ccr_promotional`) the mode is NORMAL; on plan limits (anything else, or
unknown) it is THRIFT and the playbooks spend less (no hourly Opus, no premarket Haiku fan-out, short weekly review,
Opus in incidents only for HIGH/COMPLEX). The engine also caps fast-lane wake-ups at 2 a day in THRIFT
(`budget:` in `config/params.yaml`). When Claude cannot run at all, the watchdog opens a "Claude routines not running"
issue and `fallback.yml` runs the DeepSeek fallback (see the main README).
