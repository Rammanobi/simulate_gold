# Proposal: tune the news triggers (2026-10-14)

Status: **proposal for the owner.** Implemented on branch `claude/news-trigger-tuning` (PR); nothing changes on
`main` until the owner merges it. Paper trading only.

## 1. What happened on Wed 14 Oct 2026

Every decision the engine logged today was a BLOCK, and a news block on gold and silver was active almost
continuously from 10:17 to the close, so no buy could happen.

| | Morning run (10:17-12:20) | Afternoon runs (12:21-15:30) | Day |
|---|---|---|---|
| BLOCK, "News trigger" | 27 | 30 | **57** |
| BLOCK, "Price shock" (SILVERBEES -2.8% from close) | 1 | 1 | 2 |
| Ticks with at least one waking headline | 11 | 12 | 23 |
| Distinct headlines that woke the lane | 53 | 58 (38 of them re-fired morning headlines, see 1.3) | **73** |
| Fast-lane routine actually reached | 0 (2 POSTs failed with ValueError, then the THRIFT cap of 2/day) | 0 | 0 |

The morning numbers come from `logs/decisions-2026-10-14.jsonl` and `state/fastlane_requests.jsonl` at commit
61c4bca (12:20 IST); the afternoon run's sync overwrote both files at 12:26 (fixed separately on
`claude/day1-fixes`), so `main` today only shows the 31 afternoon decisions.

### 1.1 Why

`config/news_triggers.yaml` gives **monetary_policy urgency 4**, and urgency >= 4 wakes the lane and blocks new
buys for 30 min (60 min once the fast-lane cap is used). The keywords are `fed`, `rbi`, `rate hike(s)`,
`repo rate`, ... On a day with US CPI pending, Fed speakers and an RBI FX intervention, nearly every gold, dollar
and rupee headline contains one of them. Of the 73 headlines, **67 matched monetary_policy** (30 alone, 25 with
dollar, 12 with inflation_data), 5 were dollar + inflation_data pairs (two categories) and 1 was geopolitics
("war-hit supply chains"). Typical ones:

- "Gold Steady as Fed Official's Remarks Temper Rate-Hike Bets" (Bloomberg)
- "Dollar languishes as focus turns to CPI, Fed speakers" (Reuters; 4 outlets over the day)
- "Rupee hits one-month high after RBI backs FX curbs with stability pledge" (Reuters)
- "Have a Rs 50 lakh home loan? Check how much your EMI could rise after the RBI rate hike" (Livemint)
- "Fed's Williams Interview With Bloomberg News: Full Transcript" (Bloomberg)

Two side effects made it worse:

1. **The same story re-blocks every ~10 minutes.** `dedupe_window_minutes: 30` is in the YAML but nothing
   implements it; a theme carried by ten outlets blocks ten times.
2. **The dollar category spreads blocks to HINDCOPPER.** A Fed/RBI story that mentions the rupee matches
   `dollar` (urgency 2, affects all three), and the engine blocks the union of all matched categories'
   instruments: 13 of the 57 blocks were HINDCOPPER on gold/rupee stories.

### 1.2 What a block cost

Little in money today (no buy signal was strong enough in a falling market), but the system was blind: 59/59
decisions were blocks, the daily report could only say "the system waited", and the real event of the day
(US CPI at 18:00 IST) is handled by the event rules anyway (`state/events.json`, CPI 2026-10-14 18:00).

### 1.3 Not caused by the rules

- 12:21: the afternoon run started from the 10:16 commit (stale `state/seen_headlines.json`), so every morning
  headline was "new" again: 79 triggers in one tick. Fixed on `claude/day1-fixes` (fresh checkout + start-up pull).
- The fast-lane POST failed twice with `ValueError` (urllib rejects the URL or a header): check the
  `ROUTINE_FIRE_URL` / `ROUTINE_FIRE_TOKEN` secrets (a missing `https://`, or a trailing newline in the token).
  The failed attempts still used up the THRIFT cap of 2 wake-ups.

## 2. Options

