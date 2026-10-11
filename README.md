# simulate_gold — autonomous metals paper-trader (₹1 lakh)

A **paper-trading simulator** for Indian metal instruments. Every minute while NSE is open it
pulls prices and news, decides what it would buy or sell, "executes" against a simulated
account with realistic Indian costs, and writes down *why*. No broker is connected and no real
money moves. Use it to judge whether the logic deserves your trust before you act on anything
yourself.

> ⚠️ This is not investment advice, and the author is not a licensed advisor. A steady
> 3–4% every month (~42–60% a year) is **not** something any strategy here can promise.
> The backtest below shows what this one has actually done.

## What it trades

| Sleeve | Tradable (NSE) | Max weight | Drivers it watches |
|---|---|---|---|
| Gold | `GOLDBEES` (Nippon Gold ETF) | 50% | COMEX gold, USD/INR, US dollar index, US 10y yield |
| Silver | `SILVERBEES` (Nippon Silver ETF) | 35% | COMEX silver, USD/INR, DXY, copper |
| Copper | `HINDCOPPER` (Hindustan Copper) | 20% | COMEX copper (proxy for MCX/LME copper), Nifty, DXY |

At least 10% always stays in cash. MCX "e-copper" has no free data feed, so COMEX copper
(`HG=F`) is the copper signal and Hindustan Copper is the tradable stand-in.

## How a purchase decision is made (every minute)

```
prices (1-min, Yahoo) ─┐
daily history (2y) ────┼─► technical score  (trend EMA20/50/100, risk-adjusted 20d momentum, RSI)
macro tickers ─────────┼─► macro score      (direction of USD/INR, DXY, yields, COMEX metal)
Google-News RSS ───────┴─► news score       (Haiku scores each new headline; 12h half-life decay)

score = 0.55·tech + 0.15·macro + 0.30·news           each in [-1, +1]

target weight = max_weight × min(1, score / 0.40) × min(1, 15% / realised vol)
   enter only if score > +0.15;  once held, exit when score < 0
   ↓ portfolio risk caps
   • trailing stop: price < high-water − 2.5×ATR → sell, 3-day cooldown
   • drawdown −6% from peak → exposure halved;  −10% → flat for 5 days
   • month-to-date ≥ +4% → gross ≤ 30% (lock the month's gain)
   • month-to-date ≤ −3% → gross ≤ 50%
   • ignore drifts under 3% of equity; ≤ 1 trade per asset per 30 min (keeps costs down)
   ↓ proposed orders
Opus risk review (only when there are orders) — may scale each order 0–1×, never enlarge it;
protective sells always go through
   ↓
paper fill: whole units, 0.05% slippage, brokerage (₹20 cap), exchange fee, GST, stamp duty,
STT on Hindustan Copper
```

Each decision with fills, plus one snapshot every 15 minutes, goes to `logs/decisions-YYYY-MM-DD.jsonl`.
At the close, Sonnet writes a plain-English journal entry to `reports/daily/YYYY-MM-DD.md`.

### Model routing (cost control)

| Job | Model | When it runs |
|---|---|---|
| Score headlines | `claude-haiku-5-5` | only for *new* headlines, refreshed every 10 min |
| Review proposed trades | `claude-opus-5-5` | only on minutes where the rules want to trade |
| Daily journal | `claude-sonnet-5-5` | once, at the close |

With no `ANTHROPIC_API_KEY` set, everything still runs: headlines are scored by a transparent
keyword lexicon, there is no review step, and the journal falls back to a template.

## What ₹1,00,000 would look like

**Today (data to 9 Oct 2026):** every score is negative (gold −0.12, silver −0.31,
copper −0.35). Gold and silver in rupees are down about 2–7% from early September, the Fed hiked
in September and the dollar and yields are firm. So the engine holds **100% cash** and waits.

**When the signals turn fully bullish**, using today's prices and volatility, the sizing rule
would buy about:

| Instrument | Weight | ₹ | Units @ price |
|---|---|---|---|
| GOLDBEES | 48.7% (vol 15.4% → 0.97× of the 50% cap) | 48,700 | ~396 @ ₹122.75 |
| SILVERBEES | 21.2% (vol 24.8% → 0.60× of 35%) | 21,200 | ~101 @ ₹209.68 |
| HINDCOPPER | 9.3% (vol 32.2% → 0.47× of 20%) | 9,300 | ~19 @ ₹468.85 |
| Cash | 20.8% | 20,800 | — |

The more volatile an asset, the less of it gets bought. That is why copper never gets much of
the money.

## Backtest (honest numbers)

`python run_backtest.py` covers Jul 2022 → Oct 2026, starting when SILVERBEES data begins. The news
layer is off, because free historical headlines don't exist. Costs are included.

| | Strategy | Gold ETF buy & hold |
|---|---|---|
| Final equity | ₹1,67,088 | ₹2,81,537 |
| CAGR | 12.96% | 27.85% |
| Max drawdown | −14.7% | −24.4% |
| Avg month | +1.04% | +2.12% |
| Months ≥ +3% | 23% | 40% |
| Worst month | −3.55% | −9.86% |

What this means: across 2022–26, a historic gold bull run, simply holding gold beat the active
strategy. The strategy's edge is a smaller drawdown and a much better worst month, not
higher returns. **It did not deliver 3–4% a month**, and nothing in this repo should be read as
saying it will. The weekly backtest workflow refreshes `reports/backtest.md`.

## Running it

```bash
pip install -r requirements.txt
python -m pytest -q tests
python run_backtest.py                 # history → reports/backtest.md
python run_live.py --once --force      # one tick now, even when the market is closed
python run_live.py                     # loop every minute until the 15:30 close
```

### In the cloud (GitHub Actions)

`.github/workflows/market-hours.yml` runs two jobs each weekday, at 09:10 and 12:15 IST, because
GitHub caps a job at 6 hours. Inside each job the loop ticks every 60 seconds. When a job ends,
the updated `state/`, `logs/` and `reports/` are committed back to the repo, so the paper
portfolio carries over from day to day.

1. Optional: add the repo secret `ANTHROPIC_API_KEY` (Settings → Secrets → Actions) to turn on
   the Haiku/Opus/Sonnet layer. Without it the rules run alone.
2. Actions tab → *market-hours paper trading* → **Run workflow** with `force` checked to smoke-test.

GitHub's scheduled starts can run 5–15 minutes late, so the first minutes after the open may be
missed. For a strict 09:15 start, run `python run_live.py` from a cron job on any small VM.

## Layout

```
simgold/config.py    universe, weights, risk limits, costs, model IDs
simgold/data.py      Yahoo Finance daily + 1-minute prices
simgold/signals.py   EMA/RSI/ATR/momentum, macro scoring
simgold/news.py      Google-News RSS, lexicon + Haiku scoring, decay
simgold/llm.py       Claude calls (Haiku score, Opus review, Sonnet journal)
simgold/brain.py     decision engine + risk caps (shared by backtest and live)
simgold/broker.py    paper account with Indian transaction costs
simgold/backtest.py  daily backtest (fills at the next close, no look-ahead)
run_live.py          minute loop      run_backtest.py   backtest report
```

## Known limits

- Yahoo NSE intraday data is about 1 minute delayed and sometimes has gaps. A stale feed is treated as "market closed".
- `market.py` holds a best-effort list of 2026 NSE holidays. Check it against the official circular.
- Headline sentiment is a weak, noisy signal. It carries 30% of the score and decays quickly on purpose.
- The backtest has no news layer, so live results will differ from it.
