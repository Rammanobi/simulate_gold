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
