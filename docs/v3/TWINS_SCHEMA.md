# state/twins.json (shadow paper twins): schema 1

Engine-owned, written by `simgold/twins.py` once a day at the 15:10 check, only when `twins.enabled: true`. Paper trading only; the twins never trade.
Atomic write, at most 32 KB. A file that is oversized, corrupt or invalid is moved to `state/twins.json.bad` (git-ignored) and the twins restart; a file
with a NEWER `schema` is left alone. `tools/twins.py show|summary` is read-only and never moves a file.

Keys read by other code (the report reader of PR #76, `simgold/reportblocks.py:twins_view`; it needs `MAX_TWINS >= 7`, which its branch has since 53cea341):

| key | meaning |
|---|---|
| `since` | ISO date of the first check (`start_day`) |
| `asof` | ISO date of the last check (`last_day`) |
| `twins` | `{id: {value (Rs), return_pct (percent, from that twin's OWN start), max_dd_pct (percent, 0 or negative), days, label}}` for T0..T6 that have started |
| `common_start` | `{date, base, peak, max_dd_pct, return_pct}`: every twin measured from one close, the latest start date of any twin (T2/T6 wait for a NIFTYBEES price) |
| `account_stop` | the Rs 75,000 hard stop in force (compare it to each twin's `books[id].start_value`) |
| `books` | the full ledgers (cash, whole units, peak, drawdown, fees `costs`, `traded`, `entry_loss`, `ledger`, `dust`, `brake`, `latched`) used by the engine hook and the tool |

Ids: T0 real-book mirror, T1 D5-FI default, T2 D5-FI with a 50/50 GOLDBEES+NIFTYBEES sink, T3 D5-FI without the silver trend rule, T4 D5-FI + gold-silver
tilt, T5 buy and hold GOLDBEES, T6 buy and hold 50/50 GOLDBEES+NIFTYBEES. The reader sorts the rows by id and names them from its own label map.
`logs/twins-YYYY-MM.csv` has one row per twin per check day (value, cash, weights, drawdown, days green, note).
