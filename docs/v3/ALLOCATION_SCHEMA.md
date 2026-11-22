# `state/allocation.json`: schema and engine interface (strategy v3, dormant)

Paper trading only. This is the contract between the routine tools (`tools/guidance.py allocation ...`, `tools/desk.py`) and the
engine (`simgold/allocation.py`). The engine only reads this file; routines write it only through the tool. Nothing in it can
buy more than the engine's hard limits allow: it is advice that tilts the engine's own default (D5-FI), never an order.

Owner decisions this reflects (17-18 Nov 2026): profit is the only objective; nothing idles in cash; weights are SHARES that
sum to 1.00 over the three symbols and there is no cash field; the engine scales to `invested_target` (0.98 minimum, aims for
`invested_max` 1.00) and reinvests dust in GOLDBEES; DeepSeek never writes allocations; the AI tilt starts at trust 0 (shadow).

## File

```json
{
  "for_date": "2026-11-18",
  "written": "2026-11-18T14:52:10+05:30",
  "by": "allocation",
  "phase": "final",
  "weights": {"GOLDBEES": 0.70, "SILVERBEES": 0.20, "HINDCOPPER": 0.10},
  "confidence": 0.55,
  "horizon_days": 5,
  "scenarios": [
    {"name": "base", "p": 0.50, "GOLD": "flat", "SILVER": "flat", "COPPER": "down", "why": "DXY 104, no Fed event"},
    {"name": "bull", "p": 0.25, "GOLD": "up",   "SILVER": "up",   "COPPER": "flat", "why": "soft CPI 12 Dec"},
    {"name": "bear", "p": 0.25, "GOLD": "down", "SILVER": "down", "COPPER": "down", "why": "US10Y above 4.6%"}
  ],
  "rationale": {"GOLD": "DXY 104, real yield 1.9 (investing.com)", "SILVER": "...", "COPPER": "..."},
  "invalidate": ["close_below:SILVERBEES:158"],
  "desks": {"macro": "ok", "gold": "ok", "silver": "ok", "copper": "missing", "risk": "ok", "memory": "ok"},
  "default_at_write": {"GOLDBEES": 0.98, "SILVERBEES": 0.0, "HINDCOPPER": 0.0},
  "prices_at_update": {"GOLDBEES": 110.1, "SILVERBEES": 160.5, "HINDCOPPER": 430.0},
  "budget": "THRIFT",
  "note": "optional one line"
}
```

Hold-default (an explicit "no view"; the only thing the DeepSeek fallback may ever write):

```json
{"for_date": "2026-11-18", "written": "2026-11-18T14:52:10+05:30", "by": "allocation", "phase": "final",
 "weights": "default", "confidence": 0, "note": "no usable research today"}
```

## Fields

| Key | Type | Rule |
|---|---|---|
| `for_date` | ISO date | required. The trading day the proposal is for. The engine uses it only when it equals today, or (FINAL only) as yesterday's carry for `carry_days` trading days at half confidence |
| `written` | ISO timestamp with offset | required (`created` is accepted as an alias). Not more than 5 minutes in the future, not after its `for_date` |
| `expiry` / `expires` | ISO timestamp | optional; the proposal is ignored after it |
| `by` / `source` | string, at most 60 chars | optional, default empty |
| `phase` | `provisional` or `final` | default `final`. Provisional confidence is multiplied by `allocation.provisional_conf_mult` (0.8). The tool refuses a FINAL for today at or after 15:05 IST |
| `weights` | object or the string `"default"` | all three of `GOLDBEES`, `SILVERBEES`, `HINDCOPPER`, each a number in [0, 1], no other key. They are SHARES of the invested book and must sum to 1.00 within 0.02 (sums in [0.98, 1.02] are normalised to exactly 1.00; below 0.98 is refused as hidden cash, above 1.02 as leverage). `"default"` means hold-default (confidence 0) |
| `confidence` | number in [0, 1] | required. Tilt strength is `k = ai_trust * clip((confidence - 0.30) / 0.50, 0, 1)`; hold-default is stored as 0 |
| `horizon_days` | number 1..30 | optional |
| `scenarios` | list of 2 to 4 | required with real weights: `{name (<= 60 chars), p in [0,1], GOLD|SILVER|COPPER in up/flat/down (each optional), why (<= 240 chars, optional)}`; the `p` values sum to 1 within 0.02 |
| `rationale` | object | optional: keys `GOLD`, `SILVER`, `COPPER` only; each a string of at most 240 chars. The tool also requires a number in each (a level, a percentage or a date) |
| `invalidate` | list of at most 8 strings | each exactly `close_below:SYMBOL:price` (symbol one of the three, price a plain decimal). Two consecutive fresh intraday prices below the level sell the part above the default (a risk sell); the daily check drops the advice for that symbol |
| `desks`, `default_at_write`, `prices_at_update` | objects | optional, informational (not read by the engine). `desks` values: `ok`, `missing`, `abstain`, `invalid`; the memory desk can also be `degraded` (written without price history; it does not count for the quorum) |
| `budget` | string | optional, informational (`NORMAL` or `THRIFT`) |
| `note` | string | optional, informational |

