# A day in the life: one example trading day

> **Illustrative example, not real data.** Every time, price and rupee amount below is made up to show how the
> rules play out. The numbers were checked against the real sizing and exit code (`simgold/guard.py`,
> `config/params.yaml`) so they follow the rulebook, but no such day was recorded.
>
> Storyboard version: [`architecture/10-day-in-the-life.png`](architecture/10-day-in-the-life.png).
> Overview of the parts: [`ARCHITECTURE.md`](ARCHITECTURE.md). Rules: [`RULEBOOK.md`](RULEBOOK.md).

![Storyboard of the example day](architecture/10-day-in-the-life.png)

## The setting

- A Monday, the **first trading day of a month**, with the system starting fresh: ₹1,00,000 parked in LIQUIDBEES,
  floor ₹99,500, no positions. This is "week 1", so the sizing is deliberately small (week factor 0.5, at most 30%
  invested).
- Prices: GOLDBEES about **₹121**, SILVERBEES about **₹209**, HINDCOPPER about **₹470**.
- Who acts: the **engine** (fixed rules, GitHub Actions, no AI), the **watchdog** (health check), and **routines**
  (Claude, with the model named). Each step says what file changed.

## The day, step by step

### 08:50 Premarket routine (Opus, with a Haiku helper)

- **Who:** premarket routine, Opus. A Haiku helper fetches and condenses the overnight numbers.
- **What it read:** `python tools/context.py` (brief, lessons, last 3 daily notes), then the overnight world:
  COMEX gold +0.8%, silver flat, DXY down 0.3%, rupee slightly weaker, no US data due today.
- **What it decided** (one `guidance.py set` call):
  - **GOLD: favour.** Gold is up overnight, the dollar is down, the rupee is weaker, no event for gold within 24
    hours, and the opening gap is under 1%. All the "favour" conditions in `routines/premarket.md` hold.
    Favour only lowers the buy threshold from 0.70 to **0.65**.
  - **SILVER: neutral.** Silver did not move, so there is no clear tailwind. Neutral is the default.
  - **COPPER: block.** Hindustan Copper results are due on Wednesday, which is within 2 trading days, so the rulebook
    says no new HINDCOPPER buys.
- **Files changed:** `state/guidance.json`, `state/market_today.json`. Pushed as
  `routine(premarket): … bias G=favour S=neutral C=block`.
- **Last step:** it checks whether the engine is running. It is not, so it starts it
  (`market-hours.yml` dispatch). The engine session starts at about 09:00. The scheduled 09:10 start waits in the
  queue as a backup (both share one concurrency group, so two engines can never run at once).

### 09:00 to 09:15 Engine starts

- **Who:** engine. It runs the test suite first (`pytest`), then starts the 60-second loop.
- **File changed:** a first `logs/ticks-D.csv` row, then one row every minute.

### 09:15 to 09:30 Market open, no buys allowed

- **Who:** engine. NSE opens at 09:15. The rulebook forbids buys in the first 15 minutes, and the "price above the
  first 15-minute high" part of the score cannot even be known yet.
- The engine still checks stops and the floor every minute. There is nothing to sell. No decision line is written.

### 09:33 to 09:36 A buy, after 3 minutes of confirmation

- **09:33:** gold's score reaches **0.72**, above the favour threshold of 0.65. The engine starts a 3-minute timer.
- **09:34, 09:35:** the score stays above 0.65 every minute. Had it dipped once, the timer would restart.
- **09:36:** 3 minutes in a row. Now the engine runs every buy check: price under 3 minutes old, inside 09:30 to
  15:00, no event, no block, not near the floor, limits fine. Then the size:
  - Cushion = ₹1,00,000 − ₹99,500 = ₹500. Month 1, so the **green budget** applies (about ₹300).
  - Usable risk = min(₹500 − ₹100, ₹300) × 0.5 = **about ₹150**.
  - Gold worst-case rate is 3.10%, so ₹150 ÷ (₹121.10 × 0.031) = **40 shares** (about ₹4,844, not ₹5,000, because
    shares are whole).
