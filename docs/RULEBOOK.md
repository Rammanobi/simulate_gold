# simulate_gold: Rulebook (v1 Green Guard rules + v2 trend core)

**This is paper trading. No real money is used.**
Starting capital: ₹1,00,000. We trade GOLDBEES, SILVERBEES and HINDCOPPER on NSE. Any money not in a trade sits as **plain cash** in the account and earns nothing (owner decision, 22 Oct 2026; `capital.idle: cash`). Buys pay from cash and sales go back to cash, at the exact fill price shown on every line. One-line rollback to the old behaviour: `capital.idle: liquidbees` (idle money parked in LIQUIDBEES, which earns the modelled yield). Wherever this rulebook says LIQUIDBEES below, read "cash" while `capital.idle` is `cash`; a LIQUIDBEES balance left over at the switch is moved to cash once, at no cost, and the value does not change.

> **Which rules are active?** `strategy.mode` in `config/params.yaml`. **`v2_trend`** (the section right below, the D5 "stay invested" profile) is the active strategy.
> **`v1_scores`** is the older Green Guard design, sections 0 to 10 further down; they are unchanged and apply only when the mode is
> `v1_scores`. Rollback is one line: set `strategy.mode: v1_scores`. Under v2 the sections about the floor, the budget/sizing rule,
> the entry score, daily limits, the overnight check and the month-end rules (1, 2, 3.1 to 3.4, 4, 7) are **not used**; sections 5
> (fast lane), 6 (event days) and 8 apply as described under "What stays" below.

## Strategy v2 (growth, aggressive)

**Goal: grow profit over months.** Success is judged monthly against buy-and-hold GOLDBEES. A green month end is no longer required.
Paper trading only. The owner chose the **AGGRESSIVE** risk profile; every number is in `config/params.yaml` under `strategy:`.
The code is `simgold/trend.py` (pure rules), the engine's daily check (`simgold/engine.py`) and the backtest (`python run_backtest.py --strategy v2`).

### V2.1 Trend state (from completed daily closes, plus today's live price as the provisional close)

| Symbol | ON when | OFF when |
|---|---|---|
| GOLDBEES | close > SMA200 × 1.005 | close < SMA200 × 0.995 (in between the previous state is kept: a 0.5% hysteresis band) |
| SILVERBEES, HINDCOPPER | close > highest high of the prior 55 days | close < max(peak close since entry − 3 × ATR20, lowest low of the prior 20 days) |

### V2.2 Target weight (D5 "stay invested")

1. `w_i = on_i × min(cap_i, vol_target / σ60_i)` with `vol_target` **0.14** (do not raise it), caps **GOLDBEES 100%, SILVERBEES 70%, HINDCOPPER 35%**.
   σ60 is the annualised 60-day close-to-close volatility (EWMA span 60, as in the research). If Σw > 1 every weight is scaled down proportionally (no leverage).
   Example: GOLDBEES close 121.4 > SMA200 118.9, σ60 0.161 gives a target of 87% = min(1.0, 0.14/0.161).
2. **Stop moves to gold** (`stop_to_gold`): a SILVERBEES or HINDCOPPER that is OFF hands its vol weight `min(cap, 0.14/σ60)` to GOLDBEES,
   but only while gold's own trend is ON (`stop_to_gold_requires_gold_trend`).
3. **Invested floor** (`min_invested` 0.50, `floor_symbol` GOLDBEES): GOLDBEES is raised so that metals are at least 50% of the portfolio,
   **even when gold's own trend is OFF** ("GOLDBEES floor: metals 0% < min_invested 50% → target 50% gold (trend OFF, floor holding)").
   If the total is over 100% everything is scaled down together. The rest is cash (LIQUIDBEES only when `capital.idle: liquidbees`). Set `min_invested: 0` and `stop_to_gold: false` for plain v2.
4. Rebalance only when |target − current| is more than **5 percentage points** of portfolio value; a move from zero (minimum 1%) or to zero always trades.
5. Brakes (V2.4): level 1 (−15%) halves every weight but the gold floor is kept; level 2 (−25%) is flat, the floor included.

### V2.3 The daily check

From **15:10 IST** the engine checks each symbol as soon as it has a FRESH price (not older than 3 minutes); a stale or missing price waits and retries every minute until 15:25.