Forbidden (the whole file is rejected, the default runs): any key outside the table (so `cash`, `qty`, `size`, `order`, `leverage`,
`stop`, `floor` cannot even be written), a weight for `LIQUIDBEES` or any other symbol, a negative or non-numeric weight, a boolean
or NaN anywhere a number is expected, a file over 32 KB. Text fields: no instruction-like text, no URLs (`simgold/memsafe.py`).

## What the engine does with it (`simgold/allocation.py`)

1. `load_proposal` / `resolve_advice`: valid today FINAL x1 or PROVISIONAL x0.8; else yesterday's valid FINAL at half confidence
   for `carry_days` (1) trading day; else none. None, invalid, stale and future files all mean "the default runs", logged.
2. `default_d5fi`: the v2 trend rules with the invested floor raised to `invested_max`; every leftover share goes to GOLDBEES.
3. `clamp`: tilt (<= `max_tilt` 0.30 from the default, strength `k`) -> caps (gold 1.00 / silver 0.50 / copper 0.25; off-trend
   1.00 / 0.20 / 0.10, halved when DEFENSIVE or the thesis is cautious; results window 0) -> fill to the invested level (sink
   GOLDBEES) -> portfolio vol guard (0.30, or 0.24 while the account is under the starting capital; moves weight to GOLDBEES,
   never to cash) -> brakes (level 1 at -15%: silver and copper x0.5, the freed weight rotates to GOLDBEES, no cash; level 2 at
   -25% and the 75,000 account stop: all cash, latched) -> turnover limiter (0.20 a day, 0.40 over 5 sessions, one-way; risk
   sells, and the cash from today's risk sells re-invested in GOLDBEES only, are exempt) -> 3-point rebalance band. A cap never pushes a weight below the default.
4. One `ALLOC` record per daily check in `logs/decisions-D.jsonl` (default, proposal, k, every stage, clamps, executed).
5. Shadow (shipped: `allocation.shadow: true`): the v2 decision trades and the v3 target is only logged.

## Go-live fixes (reviews of PRs 67 and 73)

- **Precedence of the switches (one source of truth).** `allocation.phase` decides when it is present; the legacy `shadow:` key is
  read only when `phase` is absent. `phase: off` or `shadow` never trades v3 (not even with `strategy.mode: v3_alloc` and `shadow: false`);
  `live_default` and `live` trade v3 whatever `strategy.mode` says (except `v1_scores`). `strategy.mode: v3_alloc` with `shadow` or an
  absent phase logs only; with `off` v3 is not even computed. The test matrix covers all 3 modes x 6 phases x 3 legacy values.
- **What other code sees (`strategy.mode` for tools and routines).** `simgold.config.load_params()` loads a `v2_trend` config whose phase
  is `live_default` or `live` as `strategy.mode: v3_alloc`, so `tools/context.py`, `tools/desk.py` and any command that reads the mode
  through `simgold.config` see `v3_alloc` plus the phase. A command that reads `config/params.yaml` with `yaml.safe_load` directly still
  prints the raw `v2_trend`; the routines' MODE command must use the loader instead:
  `python -c "from simgold import config;c=config.load_params();a=c.get('allocation') or {};print(c['strategy']['mode'],a.get('phase'),a.get('ai_trust'),(a.get('desks') or {}).get('fanout'),a.get('invested_target'))"`
  (same output format). Until the playbooks use it, the safe go-live commit sets BOTH `allocation.phase: live_default` and
  `strategy.mode: v3_alloc`.
- **Idle cash after risk sells (credit).** Every sell except the discretionary Rebalance, the brake sells, the account stop and the v1 tripwire
  adds its proceeds, as a share of the book, to `v3.credit = {day, amt}`. It is valid for that trading day only, never exceeds the cash in
  the account, and is zero and deleted at brake level 2 and under the account stop (so a brake release re-enters under the normal day
  limit, not all at once). At the next check `clamp(credit=...)` does not count GOLDBEES buys funded by it against the 0.20/0.40 budget.
  **Only the GOLDBEES sink** (owner decision): a fast-lane gold sale never becomes exempt silver or copper buys; every non-gold buy,
  a metal back up to its default included, goes through the normal turnover budget and is booked. The credit never funds an AI tilt.
  Spent credit is subtracted after use (`v3_spend_credit`, from the GOLDBEES buys executed), so a same-day re-run or a second check
  never exempts the same cash twice; the pure clamp reports `credit_used` for the caller to subtract.
- **Blocked symbols.** Symbols that cannot be bought today (stopped out or fast-laned today, or a routine `block` bias) never rise above their
  current weight (`no_add`). The leftover goes to the GOLDBEES sink; if gold is the blocked one it goes to other metals only up to their default
  weight, otherwise it waits as cash. A symbol whose proposal was invalidated is avoided first and used only as a last resort
  (`fill_last_resort`). The dust sweep also respects a `block` on GOLDBEES, so with all three blocked nothing is bought.
- **Vol guard never switches off silently.** Inputs in order: live EWMA covariance; the last good one in `v3.cov_last` (at most
  `cov_max_age_days`, ALLOC clamp `vol_guard_stale_cov`); a stand-in from per-asset volatilities at correlation `fallback_rho` (1.0,
  `vol_guard_fallback`). `alloc.vol.cov` in the ALLOC record says which. When GOLDBEES cannot take weight (blocked today, no room) and
  the limit is still broken, the riskiest symbols are reduced and the weight waits in cash (`vol_guard_no_sink`); a cut that would raise
  the volatility (a hedge) is skipped. Frozen (no data) weights count in the volatility.
- **Ledger.** All turnover is booked in `v3.ledger`: the first full check's plan, the 09:30 retries, 15:11+ re-checks and exits-only trims
  (the larger of the non-exempt buys and the discretionary sells). The dust sweep is strictly dust: at most `dust_day` 0.03 a day and
  `dust_week` 0.06 over 5 sessions (also in `v3.dust`), never the remaining turnover budget. The 5-session ledger total is at most
  `max_turnover_week` plus the dust. While ramping in (idle cash above 10% of the book that is not credit) the limits are
  `ramp_turnover_day` / `ramp_turnover_week` (default 0.20 / 0.40: about 11 sessions from all cash; the owner may raise them).
- **Caps** gold 1.00 / silver 0.50 / copper 0.25 bind the default (D5-FI) as well, under v3 only.
- **Parsing and failures.** `sanitize_allocation`, `validate_proposal` and `resolve_advice` never raise (huge integers, wrong types).
  `clamp` never raises either: an internal failure (for example an overflow from a huge credit) returns the repaired default (no AI, no
  credit, zero under the stop or level 2) with a `clamp_failed` note. A proposal written more than 48 hours ago is ignored (weekends do
  not count) and the tool refuses to write one.
- **Brake level 1 with GOLDBEES blocked.** The rotation into gold is not shown or traded when gold cannot be bought today: silver and
  copper are cut, gold is kept, the freed weight waits in cash (clamp note `brake_rotation_blocked`; idle cash under a block is accepted).
  The ALLOC record therefore shows the executed target.
- **If the pipeline fails.** `clamp` re-runs once without covariance, credit and the original brake inputs (note `clamp_retry`; every limit
  still applies). Only if that fails too is the repaired default returned, with the turnover limit applied to it, zero under the stop or
  level 2 (note `clamp_failed`).
- **Routine bias.** In v3 live the guidance `block` only means no new buys or adds of that metal; it never moves weight into another
  metal above its default. `favour` and `neutral` do nothing.

## What the evidence says (read this before `phase: live`)

Honest status, from `python run_backtest.py --strategy v3 --data-dir <research data>` (daily bars, costs and idle cash at 0% as shipped):

| run | CAGR full (2010-06 to 2026-11) | max DD full | CAGR last 2 y | max DD last 2 y |
|---|---|---|---|---|
| v2 as shipped (50% gold floor) | 14.8% | -21.6% | 44.6% | -21.6% |
| v3 default, fully invested, no AI | 13.3% | -30.9% | 41.6% | -26.1% |
| v3 + seeded random "noise AI" (no skill) | 6.9% | -47.3% | 35.5% | -25.1% |
| buy and hold GOLDBEES | 11.5% | -27.4% | 32.3% | -25.0% |

- **Being fully invested is not a profit edge when idle cash earns nothing.** The v3 default earns about the same as v2 or a little
  less, with a deeper drawdown (about 4 points deeper over the last 2 years, 9 points over the full sample). What it buys is exposure to
  the metals when the trend is OFF, which was paid for in drawdown, not return. (The review's own runs gave about 41.5% against 43.5% over
  2 years and about 9 points deeper drawdown; the exact figures move a little with the data file.)
- **A random AI does badly** (6.9% a year, -47% drawdown over the full sample, about 30 trades a month): the clamp bounds the damage, it
  does not make a skill-less tilt profitable. AI tilts need MEASURED skill (shadow ALLOC logs, the evals and Brier criteria in
  `docs/v3/ROLLOUT.md`) before `phase: live` and `ai_trust > 0`.

**Simulation of the first live_default day, from the review (not re-run here):** flipping on Monday 23 Nov buys 211 GOLDBEES on the first
check, reaches about 90% invested by 25 Nov and 99.9% by 1 Dec under the 0.20/day 0.40/week limits, costs about Rs 27, and ends 100%
GOLDBEES while the gold trend is OFF (the silver and copper trends are OFF too). So the first weeks of P1 are a gold-only book; the owner
should expect that, and can speed or slow the ramp with `ramp_turnover_day/week`.

## Python interface for the tools (all pure; import from `simgold.allocation`)

```python
settings(p=None) -> dict                      # the `allocation:` block with defaults and bounds (p = full params or the block)
sanitize_allocation(obj, cfg=None, now=None, *, for_write=False) -> (clean | None, [reasons])
        # THE filter. clean is the object to write: whitelisted keys, weights rounded to 4 places summing to exactly 1.00 (or
        # "default" with confidence 0). for_write=True with an IST-aware `now` adds the write-time rules (written not in the future,
        # for_date today..+5 days, no FINAL for today from 15:05, memsafe text, a number in every rationale). Never raises.
validate_proposal(obj, cfg=None) -> (normalised | None, reason | None)   # structural part only (what the engine applies on read)
resolve_advice(obj, now, cfg, tdays=None) -> (Advice | None, [notes])    # the engine's reading of a proposal at `now`
load_proposal(path, now, cfg, tdays=None) -> (Advice | None, [notes])    # same, from state/allocation.json; never raises
default_d5fi(infos, st, cfg, ineligible=()) -> ({sym: w}, detail)        # infos = {sym: (trend_on, sigma60)}; st = params["strategy"]
clamp(*, default, advice, confidence, current, on, cfg, cov=None, halve=(), zero=(), ineligible=(), invalidated=(),
      brake_level=0, brake_mult=0.5, halted=False, day_left=inf, week_left=inf, underwater=False, frozen=None,
      notes=(), advice_source=None) -> AllocResult      # .final, .stages, .clamps, .caps, .as_record()
tilt_strength(ai_trust, confidence) -> float
ewma_cov(series, span=60, min_obs=20), port_vol(w, cov), turnover_left(ledger, today, cfg, prev_td=None)
guard.sanitize_allocation(obj, now, p=None, tdays=None) -> (Advice | None, [reasons])   # same filter, engine-style, in guard.py
```

## CLI the tools builder should offer (`tools/guidance.py allocation ...`)

All writes go through `sanitize_allocation(..., for_write=True)`; on refusal exit code 2, print the reasons, write nothing.
Success prints the clamped preview (never refuses on the preview).

```
python tools/guidance.py allocation set --weights GOLDBEES=0.70 SILVERBEES=0.20 HINDCOPPER=0.10 --confidence 0.55 \
    --scenario "base:0.50:GOLD=flat,SILVER=flat,COPPER=down:DXY 104" (x2..4) \
    --rationale GOLD="..." SILVER="..." COPPER="..." [--invalidate close_below:SILVERBEES:158 ...] \
    [--horizon-days 5] [--for-date YYYY-MM-DD] --phase provisional|final [--desks macro=ok ...] [--note "..."]
python tools/guidance.py allocation explain [--file PATH]   # read-only: what the engine would trade after tilt, caps, vol, brakes, turnover
python tools/guidance.py allocation show                    # the file as the engine reads it (valid today / carry / ignored, and why)
python tools/guidance.py allocation hold-default [--phase provisional|final] --note "<why>"   # {"weights": "default", "confidence": 0}; --phase defaults to final; also the DeepSeek fallback's only option
```

`set` fills `written` (now, IST), `by`, `default_at_write` and `prices_at_update` itself; `--weights` are shares (typed as given,
normalised by the tool and by `sanitize_allocation`). It must not accept `--cash`, a size, a quantity or a symbol outside the three.

Tool rules that sit on top of the engine's filter:

* **Weights** total 1.00 within 0.02: refused below 0.98 and above 1.02, aim for exactly 1.00. The tool is tolerant of float rounding at the
  boundary (`0.70 + 0.20 + 0.08` is `0.9799999999999999` in floating point and is accepted) and normalises a total that is not exactly 1.00.
  A NaN, an infinity or a huge number is a refusal, not a traceback.
* **`--invalidate`** takes ONLY `close_below:SYMBOL:price`, up to 8 entries (the engine's one enforced form); `close_above`, `regime_is`,
  `date` and any other form are refused. Dates are written exactly `YYYY-MM-DD`.
* **`for_date`** defaults to today before 15:30 IST on a trading day, else the next trading day, and is at most 5 calendar days ahead.
* **Role prefixes**: text (note, rationale, scenario why, `--by`) with a line starting `system:`, `assistant:`, `user:` (and similar) is refused.
* **Shadow rule (no effect on trades).** While the proposal is logged only (`strategy.mode` not `v3_alloc`, or `allocation.shadow` true, or
  phase `shadow`, or `ai_trust` 0) the tool writes `state/allocation.json` and the ledger row and NOTHING in `state/guidance.json` (it
  prints `guidance.json not touched`). The engine in `v2_trend` never reads `state/allocation.json`, and in shadow v3 it only logs the
  would-have result, so the v2 rule trades identically with and without a proposal file; the only bias it sees is one set explicitly with
  `guidance.py set` (`tests/test_v3_shadow_isolation.py` replays the stored ticks both ways).
* **Derived bias (live only).** When the tilt trades live (`v3_alloc`, shadow false, phase live, `ai_trust` above 0) the tool also writes a
  `bias` for v2 rollback compatibility: never `favour` (an existing favour becomes neutral); an existing `block` is kept (the derived bias
  never lowers or lifts one: the stricter of the existing and the derived wins); a SILVER or COPPER weight more than 0.10 below the default
  adds a `block`; GOLD is never blocked (GOLDBEES is the sink). Nothing that adds exposure is ever written.
* **Calibration ledger** `memory/ltm/calibration.json`: `set` and `hold-default` write the synthesis row (one per `(for_date, phase)`,
  idempotent). Only the premarket, allocation and fast-lane sessions run them, so they are the single writers and stage the file. Desk calls are
  NOT recorded by `tools/desk.py`; `tools/calibration.py` takes them from the committed `analysis/D/<desk>.json` files when it scores.