- **Log line written by the engine:**

  ```
  09:36  BUY  GOLDBEES  40 @ ₹121.10
  "The buy score for GOLDBEES stayed at 0.72 or higher (needs 0.65) for 3 minutes, and the risk budget allows 40 shares."
  ```
- **Details:** costs ₹2.64. The initial stop is 1.2% below: **₹119.65**. The whole risk budget (₹150) is now used.
- **Files changed:** `state/portfolio.json`, `logs/decisions-D.jsonl`, `logs/ticks-D.csv`. About every 5 minutes the
  engine commits and pushes `state/` and `logs/` to `main`.

### 09:48 to 09:51 A SKIP because of the 30-minute spacing

- **Who:** engine. Silver's score reaches 0.77 and holds for 3 minutes. The neutral threshold for silver is 0.75.
- At 09:51 it tries to buy and the limit check stops it:

  ```
  09:51  SKIP  SILVERBEES  @ ₹209.30
  "The last buy was 15 minutes ago and at least 30 minutes must pass between buys."
  ```
- By 10:21 silver's score has faded below 0.75, and the risk budget was full anyway, so no silver trade follows.
- **File changed:** `logs/decisions-D.jsonl`. The engine logs one SKIP per instrument per 30 minutes, so the log
  stays readable.

### 10:15 Hourly routine (Sonnet): nothing changed

- **Who:** hourly routine, Sonnet. It checks the engine is running (it is), then runs `should_think.py`. Nothing
  moved enough, no new headline, no event: **SKIP**.
- It runs `guidance.py touch` so the guidance stays fresh (after 75 minutes the engine would add 0.05 to every
  threshold). The whole run takes under 2 minutes.
- **File changed:** `state/guidance.json` (the `updated` time). Pushed as `routine(hourly): 10:15 touch`.

### 10:41 to 10:53 A small incident: the price feed goes missing

- **10:41:** Yahoo Finance returns no prices. **Who:** engine. With no fresh price, nothing can be bought, and the
  engine waits. The stops stay set; they run again as soon as a price returns.
- **10:52:** the feed has been missing for more than 10 minutes. The engine raises an alert:
  category `data_source_down`, which the policy table sets to **MEDIUM**.
  Files changed: `state/alerts.json`, `logs/incidents-D.jsonl`. It is the first Claude wake-up of the day (budget: 6).
- **10:53:** prices are back. The engine marks the feed fresh again.
- **10:55:** the **incident routine** (Sonnet) runs. A Haiku helper summarises the payload and logs in 15 lines. Sonnet
  classifies it as **TRIVIAL**: a data source blip that cleared by itself, so no code change is needed. It opens an
  incident record, notes the action, sets the status to `resolved`, and asks `should-email`. The answer is **HOLD**:
  MEDIUM, no approval needed, fix did not fail, so **no email**.
- **Files changed:** `incidents/ID.json`, `state/alerts.json`. An Inbox item is written as `resolved`.
- **Visible to you:** only as a line under **"Fixed today"** in the 15:45 email.
- *Side note:* the watchdog's 10:50 run was started late by GitHub (it ran at 10:56, after prices were back), so it
  saw a healthy engine. A watchdog run in the middle of a gap would have flagged stale ticks as a separate alert.

### 11:15 Hourly routine (Sonnet): SKIP, touch

Same as 10:15. Nothing material changed. The engine session also hands over from "morning" to "afternoon" at
12:15 to 12:20 (a second scheduled start picks up straight after the first ends).

### 11:40 The profit target: sell half

- **Who:** engine. Gold reaches **₹123.00**, which is 1.6% above the entry of ₹121.10, past the +1.5% target.
- It sells half (20 shares) at ₹122.94 after slippage and costs, and moves the stop on the other half up to
  break-even, **₹121.22**. The trailing stop (1.0% below the highest price since entry) is now live as well.

  ```
  11:40  SELL  GOLDBEES  20 @ ₹122.94   P&L +₹34.47
  "Price rose 1.6% above the entry price of ₹121.10, reaching the 1.5% profit target, so half the position was sold and the stop on the rest moves to break-even."
  ```
