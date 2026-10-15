# Backup email setup

Paper trading only; no real money. The daily report email is normally sent at 15:45 IST by the Claude
`daily_report` routine through the Gmail connector. When Claude is unavailable (usage limit), `report-retry.yml`
retries it up to 8 times (30 min, 60 min, then every 2 h). If the 8th attempt was fired more than 90 minutes ago and
the report is still not pushed, the same workflow sends a **backup email** once:

- `tools/backup_email.py` builds the normal report with Python (numbers from the logs, no AI) and sends it with
  Gmail SMTP (`smtp.gmail.com:465`, SSL) to the single address in `REPORT_EMAIL`.
- Subject: the normal one plus ` · backup (Claude unavailable)`; it always starts with "Metals Daybook".
- If `DEEPSEEK_API_KEY` is set and Claude wrote no market notes for that day, a short DeepSeek summary paragraph
  (plain text, every number checked against the facts) is put in a labelled banner at the top. If DeepSeek fails,
  the deterministic report still goes.
- `state/report_retry.json` then gets the status `backup_sent`, so it is never sent twice.
- Addresses and the password are never printed or committed; logs show the recipient masked (first letter + `***`).

## Owner steps (one time)

1. On the Google account that will **send** the email, turn on **2-Step Verification**
   (Google Account, Security, 2-Step Verification). App passwords need it.
2. Create an **App password** at https://myaccount.google.com/apppasswords (any name, for example "simgold backup").
   Google shows a 16-character password once; copy it.
3. Add three repository secrets at https://github.com/Rammanobi/simulate_gold/settings/secrets/actions
   (New repository secret):
   - `SMTP_USER`: the sending Gmail address (the account from step 1)
   - `SMTP_APP_PASSWORD`: the 16-character app password from step 2 (spaces are fine)
   - `REPORT_EMAIL`: the one address that receives the report (it may be the same Gmail address)
4. Optional: `DEEPSEEK_API_KEY` (already used by `fallback.yml`) adds the summary paragraph.

Never put any of these values in a file in the repository: it is public.

Without the three secrets the workflow writes "Backup email not configured: add SMTP_USER, SMTP_APP_PASSWORD,
REPORT_EMAIL secrets" to the run summary and does nothing else.

## Test it

Actions, **report-retry**, Run workflow: set `date` to a trading day (YYYY-MM-DD) and tick `force_backup`. The backup
email for that day is sent at once (same safety checks) and the run summary shows the subject, the masked recipient
and the sizes. Locally, `python tools/backup_email.py --date D --dry-run` builds it and sends nothing.

Note: `fallback.yml` has its own optional SMTP path that uses different secrets (`SMTP_HOST`, `SMTP_PASS`); setting
only the three secrets above does not turn that one on.