| | Change | Where |
|---|---|---|
| **A. Decision words** | monetary_policy urgency 4 -> **3**; back to **5** only when the headline has a decision phrase (`cuts rates`, `raises/hikes rates`, `hikes repo rate`, `holds rates`, `keeps rates unchanged`, `rate decision`, `emergency`, `surprise`, `unscheduled`, `intermeeting`, ...). Only categories at urgency >= 3 count for the "2+ categories" rule and give the instruments to block (dollar at 2 no longer counts or spreads to HINDCOPPER). | YAML (`escalate`, `global_rules.count_min_urgency`), `simgold/triggers.py` |
| **B. Cooldown** | A category blocks the same instrument at most once per **2 h** (keyed per urgency level, so a decision after commentary still blocks). | YAML `global_rules.cooldown_minutes`, `simgold/engine.py` (`pf.runtime.news_cooldown`) |
| **C. Preview filter** | Headlines with `context_phrases` (`ahead of`, `focus turns to`, `looms`, `outlook`, `preview`, `explainer`, `forecast`, `full transcript`, `how to`, `check how`, `EMI`, `home loan`, `fixed deposit`, ...) never wake unless a category reaches urgency 5 (a decision, a market halt). Not the ignore list: a halt "ahead of results" must still wake. | YAML `context_phrases`, `simgold/triggers.py` |
| **D. A + B + C** | All three. | |

"Block only the symbols in `affects`" was considered: the engine already does that; the leak to HINDCOPPER
came from the low-urgency dollar category, which A fixes.

## 3. Replay of today's headlines

`docs/proposals/2026-10-14-news-trigger-replay.py` feeds each of the 23 recorded trigger ticks, with their
headlines, through the real `run_tick` with prices but no price series (so only news can block), once per
option, and counts BLOCK decisions with trigger "News trigger". The baseline reproduces today's log exactly
(27 + 30 = 57).

| Option | Morning | Afternoon | **Day** | GOLDBEES / SILVERBEES / HINDCOPPER | Headlines that still wake (of 73) |
|---|---|---|---|---|---|
| Baseline (rules on main) | 27 | 30 | **57** | 22 / 22 / 13 | 73 |
| A. Decision words | 10 | 11 | **21** | 10 / 10 / 1 | 14 |
| B. 2 h cooldown | 3 | 8 | **11** | 4 / 4 / 3 | 73 |
| C. Preview filter | 27 | 29 | **56** | 22 / 22 / 12 | 55 |
| A + C | 4 | 7 | **11** | 5 / 5 / 1 | 5 |
| **D. A + B + C** | **2** | **7** | **9** | 4 / 4 / 1 | 5 |

Reproduce (repo root, this branch):
`git show 61c4bca:state/fastlane_requests.jsonl > /tmp/am.jsonl; git show fbebc88:state/fastlane_requests.jsonl > /tmp/pm.jsonl;
PYTHONPATH=. python docs/proposals/2026-10-14-news-trigger-replay.py . /tmp/am.jsonl /tmp/pm.jsonl`.

(The afternoon column replays the afternoon run as it happened, including its stale 12:21 re-fire, and each
column starts with no cooldowns; "Day" is one continuous replay.)

Under D the five headlines that still wake are:

| Time | Headline | Why it wakes |
|---|---|---|
| 10:17 | Sensex sheds 430 points, Nifty loses 173 points after RBI hikes repo rate | decision phrase "hikes repo rate" (urgency 5) |
| 10:17 | What 3 RBI papers reveal about inflation and Indian households | monetary + inflation, both urgency 3 |
| 11:31 | The Federal Reserve sounds the alarm on further rate hikes! ... energy-driven inflation ... | monetary + inflation |
| 14:54 | Base metals head to LME Week 2026 with record-low TCs, war-hit supply chains | geopolitics "war" (urgency 4) |
| 15:25 | India's September inflation rises to 5.54%, strengthening case for further RBI rate hikes | monetary + inflation (a real data release) |

The last one is exactly the kind of headline the lane is for. The "war-hit" one is a false positive of the
whole-word rule (`war-hit` contains the word `war`); worth a follow-up (e.g. ignore `war-hit`, or require
geopolitics to have a second word), not part of this change.

## 4. Recommendation

**Option D.** A removes the cause (Fed/RBI commentary at urgency 4), C removes most of what A leaves (previews
and explainers that pair "Fed" with "CPI"), and B caps what is left at one block per theme per 2 h, which also
stands in for the never-implemented dedupe window. Together: 57 -> 9 blocks on today's tape (-84 %), the
HINDCOPPER leak 13 -> 1, and every real decision, data release, halt or surprise still wakes the lane at
urgency 5. The engine's own protections are untouched: price-shock blocks, stops, the floor, the event rules
(CPI/FOMC days) and the fast-lane limits.