- **Files changed:** `state/portfolio.json`, `logs/decisions-D.jsonl`.
- (A trailing stop sells the *rest* if price falls 1.0% from the high. Here it never triggers; see 13:05.)

### 13:05 A news trigger: "US jobless claims jump"

1. **Immediately (13:05, engine, no AI).** The news refresh finds the headline *"US jobless claims jump; traders bet
   on a Fed rate cut"*. It matches two categories (US jobs and monetary policy), which is enough to wake the fast
   lane. The engine **blocks new buys in GOLDBEES and SILVERBEES for 30 minutes** (until 13:35):

   ```
   13:05  BLOCK  GOLDBEES
   "A news headline looks market-moving, so new buys in GOLDBEES are paused for 30 minutes while the AI reviews it."
   ```
   It then calls the fast-lane routine (run 1 of at most 4 today). Files changed: `state/portfolio.json`,
   `logs/decisions-D.jsonl`, `state/fastlane_requests.jsonl`.
2. **13:08 (2 to 4 minutes later, fast lane, Opus).** It validates the payload against `state/portfolio.json`, does
   a quick check of the story (2 searches), and picks from the defensive menu. We hold 20 shares and the move is in
   our favour, so the menu says: tighten the stop, do not sell.
   `guidance.py fastlane --symbol GOLDBEES --tighten-stop 122.50 --note "…"`. The tool allows it because 122.50 is
   above the current stop (₹121.22) and still 0.7% below the price of ₹123.40 (the minimum gap for gold is 0.6%).
   The tool would refuse a buy, "favour", or a looser stop. **File changed:** `state/fastlane.json`.
3. **About 13:10 (engine).** It picks up the file on its next sync, runs it through the same rule filter, and
   applies the tighter stop. The 20 shares are now protected at ₹122.50.
4. **13:15 (hourly, Sonnet + an Opus helper).** A new headline arrived since the last guidance, so this run is
   **THINK**. The Opus helper rethinks: gold **favour becomes neutral** ("a jobs shock can swing both ways; wait"),
   silver neutral, copper block. **File changed:** `state/guidance.json`.

The block lapses at 13:35. No further gold buy is possible anyway: one buy per instrument per day is the limit.

### 13:40 to 15:15 The rest of the afternoon

Gold peaks at ₹123.45 around 13:40 and drifts back to about ₹123.20. The trailing stop sits 1.0% below the high,
at ₹122.22, and the tightened stop is at ₹122.50. Neither is touched. At 15:00 the buy window closes. At 15:15 the
engine runs the time-stop and score exit checks (the score is well above 0.35). Nothing to sell. The hourly routine
at 14:15 and 15:15 SKIPs.

### 15:20 The overnight check

- **Who:** engine. It asks: if gold gaps down 3% overnight (plus costs), does the worst-case loss still fit the risk
  budget? The 20 gold shares are worth ₹2,464, so the worst case is ₹76. The budget is about ₹182. It passes.

  ```
  15:20  HOLD  GOLDBEES  20 @ ₹123.20
  "The 15:20 overnight check passed: the worst-case overnight loss on GOLDBEES (₹76) fits inside the risk budget, so it is kept."
  ```
- **File changed:** `logs/decisions-D.jsonl`.

### 15:30 Close, then the engine session ends (about 15:35)

The engine does a final sync, so `state/portfolio.json` and the logs reach `main`.

### 15:45 Daily report routine (Sonnet, with a Haiku helper)

1. Waits for the engine to finish, then pulls. Today is a trading day.
2. Market notes already exist (premarket wrote them). **Tomorrow's note is missing**, so it writes one through the
   tool: *"Hindustan Copper results are due on Wednesday, so no new copper buys, and any copper would be sold by
   15:00 on Tuesday."* **File changed:** `state/tomorrow.txt` (via `guidance.py tomorrow`).