- **Entries and adds** go straight to the target (`entry.rsi_timing: false`, D5). With `rsi_timing: true` the earlier rule applies: an entry waits while RSI(2) > 90 and an add needs RSI(2) < 5; bias "favour" then only loosens the dip to RSI(2) < 15 and never removes it.
- **Trim**: a position more than 5 points over its target is sold down to the target.
- **Exit**: when the target is 0 (trend OFF and no floor), the **whole position is sold at once** at the check.
- **Exits are never missed**: after 15:25 the check still runs for every symbol not yet checked, exits and trims only (no buys), until the close; if a whole day was missed, the first tick of the next day does it.
- A buy paused by a news or shock block that ends before 15:25 stays pending and is retried.
- **Intraday trend-exit stop (D5-S, `strategy.stops`, owner approval 2026-10-29)**: for the symbols in `stops.intraday_exit` (**SILVERBEES, HINDCOPPER**) the exit level
  `max(peak close since entry − 3 × ATR20, lowest low of the prior 20 days)` is no longer only a daily-check rule. The level for the day is computed from completed bars and
  frozen at the first v2 tick (`exit_level` of the position shows it). **Two consecutive fresh prices (`confirm_ticks` 2) at or under it sell the whole position** at once
  (trigger "Trend exit"); a price above the level resets the count and a stale price neither counts nor resets. A gap-down open under the level sells as soon as the two ticks are in (`gap: sell`, no waiting for the close).
  The symbol is not bought back that day, and re-entry needs a close above the prior 55-day high (the existing entry rule). The freed weight goes to gold at the next 15:10 check (stop moves to gold); there is no intraday buy.
  The reason of every stop sale states the level, the two prices and `units × fill price = amount`. **GOLDBEES has no new stop**: its SMA200 exit stays at the 15:10 check only.
- Prices are closes or the live price. Yahoo opens are never used. Daily bars are filtered: a one-day spike that reverts is dropped, highs/lows are clipped to close ± 4 × the median range, forward-filled duplicate bars are dropped (logged).

### V2.4 Portfolio risk (replaces the Green Guard floor, gap sizing and the 30% cash rule)

- **Drawdown brake** on total value from its peak (peak starts at ₹1,00,000, advanced once per trading day):
  **−15%**: every target × 0.5 (gold floor kept), released above −13%.
  **−25%**: targets 0, everything sold (floor included). Release: value back above −15% (→ level 1 if still under −13%, else level 0), **or** after
  **20 trading days** at level 2 once at least one symbol has turned OFF and ON again and the account stop is not in force → **level 1 (half size) with the peak re-based to the current value**;
  that re-based level 1 goes to level 0 only after **20 more trading days and value more than 3% above the re-base**.
- **Hard account stop ₹75,000**: value at or under it **sells everything at once**, halts buys, raises a HIGH alert (`account_stop`) and **latches**.
  To resume, the owner commits `strategy.account_stop_clear_after: "YYYY-MM-DD"` (a date on or after the day it fired) in `config/params.yaml`; routines cannot clear it.
  `floor` in `state/portfolio.json` is this fixed number under v2.
- **AI stops**: a fast-lane `tighten_stop` is stored as a separate stop (only ever raised) and enforced every minute: **two consecutive fresh prices** (`confirm_ticks`) at or under it sell the whole position (on GOLDBEES only the units above the floor, see the next point). A v1 stop on an open position is inherited the same way at the first v2 tick.
  The trend exit level is shown as `exit_level`; for SILVERBEES / HINDCOPPER it is enforced every minute as the D5-S stop above (V2.3), for GOLDBEES it is never a stop.
- **Gold floor protection (D5-S, `stops.protect_floor: true`)**: the GOLDBEES floor units are `int(max(0, min_invested − satellite weights) × value / price)` (0 at brake level 2).
  An AI `tighten_stop` or `sell_fraction` acts only on the units **above** the floor; if the whole holding is floor the advice is refused and logged ("floor protected", no sale). An AI `block` bias caps gold at its floor weight
  (no adds above it) but never skips the floor buy. News and price-shock pauses, and the same-day no-re-buy, only delay a floor top-up until `check_until` (15:25); at that time it is made. Safe mode, the account stop,
  brake level 2, the AI's halt for the day and the event-day buy blocks still block it. Only the −25% brake and the ₹75,000 account stop sell the floor (the −15% halving keeps it).
- **No re-buy**: after a fast-lane sale, an AI-stop sale, an intraday trend-exit stop or the HINDCOPPER results exit, that symbol is not bought again until the next trading day.
- No score, no take-half, no trailing stop, no time stop, no daily buy limits, no maximum of 3 positions, no month-end blackout.
- The portfolio fast-lane trigger under v2 is a drop of ≥1.5% of the value within 15 minutes (payload carries `drawdown_from_peak`).
- If the daily-history download fails, the last good copy is used; with none, nothing is bought but every exit still runs.
- Switching `strategy.mode` resets the mode-specific state (one log line): to v2 clears the old brake and inherits v1 stops; to v1 re-derives every v1 stop.