Risk: a genuinely market-moving Fed/RBI story without a decision word ("Fed signals rate cut" alone) no longer
blocks buys for 30 min. Mitigations: price shocks still auto-block within the minute; two urgency-3 categories
("... rate cut as CPI cools") still wake; the FOMC/RBI dates are in the events calendar, where the engine blocks
by rule. Because it lowers a protection, it needs the owner's approval (CLAUDE.md: "a change that adds risk").

## 5. What the PR changes

- `config/news_triggers.yaml`: monetary_policy urgency 3 + `escalate`, `count_min_urgency: 3`,
  `cooldown_minutes: 120`, `context_phrases`.
- `simgold/triggers.py`: escalation, counted categories, context phrases; the result also carries `levels`,
  `wake_categories`, `context`, `cooldown_minutes`. A rules file without the new keys behaves as before.
- `simgold/engine.py`: per-(category, urgency, symbol) cooldown in `pf.runtime["news_cooldown"]`.
- `docs/RULEBOOK.md` 5.2; tests in `tests/test_triggers.py` (today's headlines, decision words, the old-file
  behaviour), `tests/test_guard.py` (cooldown), `tests/test_memory.py` (brief tag u4 -> u3).

## Appendix: every headline that woke the lane today

Time = first tick it fired (IST); categories and urgency as logged on main; last column = outcome under D.

