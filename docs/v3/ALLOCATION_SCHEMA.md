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
| `desks`, `default_at_write`, `prices_at_update` | objects | optional, informational (not read by the engine) |
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
   sells and their reinvestment are exempt) -> 3-point rebalance band. A cap never pushes a weight below the default.
4. One `ALLOC` record per daily check in `logs/decisions-D.jsonl` (default, proposal, k, every stage, clamps, executed).
5. Shadow (shipped: `allocation.shadow: true`): the v2 decision trades and the v3 target is only logged.

## Go-live fixes (review of the first engine PR)

- **Idle cash after risk sells.** Every sell except the discretionary "Rebalance" (silver/copper stops, fast lane, results exit,
  invalidation sell, trend exits) adds its proceeds, as a share of the book, to a running `v3.credit` in the runtime state. At the
  next check `clamp(credit=...)` does not count gold buys up to that credit against the 0.20/day 0.40/week turnover budget; the credit
  never exceeds the cash in the account and is zero at brake level 2 and under the account stop. Spent credit disappears.
- **Symbols that cannot be bought today** (stopped out or fast-laned today, or a routine `block` bias) never rise above their current
  weight (`no_add` clamp line); the weight they cannot take goes to the other symbols with room. A symbol whose proposal was
  invalidated is avoided first and used only as a last resort (`fill_last_resort`), so cash never idles because of it.
- **Vol guard never switches off silently.** Order of inputs: live EWMA covariance; the last good one in `v3.cov_last`
  (at most `cov_max_age_days`, ALLOC clamp `vol_guard_stale_cov`); then a stand-in built from per-asset volatilities at correlation
  `fallback_rho` (1.0, ALLOC clamp `vol_guard_fallback`). `alloc.vol.cov` in the ALLOC record says which one was used.
- **Ledger.** The 3-point dust sweep and the 09:30 retries of blocked buys are booked in `v3.ledger`; the exempt part of the sweep is
  also in `v3.dust` and bounded (`dust_day` 0.03, `dust_week` 0.06 over 5 sessions). The 5-session ledger total is therefore at most
  `max_turnover_week` plus the dust. While ramping in (idle cash above 10% of the book that is not risk-sell credit) the limits are
  `ramp_turnover_day` / `ramp_turnover_week` (default 0.20 / 0.40: about 11 sessions from all cash; the owner may raise them).
- **Caps** gold 1.00 / silver 0.50 / copper 0.25 bind the default (D5-FI) as well, under v3 only.
- **Parsing.** `sanitize_allocation`, `validate_proposal` and `resolve_advice` never raise (huge integers, wrong types). A proposal
  written more than 48 hours ago is ignored (Saturdays and Sundays do not count) and the tool refuses to write one.
- **One switch.** `allocation.phase`: `off` / `shadow` (dormant under `v2_trend`), `live_default` (D5-FI trades, AI only logged,
  trust not used), `live` (AI tilt trades at `ai_trust`). `strategy.mode: v3_alloc` also turns v3 on; `shadow: false` there is read as
  `live_default`.
- **Routine bias.** In v3 live the guidance `block` only means no new buys/adds of that metal; `favour`/`neutral` do nothing.

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
python tools/guidance.py allocation hold-default --note "<why>"   # {"weights": "default", "confidence": 0}; also the DeepSeek fallback's only option
```

`set` fills `written` (now, IST), `by`, `default_at_write` and `prices_at_update` itself; `--weights` are shares (typed as given,
normalised by `sanitize_allocation`). It must not accept `--cash`, a size, a quantity or a symbol outside the three. It also
writes the derived `bias` into `state/guidance.json` for v2 rollback compatibility (weight more than 0.10 below the default means
`block`, else neutral), and never anything that adds exposure.