3. `send_report.py` builds the report. `daily_memory.py` prints the day's facts; Haiku drafts
   `memory/daily/D.md` (at most 15 lines, one "Lesson candidate:" line: "Jobs-data headline arrived 8 minutes
   after the half-sale; the tighter stop was not needed but cost nothing"). `write_brief.py` refreshes
   `memory/brief.md`.
4. **Email**, one message to the owner's address, subject starting "Metals Daybook":

   ```
   Day P&L: +₹74 (+0.07%)         Portfolio: ₹1,00,074
   Floor: ₹99,500                 Distance to floor: ₹574
   Costs paid today: ₹4           LIQUIDBEES income today: ₹0

   DECISIONS
   09:36  BUY    GOLDBEES   40 @ ₹121.10   score 0.72 held 3 minutes, risk budget allows 40 shares
   09:51  SKIP   SILVERBEES  @ ₹209.30     30 minutes must pass between buys
   11:40  SELL   GOLDBEES   20 @ ₹122.94   target reached, sold half, +₹34.47
   13:05  BLOCK  GOLDBEES / SILVERBEES      news headline; paused 30 minutes
   15:20  HOLD   GOLDBEES   20 @ ₹123.20   overnight check passed

   FIXED TODAY
   Price feed was missing for 12 minutes (10:41 to 10:53). Recovered by itself. No action needed.
   ```
5. **Inbox item** `daily-D` is written with the `ArtifactData` tool (status "needs your look"). The email links to it.
6. Commit and push: `reports/daily/D.*`, `memory/daily/D.md`, `memory/brief.md`, `state/tomorrow.txt`.

### 16:30 Watchdog (and, on the fallback branch, 16:45 and 16:50)

The post-close health check passes and writes `state/health.json`. On the DeepSeek fallback branch, the later
checks confirm the routines were not silent today, so nothing needs to take over.

## What the numbers were at the close

| | Amount |
|---|---|
| **Portfolio value** (cash + LIQUIDBEES + gold, net of the cost to sell) | **₹1,00,074** |
| **Floor** (best profit of ₹79 has not reached ₹500 yet, so it stays at the start level) | **₹99,500** |
| **Cushion** (value − floor) | **₹574** |
| **Day P&L** | **+₹74 (+0.07%)** |

How the ₹74 adds up:

| Part | Amount |
|---|---|
| Profit on the 20 shares sold at ₹122.94 (₹36.76 gross, less ₹0.97 sell cost and ₹1.32 of the buy cost) | +₹34.47 |
| Unrealised profit on the 20 shares still held (₹123.20 vs entry ₹121.10) | +₹41.99 |
| Buy costs still attached to those 20 shares | −₹1.32 |
| Allowance for the cost to sell them (value is always shown net of this) | −₹1.23 |
| LIQUIDBEES interest (first day, nothing credited yet) | ₹0 |
| **Total = value − ₹1,00,000** | **+₹73.91 ≈ +₹74** |

Cross-checks:

- Cash and LIQUIDBEES ₹97,611.14 + gold position ₹2,462.77 (20 × ₹123.20, net of the sell allowance) =
  **₹1,00,073.91**.
- Total costs paid today: ₹2.64 (buy) + ₹0.97 (sell) = ₹3.61.
- Highest value seen: ₹1,00,078.91, so best profit ₹78.91, below the ₹500 needed to lift the floor.
- Overnight: worst-case loss ₹76 against about ₹182 of budget. Even a 3% gold gap would leave about ₹1,00,000, well
  above the ₹99,500 floor.

## What you (the owner) did today

You did one thing. In the evening you read one email, opened the **Metals Daybook Inbox** from its link, and tapped
**"Looks right"** on the day's item. That wakes the incident routine in `owner_decision` mode. It reads your
decision from the Inbox database, marks the item resolved, and sends no email. It took about 30 seconds.

You did not choose a trade, size a position, restart anything, or approve a fix. If a number had looked wrong, you
would have tapped Reject and typed why; the routine would re-check the day's numbers from the logs and reply.