### V2.5 What stays

- **Claude's bias** (via `guidance.json`, stale counts as neutral): `block` = no new buys or adds for that metal (an existing core is held unless its target falls to 0); `neutral` = default; `favour` changes nothing under D5 (entries already go to target) and only loosens the dip when `rsi_timing` is on. Routines still never buy and never set a size. Every safety invariant in `CLAUDE.md` is unchanged.
- **Block bias on GOLDBEES** (D5-S): `block` no longer means "no gold at all": gold is held at its floor weight (never above it for new buys); other symbols are unchanged.
- **Pauses delay NEW buys only** (never exits): fast-lane price shocks, news pauses, `halt_buys_today`, safe mode, a v1 tripwire's `no_buys_until`, and the event-day buy blocks of section 6.
- **Owner-approved 29 Oct 2026** (`strategy.news_blocks_buys`, `fastlane_only_held`, `retry_missed_buys`, `stuck_alert_points`): news headlines are logged and shown but no longer pause buys under v2 (price-shock pauses stay); the fast lane is woken only by a price shock on a symbol that is held; a daily-check buy that a temporary blocker stopped is retried the next trading morning between `retry_buy_from` and `retry_buy_until` (buys only, that morning's blockers apply, dropped after the window); metals more than 10 points off target at 2 consecutive daily checks raise a MEDIUM `allocation_stuck` alert (never safe mode).
- **HINDCOPPER** keeps its results-day block and the sell-before-close-on-results exit (section 6).
- Paper costs: slippage and trade costs as before (`valuation`, `broker.trade_costs`).

### V2.6 Logging

Every trade or skip has a one-line reason that states the formula numbers, for example
`GOLDBEES trend ON (close 121.40 > SMA200 118.90); target 87% = min(1, 0.14/0.161); buying 40 units to reach target (trend ON and no position)`.
A daily **TARGET** record per symbol (trend state, σ60, target %, actual %) is written to `logs/decisions-DATE.jsonl` and shown as `targets` in the dashboard document.
The per-minute check (`logs/checks-DATE.jsonl`) says, for example, `Price ₹119.80. Trend OFF: close 119.80 < SMA200 121.20, target 0% (now 0%).`

### V2.7 Backtest (daily data, signal at the close of t, fill at the close of t+1, costs 0.15% ETF / 0.30% HINDCOPPER per side)

`python run_backtest.py --strategy v2 [--data-dir DIR] [--start D] [--set key=value]` replays exactly these rules (same code as the engine).
With `strategy.stops.intraday_exit` the backtest also enforces the silver/copper exit level on the day's open and low (fill at min(open, level), +0.10% extra slippage, no re-entry before a close above the 55-day high); `--set stops.intraday_exit=[]` gives the older daily-close numbers exactly. Real opens come from `--data-dir`; without an open the level itself is the fill.
D5 on the research data: last 2 years about 46.6% CAGR, max drawdown about −22.6%; 2010-06 to 2024-10 about 10.6%, −19.0% (research doc: 46.5% / −23.1% and 10.8% / −17.9%).
Plain v2 (the earlier profile) is `--set min_invested=0 --set stop_to_gold=false --set entry.rsi_timing=true`.

### V2.8 Risks the owner accepted by choosing AGGRESSIVE

Because the D5 floor keeps about half the portfolio in gold even in a gold downtrend, drawdowns of 20% to 25% from the peak are expected before the brake bites (in-sample about −19%, last 2 years about −23%); a single month of −6% to −9% has happened in the backtest;
trend following is flat or slightly negative in choppy markets (as in 2026); there is no longer any guarantee of a green month end;
SILVERBEES results before 2022 in the backtest use a COMEX proxy.

## 0. The one goal

The portfolio must be **green at every month end**. Green means a value of at least **₹1,00,000** at the close of the last NSE trading day of the month.
Profit size comes second. When a rule has to pick between safety and profit, it picks safety.

Some words used below:

- **Value**: cash (+ LIQUIDBEES when that mode is on) + every position at its last price, *minus the cost to sell it*. It is checked every minute.
- **Floor**: the "Green Guard" line. Value must never go below it.
- **Cushion**: value − floor. This is the only money we are allowed to risk.
- **Score**: a number from 0.00 to 1.00 that the engine calculates for each instrument (section 3).

Who does what:

- **The engine** (GitHub Actions, every minute, fixed rules, no AI) is the only thing that places orders. Every safety rule lives in the engine.
- **Opus** gives advice only, by writing `guidance.json` or a fast-lane reply. The engine checks every piece of advice against this rulebook. Advice that breaks a rule is ignored and logged.

---

## 1. Green Guard floor

1. At the start the floor is **₹99,500**. That is the ₹500 the owner agreed could be lost in the first days.
2. **Best profit** is the highest value seen so far, minus ₹1,00,000. It is checked every minute.
3. When best profit first reaches **₹500**, the floor becomes **₹1,00,000**.
4. After that: **floor = ₹1,00,000 + 50% × (best profit − ₹500)**.
   - *Why the "− ₹500":* the agreement says the floor is exactly ₹1,00,000 when profit hits ₹500. Using plain "50% of best profit" would jump the floor to ₹1,00,250 at that moment and leave only ₹250 of room. This version starts at ₹1,00,000 and rises 50 paise for each extra rupee of best profit. To use the plain version instead, set `ratchet_offset: 0`.
   - Examples: best profit ₹1,500 gives a floor of ₹1,00,500. Best profit ₹4,500 gives ₹1,02,000.
5. The floor **never goes down**. That includes the change from one month to the next (section 7).
6. **Tripwire:** if value ≤ floor + **₹100**, the engine sells every trading position at once and moves the money to cash (LIQUIDBEES in the old mode). It makes no new buys until the next trading day.
7. **Caution zone:** if value ≤ floor + **₹300**, the engine makes no new buys. Exits still work as normal.

---

## 2. How much may be risked (sizing)

### 2.1 The worst-case cost of a position

Every position is budgeted at its **worst case**. That means the price jumps straight past our stop overnight by the full gap allowance:

`position_risk = qty × price × (gap% + round-trip cost%)`

This one number already covers both parts of the agreed rule. It is the stop risk, qty × (price − stop), *plus* the extra gap loss beyond the stop. A trailing stop does **not** lower this number, because a gap jumps over any stop.

| Instrument | Daily volatility | Stop | **Gap allowance** | Cost | **Risk rate** |
|---|---|---|---|---|---|
| GOLDBEES | ~1.0% | 1.2% | **3.0%** | 0.10% | **3.10%** |
| SILVERBEES | ~1.8% | 2.0% | **5.0%** | 0.10% | **5.10%** |
| HINDCOPPER | ~2.8% | 3.0% | **7.0%** | 0.25% | **7.25%** |

**Why these gap sizes:** each one is about 2.5 to 3 times the normal daily move.

- Gold uses 3% rather than 2.5%. GOLDBEES follows world gold *and* the rupee, and both keep moving while NSE is closed.
- Silver uses 5% rather than 4%. Silver had some of the largest one-day falls in its history in early 2026, so 4% (about 2.2 daily moves) is too thin.
- HINDCOPPER uses 7%. A single stock can gap on company news, but it is also our smallest position.

None of these numbers covers a true crash (section 8).

**Gap multipliers.** Before a weekend, multiply gap% by **1.25**. Before an NSE holiday when world markets stay open, multiply by **1.5**. Event nights have their own multipliers (section 6).

### 2.2 The budget rule (checked before every buy)

`Σ position_risk (all open positions, including the new one) ≤ usable_risk`

`usable_risk = min(cushion − ₹100, green_budget) × week_factor`

- The **₹100** is held back for slippage. It also matches the tripwire.
- **green_budget** = value + 0.8 × (LIQUIDBEES carry expected until month end) − ₹1,00,000 − ₹100.
  - This only matters while the floor is below ₹1,00,000, which is the first month. It makes sure a worst-case loss can be earned back by the LIQUIDBEES interest before month end.
  - Carry is about ₹17 per calendar day on ₹1,00,000 at 6.3%. The 0.8 cuts it to about 5% to be safe.
- **week_factor** depends on the trading day of the month:

| Trading day of month | week_factor | Max total invested (excluding LIQUIDBEES) |
|---|---|---|
| Days 1–5 (week 1) | 0.50 | 30% |
| Days 6–10 (week 2) | 0.75 | 50% |
| Day 11 to 4th-last day | 1.00 | 70% |
| Last 3 trading days | no new buys | only reduce (section 7) |

### 2.3 Worked table: largest single position

This assumes it is the only open position. Amounts are rounded down to ₹100. "cap" means the per-instrument cap or the weekly total cap was the limit, not the risk budget.

**Day 11 onwards (week_factor 1.0):**

| Cushion | Usable risk | GOLDBEES | SILVERBEES | HINDCOPPER |
|---|---|---|---|---|
| ₹500 | ₹400 | ₹12,900 | ₹7,800 | ₹5,500 |
| ₹1,000 | ₹900 | ₹29,000 | ₹17,600 | ₹12,400 |
| ₹2,000 | ₹1,900 | ₹60,000 (cap; budget allows 61,200) | ₹30,000 (cap) | ₹15,000 (cap) |
| ₹4,000 | ₹3,900 | ₹60,000 (cap) | ₹30,000 (cap) | ₹15,000 (cap) |

**Week 1 (week_factor 0.5, total cap 30%):**

| Cushion | Usable risk | GOLDBEES | SILVERBEES | HINDCOPPER |
|---|---|---|---|---|
| ₹500 | ₹200 | ₹6,400 | ₹3,900 | ₹2,700 |
| ₹1,000 | ₹450 | ₹14,500 | ₹8,800 | ₹6,200 |
| ₹2,000 | ₹950 | ₹30,000 (cap) | ₹18,600 | ₹13,100 |
| ₹4,000 | ₹1,950 | ₹30,000 (cap) | ₹30,000 (cap) | ₹15,000 (cap) |

Positions share one budget. **Example:** the cushion is ₹2,000, so usable risk is ₹1,900. ₹30,000 of gold uses ₹930. That leaves ₹970, enough for at most ₹19,000 of silver.

**Month 1, day 1:** green_budget is about ₹314, so the first row is really ₹314 × 0.5 = ₹157 of usable risk. That allows about ₹5,000 of gold. This is slow on purpose.

### 2.4 Caps

| Rule | Value |
|---|---|
| GOLDBEES max | 60% of value |
| SILVERBEES max | 30% of value |
| HINDCOPPER max | 15% of value |
| Minimum in LIQUIDBEES/cash | 30% of value, always |
| Max open positions | 3 (one per instrument) |
| Adding to a position | at most once, only if it is already +0.5× its target and the budget allows |
| Engine slippage on paper fills | 0.05% ETFs, 0.10% HINDCOPPER (on top of costs) |

---

## 3. Entries and exits

### 3.1 Score (engine, fixed rules, 0.00–1.00)

| Part | Points |
|---|---|
| Last close > 20-day EMA | 0.15 |
| 20-day EMA > 50-day EMA | 0.15 |
| 10-day return > 0 | 0.10 |
| RSI(14) between 50 and 70 | 0.15 |
| Price > today's VWAP | 0.10 |
| Price > high of the first 15 minutes | 0.15 |
| Today's range so far < 1.5 × ATR(14) | 0.10 |
| Opening gap smaller than 1 × daily volatility | 0.10 |

### 3.2 Buy threshold and guidance.json

| Bias in guidance.json | GOLDBEES | SILVERBEES | HINDCOPPER |
|---|---|---|---|
| favour | 0.65 | 0.70 | 0.75 |
| neutral | 0.70 | 0.75 | 0.80 |
| block-new-buys | no buys | no buys | no buys |
| guidance missing or older than 75 min | neutral + 0.05 | neutral + 0.05 | neutral + 0.05 |

Guidance can only change the **threshold**. It can never change size, caps, stops or the floor.

A buy also needs all of these:

- the score has stayed above the threshold for **3 minutes in a row**;
- the budget passes;
- the price is no more than 3 minutes old.

### 3.3 Exits (engine; checked every minute, in this order)

1. **Tripwire** (section 1).
2. **Initial stop:** entry − 1.2% gold, − 2.0% silver, − 3.0% HINDCOPPER.
3. **Take half profit:** at +1.5% gold, +2.5% silver, +4.0% HINDCOPPER, sell half. Then move the stop on the rest to entry + costs.
4. **Trailing stop** (starts after the half is sold): 1.0% gold, 1.6% silver, 2.5% HINDCOPPER below the highest price since entry.
5. **Time stop:** sell at 15:15 if the position has not reached half its target within 5 trading days (gold), 4 (silver) or 3 (HINDCOPPER).
6. **Score exit:** sell at 15:15 if the score is below 0.35.
7. **Event and month-end exits** (sections 6 and 7).

Exits are never limited by trade counts or cooldowns.

### 3.4 Overnight check (15:20 every day)

Using tonight's gap multiplier, the budget rule must still pass. If it fails, the engine sells the position with the highest risk first, until it passes.

---

## 4. Daily limits

| Limit | Value |
|---|---|
| New buys per day (all instruments) | 3 |
| New buys per instrument per day | 1 |
| New buys per instrument per week | 3 |
| Minimum gap between any two buys | 30 min |
| Re-buy after a stop-out | not the same day for that instrument |
| No buys | 09:15–09:30 (first 15 min), and after 15:00 |
| After 2 losing trades in one day | no new buys for the rest of the day |
| Fast-lane runs | max 4 per day, at least 20 min apart |

---

## 5. Fast lane

### 5.1 Price-shock triggers (based on the minute prices)

| Instrument | Move within 10 minutes | Move since yesterday's close |
|---|---|---|
| GOLDBEES | ±0.6% | ±1.5% |
| SILVERBEES | ±1.0% | ±2.7% |
| HINDCOPPER | ±1.8% | ±4.0% |

The 10-minute numbers are about 4 times a normal 10-minute move. The "since close" trigger fires at most once per instrument per day.

There is also a **portfolio trigger**: value falls by 40% or more of the cushion within 15 minutes.

### 5.2 News triggers

These use `news_triggers.yaml`. The lane wakes on urgency ≥ 4, or when two or more categories match.

Categories: monetary policy, inflation data, US jobs, geopolitics, dollar/rupee, gold-specific, silver-specific, copper-specific and Hindustan Copper. Add these categories:

- **India duty/budget** ("import duty", "customs duty", "union budget"; urgency 4).
- **Company actions** ("offer for sale", "stake sale", "disinvestment", "block deal", "mine accident"; HINDCOPPER; urgency 4).
- **Market halt** ("circuit breaker", "trading halt", "exchange outage"; all instruments; urgency 5).

Replace "powell" with "fed chair".

Tuning after 14 Oct 2026 (proposal `docs/proposals/2026-10-14-news-trigger-tuning.md`):

- **Monetary policy is urgency 3**, raised to **5** only by a decision word ("cuts rates", "raises/hikes rates",
  "hikes repo rate", "holds rates", "rate decision", "emergency", "surprise", "unscheduled", "intermeeting").
- Only categories at urgency ≥ 3 count toward "two or more categories", and only they give the instruments
  to block (a dollar/rupee mention no longer pauses HINDCOPPER on a Fed or RBI story).
- Previews, outlooks, explainers and personal-finance pieces (`context_phrases`: "ahead of", "outlook", "how to",
  "EMI", …) never wake the lane unless a category reaches urgency 5.
- A category blocks the same instrument at most once per **2 hours** (per urgency level, so a decision after
  earlier commentary still blocks).

### 5.3 What happens

1. **Straight away, with no AI:** when a shock fires, the engine blocks new buys in that instrument for 30 minutes. Stops and the tripwire keep running. The engine **never waits** for Opus to protect the money.
2. **After 2–4 minutes**, Opus replies. Opus **may** do any of these:
   - set the bias to neutral or block-new-buys, until the next hourly run;
   - ask to sell part or all of a position;
   - tighten a stop (move it closer to the price);
   - stop all new buys for the rest of the day.
3. Opus **may not**:
   - buy, or set "favour";
   - loosen a stop;
   - raise a cap or a size;
   - change the floor, the gap allowance or the tripwire;
   - skip a cooldown or a limit.
   The fast lane is **for defence only**.
4. If the daily limit of 4 runs is used up, a new shock gets the automatic default instead: block new buys in that instrument for **60 min**.

---

## 6. Event days

The weekly Saturday review keeps an `events.json` calendar. The 08:50 pre-market run checks it each day. US times are shown in IST: the first time is for the US summer period, the second for US winter.

| Event | Rule |
|---|---|
| **US CPI** (18:00 / 19:00 IST) | No new gold/silver buys after 13:00. At 15:20 the overnight check uses **1.5×** gap for gold and silver. Next day: no buys before 09:45. |
| **US jobs report** (usually 1st Friday, 18:00 / 19:00 IST) | Same as CPI. The weekend multiplier also applies, so the factor is 1.5 × 1.25. |
| **FOMC decision** (23:30 / 00:30 IST) | No new gold/silver buys all day. At 15:20 use **2×** gap for gold and silver. Next day: no buys before 10:15, after the first hourly run. |
| **RBI policy** (about 10:00 IST) | No new buys in any instrument from 09:15 to 10:45. The fast lane is on alert. |
| **Hindustan Copper results / board meeting** | No new HINDCOPPER buys from 2 trading days before. Sell all HINDCOPPER by 15:00 the day before (or on the day, if the results come during market hours). Buying is allowed again from 10:15 on the second trading day after the results. |
| **Union Budget day** | Treated like FOMC for gold and silver. Import duty changes can move these ETFs by about 5% in one go. |

---

## 7. Month end and the new month

### 7.1 Last 3 trading days

1. **No new buys.** Positions can only be cut or sold.
2. **Month-end check** at 15:20 on each of the last 3 days:
   `value − Σ qty × price × gap% × 1.5 × (any event or weekend multiplier) ≥ max(floor, ₹1,00,000) + ₹200`.
   If it fails, sell the position with the highest risk first, until it passes.
3. If value falls below **₹1,00,300** at any time in these days, sell everything and move it to LIQUIDBEES for the rest of the month.
4. **Last trading day:** the month is valued at that day's close, so the night after it does not count for this month. By 15:15, any position whose stop loss could pull value below ₹1,00,200 is sold.

### 7.2 New month: the floor keeps ratcheting (it does not reset)

**Decision:** the floor carries over unchanged and keeps ratcheting up. It does **not** reset to the new month's starting value.

Why:

- **Green means ₹1,00,000, not "up this month".** If the floor were reset to the month's start value minus a loss allowance, gains already made could be lost again.
- **The agreed design already says the floor never goes down.** Resetting it at month end would break that.
- **A higher floor makes red months less likely,** not more. Once the floor is above ₹1,00,000, a month can only end red if a crash jumps past the floor (section 8).

The cost is that after a good run the cushion is only half of best profit. After a tripwire it can be very small, so trading slows down. LIQUIDBEES slowly builds it back, at about ₹500 a month.

What does reset on the 1st trading day: the week_factor goes back to 0.5, and all counters restart.

---

## 8. What can still go wrong

1. **A crash bigger than the gap allowance.** This is the main risk. In early 2026 silver fell by roughly a quarter and gold by roughly a tenth in a single session. With silver at its 30% cap, a 25% fall loses about ₹7,500, which goes far past any cushion. The floor is a rule, not a guarantee.
2. **Month 1.** Until profit reaches ₹500, the floor is ₹99,500. A bad first week can still leave the month just red, even with the LIQUIDBEES top-up.
3. **Stuck near the floor.** After a tripwire, the cushion is about ₹100–300, so positions are tiny. It can take weeks to recover. This is by design.
4. **Whipsaw.** Stops in choppy markets cause many small losses plus costs.
5. **Missing big trends.** Over the backtest period, holding gold made about 28% a year. This system holds at most 70%, and usually much less, so it will lag badly in strong gold rallies.
6. **Data or system problems.** Stale prices, GitHub Actions delays or skipped minutes, a failed Opus run, or a wrong event date. Fills in the simulator are better than real life.
7. **LIQUIDBEES yield.** It can be below 6.3% if RBI cuts rates. That is why the carry is cut by 0.8.
8. **ETF tracking.** GOLDBEES and SILVERBEES can trade away from their fair value (premium or discount), especially in fast markets.
9. **Rules not yet tested.** These exact rules have **not** been backtested. The 13%/yr, −14.7% backtest used a different, riskier design.

### Expected monthly outcomes (rough, not a promise)

| | Estimate |
|---|---|
| Typical month | about −0.3% to +1.2% (−₹300 to +₹1,200) |
| Middle of the range | about +0.4% to +0.7% a month, half of it from LIQUIDBEES |
| Good month | +1.5% to +2.5% (rare) |
| Bad month | −0.5% (month 1), or much worse in a crash beyond the gap allowance |
| Per year | maybe 5–9% |
| 3–4% per month | **not realistic** with these safety rules |

Red month-ends should be uncommon, but they are possible. Measure the real red-month rate over at least 6 months of paper trading before trusting it.

---

## 9. Changing the rules

- The Saturday Opus review may **suggest** changes. Only the owner can approve them.
- The engine may apply a change that makes things *safer* (lower caps, wider gaps) at once.
- A change that adds risk needs the owner's approval in writing (a commit to `params.yaml`).

---

## 10. Parameters (for engineers)

```yaml
# simulate_gold rulebook v1 - all amounts in INR, percentages as decimals
capital:
  start: 100000
  park_symbol: LIQUIDBEES
  liquid_yield_annual: 0.063
  liquid_carry_haircut: 0.8          # green_budget uses 80% of expected carry
  min_cash_pct: 0.30

valuation:
  mark: ltp_minus_exit_cost          # value is net of round-trip/2 exit cost
  month_end_mark: close_last_nse_trading_day
  paper_slippage: {GOLDBEES: 0.0005, SILVERBEES: 0.0005, HINDCOPPER: 0.0010}

green_guard:
  initial_floor: 99500
  lock_trigger_profit: 500
  locked_floor: 100000
  ratchet_share: 0.50
  ratchet_offset: 500                # floor = 100000 + 0.5*(best_profit - 500); set 0 for literal
  floor_never_decreases: true
  tripwire_buffer: 100               # flatten all at value <= floor + 100
  tripwire_cooldown: next_trading_day
  caution_buffer: 300                # no new buys at value <= floor + 300
  monthly_reset: false               # floor carries over and keeps ratcheting

sizing:
  risk_formula: qty * price * (gap_pct * gap_multiplier + cost_pct)
  reserve: 100                       # usable = min(cushion - 100, green_budget) * week_factor
  green_budget_target: 100000
  green_budget_buffer: 100
  week_factor:   {week1: 0.50, week2: 0.75, rest: 1.00}
  week_days:     {week1: [1, 5], week2: [6, 10]}
  max_gross_pct: {week1: 0.30, week2: 0.50, rest: 0.70}
  gap_multiplier: {weekend: 1.25, nse_holiday_global_open: 1.5}
  round_qty: floor

instruments:
  GOLDBEES:   {cap_pct: 0.60, stop: 0.012, gap: 0.03, cost: 0.0010, target_half: 0.015, trail: 0.010, time_stop_days: 5}
  SILVERBEES: {cap_pct: 0.30, stop: 0.020, gap: 0.05, cost: 0.0010, target_half: 0.025, trail: 0.016, time_stop_days: 4}
  HINDCOPPER: {cap_pct: 0.15, stop: 0.030, gap: 0.07, cost: 0.0025, target_half: 0.040, trail: 0.025, time_stop_days: 3}

positions:
  max_open: 3
  one_per_instrument: true
  max_adds: 1
  add_requires_progress: 0.5         # fraction of target_half already reached
  after_half_stop: entry_plus_costs
  score_exit_below: 0.35
  score_exit_time: "15:15"
  time_stop_exit_time: "15:15"

entry:
  score_confirm_minutes: 3
  max_price_age_min: 3
  thresholds:
    favour:  {GOLDBEES: 0.65, SILVERBEES: 0.70, HINDCOPPER: 0.75}
    neutral: {GOLDBEES: 0.70, SILVERBEES: 0.75, HINDCOPPER: 0.80}
    block:   no_buys
  stale_guidance_minutes: 75
  stale_guidance_penalty: 0.05       # treated as neutral + 0.05
  score_weights:
    close_gt_ema20: 0.15
    ema20_gt_ema50: 0.15
    ret10d_pos: 0.10
    rsi14_50_70: 0.15
    price_gt_vwap: 0.10
    price_gt_or15_high: 0.15
    range_lt_1p5_atr14: 0.10
    open_gap_lt_1_dailyvol: 0.10

daily_limits:
  max_buys_per_day: 3
  max_buys_per_instrument_day: 1
  max_buys_per_instrument_week: 3
  min_minutes_between_buys: 30
  no_rebuy_after_stop_same_day: true
  no_buys_before: "09:30"
  no_buys_after: "15:00"
  max_losing_trades_then_stop: 2
  overnight_check_time: "15:20"

fast_lane:
  max_runs_per_day: 4
  min_minutes_between_runs: 20
  shock_window_min: 10
  shock_pct:      {GOLDBEES: 0.006, SILVERBEES: 0.010, HINDCOPPER: 0.018}
  from_close_pct: {GOLDBEES: 0.015, SILVERBEES: 0.027, HINDCOPPER: 0.040}
  from_close_once_per_day: true
  portfolio_drop_share_of_cushion: 0.40
  portfolio_drop_window_min: 15
  auto_block_on_shock_min: 30
  auto_block_when_limit_hit_min: 60
  news_file: news_triggers.yaml
  add_categories: [india_duty_budget, company_actions, market_halt]
  allowed_actions: [set_bias_neutral, set_bias_block, exit_partial, exit_full, tighten_stop, halt_buys_today]
  forbidden_actions: [buy, set_bias_favour, loosen_stop, raise_cap, change_floor, change_gap, skip_limits]

events:
  calendar_file: events.json
  us_cpi:   {no_metal_buys_after: "13:00", overnight_gap_mult: 1.5, next_day_no_buys_before: "09:45", applies: [GOLDBEES, SILVERBEES]}
  us_jobs:  {no_metal_buys_after: "13:00", overnight_gap_mult: 1.5, next_day_no_buys_before: "09:45", applies: [GOLDBEES, SILVERBEES]}
  fomc:     {no_metal_buys_all_day: true, overnight_gap_mult: 2.0, next_day_no_buys_before: "10:15", applies: [GOLDBEES, SILVERBEES]}
  rbi:      {no_buys_window: ["09:15", "10:45"], applies: all}
  union_budget: {same_as: fomc}
  hindcopper_results: {no_buys_from_days_before: 2, exit_by: "15:00", exit_day: day_before, resume: "10:15 on 2nd trading day after"}

month_end:
  protect_last_trading_days: 3
  no_new_buys: true
  gap_mult: 1.5
  required_min: max(floor, 100000) + 200
  flatten_if_value_below: 100300
  last_day_exit_time: "15:15"
  last_day_min_after_stop: 100200

governance:
  weekly_review_can_tighten: true
  loosening_requires_owner_approval: true
```