| Time | Headline | Categories | Urgency | Under D |
|---|---|---|---|---|
| 10:17 | Fed's Collins sees another rate hike on the table before end of 2026 - Reuters | monetary_policy | 4 | no wake |
| 10:17 | Williams says Fed may not have to hike rates in 2027, at odds with traders - CNBC | monetary_policy | 4 | no wake |
| 10:17 | One More U.S. Interest-Rate Increase May Be Appropriate, Fed’s Williams Says - WSJ | monetary_policy | 4 | no wake |
| 10:17 | Fed’s Williams Interview With Bloomberg News: Full Transcript - Bloomberg.com | monetary_policy | 4 | context |
| 10:17 | Global Market Today: Asian stocks rise on Fed rate signal, oil gains - The Economic Times | monetary_policy | 4 | no wake |
| 10:17 | FOREX-Dollar languishes as focus turns to CPI, Fed speakers - Devdiscourse | monetary_policy, inflation_data | 4 | context |
| 10:17 | Fed's Williams sees no need for rate hike in 2027, Bloomberg News reports - Reuters | monetary_policy | 4 | no wake |
| 10:17 | Bank of America: Fed October Interest Rate Hike – Biggest Bullish Catalyst for U.S. Stock Market - 36Kr | monetary_policy | 4 | no wake |
| 10:17 | Fed’s Williams currently sees no need for rate hike in 2027 - The Business Times | monetary_policy | 4 | no wake |
| 10:17 | Gold struggles to reclaim $4,200 as Fed interest rate hike risks persist, US CPI looms - FXStreet | monetary_policy, inflation_data | 4 | context |
| 10:17 | Gold Steady as Fed Official’s Remarks Temper Rate-Hike Bets - Bloomberg.com | monetary_policy | 4 | no wake |
| 10:17 | HDFC Bank, ICICI Bank, PNB, BoB fixed deposit rates: Where they stand after RBI's repo rate hike - Upstox | monetary_policy | 4 | context |
| 10:17 | Tamil Nadu MSMEs Fear Impact of RBI Repo Rate Hike - Construction World | monetary_policy | 4 | no wake |
| 10:17 | Don’t Lock Your FD Yet: How To Ladder After The RBI Repo Rate Hike - NDTV Profit | monetary_policy | 4 | context |
| 10:17 | Have a ₹50 lakh home loan? Check how much your EMI could rise after the RBI rate hike - Livemint | monetary_policy | 4 | context |
| 10:17 | Retail Inflation Rises To 5.54%, Nears RBI’s Upper Limit – Is Another Rate Hike Likely? - Outlook Money | monetary_policy, inflation_data | 4 | context |
| 10:17 | RBI may hike repo rate by 75 bps, likely to stay on tightening path through H1FY28: Brokerages - bfsi.economictimes.indi | monetary_policy | 4 | no wake |
| 10:17 | RBI may raise repo rate by 75 bps, maintain tight stance through H1FY28 - The Assam Tribune | monetary_policy | 4 | no wake |
| 10:17 | Sensex sheds 430 points, Nifty loses 173 points after RBI hikes repo rate - Mid-Day | monetary_policy | 4 | wakes |
| 10:17 | RBI governor says FX curbs to quell speculation; rates, financial stability interlinked - Reuters | monetary_policy | 4 | no wake |
| 10:17 | RBI expects surplus liquidity in banking system to decline shortly: Governor Malhotra - TradingView | monetary_policy | 4 | no wake |
| 10:17 | What 3 RBI papers reveal about inflation and Indian households - ThePrint | monetary_policy, inflation_data | 4 | wakes |
| 10:17 | United States Dollar Index weakens to near 102.00 ahead of US CPI release - FXStreet | inflation_data, dollar | 3 | context |
| 10:17 | Dollar languishes as focus turns to CPI, Fed speakers - Reuters | monetary_policy, inflation_data | 4 | context |
| 10:17 | Dollar dips a day ahead of CPI, while euro rises and Indian rupee extends rally - Investing.com | inflation_data, dollar | 3 | context |
| 10:17 | Dollar Index slips from its 18-month high before US CPI - FXStreet | inflation_data, dollar | 3 | no wake |
| 10:17 | Rupee strengthens as RBI curbs dollar demand; USD/INR slips to one-month low - VT Markets | monetary_policy, dollar | 4 | no wake |
| 10:17 | US dollar to Indian rupee rate sees RBI intervention: USD/INR holds above long-term average - Traders Union | monetary_policy, dollar | 4 | no wake |
| 10:17 | RBI’s ‘bazooka’ sees rupee rising 1.4% versus dollar in two days, but should central bank let it fall? - The Times of In | monetary_policy, dollar | 4 | no wake |
| 10:17 | INDIA RUPEE-Indian rupee to build on rally after RBI chief reinforces currency support - marketscreener.com | monetary_policy, dollar | 4 | no wake |
| 10:17 | RBI's moves to shore up rupee patches on long-term weakness - The New Indian Express | monetary_policy, dollar | 4 | no wake |
| 10:17 | Indian Rupee: RBI defense limits losses against US Dollar – Societe Generale - FXStreet | monetary_policy, dollar | 4 | no wake |
| 10:17 | Rupee Appreciates by 49 Paise to 95.28 Against Dollar on RBI Measures - Deccan Chronicle | monetary_policy, dollar | 4 | no wake |
| 10:17 | Can RBI’s six forex measures help stabilise the rupee? - The Economic Times | monetary_policy, dollar | 4 | no wake |
| 10:28 | Gold Holds Steady Ahead Of US Inflation Data, Fed Rate Outlook In Focus - BusinessToday Malaysia | monetary_policy, inflation_data | 4 | context |
| 10:28 | RBI Steps In Again As The Rupee Tries To Hold Its Gains - Finimize | monetary_policy, dollar | 4 | no wake |
| 10:28 | Rupee rallies to 1-month high on RBI curbs, but banks remain uneasy - Business Standard | monetary_policy, dollar | 4 | no wake |
| 10:28 | Rupee outpaces Asian peers on fresh FX curbs, RBI intervention - bfsi.economictimes.indiatimes.com | monetary_policy, dollar | 4 | no wake |
| 10:28 | Indian rupee to build on rally after RBI chief reinforces currency support - marketscreener.com | monetary_policy, dollar | 4 | no wake |
| 10:38 | Fed rate hike not likely to impact NH economy - NH Business Review | monetary_policy | 4 | no wake |
| 10:59 | Rupee rises 44 paise to 94.92 against US dollar amid RBI intervention - Mid-Day | monetary_policy, dollar | 4 | no wake |
| 11:09 | Indian rupee to build on rally after RBI chief reinforces currency support - Reuters | monetary_policy, dollar | 4 | no wake |
| 11:20 | RBI may deliver another 25 bps rate punch in December despite moderating food prices - bfsi.economictimes.indiatimes.com | monetary_policy | 4 | no wake |
| 11:20 | Dollar eases from 18-month high as markets brace for US CPI and Fed signals - VT Markets | monetary_policy, inflation_data | 4 | context |
| 11:20 | Importers in the RBI’s crosshairs amid rupee’s relentless slide - TradingView | monetary_policy, dollar | 4 | no wake |
| 11:20 | Rupee Rebounds Past 95 Per Dollar As RBI Measures Curb Volatility / Gaurav Sharma Explains Market Outlook - Business Tod | monetary_policy, dollar | 4 | context |
| 11:31 | The Federal Reserve sounds the alarm on further rate hikes! Caught between the heavy burden of energy-driven inflation a | monetary_policy, inflation_data | 4 | wakes |
| 11:41 | Dollar languishes as focus turns to CPI, Fed speakers - The Economic Times | monetary_policy, inflation_data | 4 | context |
| 11:41 | Gold steady as Fed official’s remarks temper rate-hike bets - The Edge Malaysia | monetary_policy | 4 | no wake |
| 11:52 | RBI will continue efforts to keep rupee stable; volatility due to external factors: Nirmala Sitharaman - Deccan Herald | monetary_policy, dollar | 4 | no wake |
| 11:52 | Rupee Surges After RBI Steps In to Curb Dollar Pressure - Dalal Street Investment Journal | monetary_policy, dollar | 4 | no wake |
| 11:52 | Gold caught between central banks and the Fed - www.equiti.com | monetary_policy | 4 | no wake |
| 12:02 | Rupee hits one-month high after RBI backs FX curbs with stability pledge - Reuters | monetary_policy, dollar | 4 | no wake |
| 12:42 | Gold price headed for Rs 1.50 lakh per 10 grams? All eyes on US inflation, Fed cues - Business Today | monetary_policy, inflation_data | 4 | context |
| 12:42 | RBI expects surplus liquidity in banking system to decline shortly: Governor Malhotra - Moneycontrol.com | monetary_policy | 4 | no wake |
| 12:52 | Dollar Index slips from its 18-month high before US CPI - www.tmgm.com | inflation_data, dollar | 3 | no wake |
| 12:52 | Rupee stopgap: RBI's new props have boosted its external value but this only spells momentary relief - TradingView | monetary_policy, dollar | 4 | no wake |
| 13:03 | RBI liquidity measures lift call money rates - Ahmedabad Mirror | monetary_policy | 4 | no wake |
| 13:03 | US Dollar Index Price Forecast: DXY holds above 101.70 pivotal support ahead of US CPI - FXStreet | inflation_data, dollar | 3 | context |
| 13:14 | Understand the Impact of Repo Rate Hike - ICICI Direct | monetary_policy | 4 | no wake |
| 13:24 | PIMCO neutral on Indian assets, eyes 75 bps in interest rate hikes, exec says - Reuters | monetary_policy | 4 | no wake |
| 13:46 | Indian Rupee: RBI FX steps support INR but raise hedging costs – MUFG - FXStreet | monetary_policy, dollar | 4 | no wake |
| 13:57 | How the Fed Usually Handles Rates Near an Election - Forex Factory | monetary_policy | 4 | no wake |
| 14:19 | Gold slips below $4,150 as Fed rate outlook weighs on bullion - Traders Union | monetary_policy | 4 | context |
| 14:19 | Indian Bond Yields Touch Three-Year High On Oil, RBI Tightening - BW Businessworld | monetary_policy | 4 | no wake |
| 14:54 | Gold steady as Fed official’s remarks temper rate-hike bets - The Edge Singapore | monetary_policy | 4 | no wake |
| 14:54 | India importers press on with rupee hedges despite RBI's higher-cost deterrent - Reuters | monetary_policy, dollar | 4 | no wake |
| 14:54 | Base metals head to LME Week 2026 with record-low TCs, war-hit supply chains - Fastmarkets | geopolitics | 4 | wakes |
| 15:05 | Rupee hits over one-month high as RBI forex measures, governor's remarks lift sentiment - Moneycontrol.com | monetary_policy, dollar | 4 | no wake |
| 15:05 | Rupee Gains as RBI Signals More Steps to Curb Forex Speculation: INR/USD - Bloomberg.com | monetary_policy, dollar | 4 | no wake |
| 15:05 | Indian Rupee rallies further on RBI's forceful measures to curb USD's demand - FXStreet | monetary_policy, dollar | 4 | no wake |
| 15:25 | Dollar languishes as focus turns to CPI, Fed speakers - Yahoo Finance | monetary_policy, inflation_data | 4 | context |
| 15:25 | India’s September inflation rises to 5.54%, strengthening case for further RBI rate hikes - Prop News Time | monetary_policy, inflation_data | 4 | wakes |
