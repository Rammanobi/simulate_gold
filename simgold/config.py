"""All tunable knobs in one place. Paper trading only — no broker is ever called."""
from dataclasses import dataclass, field

STARTING_CAPITAL = 100_000.0  # ₹1 lakh

# Tradable instruments (NSE, via Yahoo Finance tickers).
# "E-copper": MCX copper has no free feed, so COMEX copper (HG=F) is used as the
# copper *driver* and Hindustan Copper (HINDCOPPER.NS) as the tradable copper proxy.
ASSETS = {
    "GOLD": {
        "ticker": "GOLDBEES.NS", "name": "Nippon India Gold ETF",
        "max_weight": 0.50, "kind": "etf",
        "drivers": {"GC=F": +1.0, "USDINR=X": +0.5, "DX-Y.NYB": -0.4, "^TNX": -0.4},
        "news_query": "gold price OR gold ETF OR bullion",
    },
    "SILVER": {
        "ticker": "SILVERBEES.NS", "name": "Nippon India Silver ETF",
        "max_weight": 0.35, "kind": "etf",
        "drivers": {"SI=F": +1.0, "USDINR=X": +0.5, "DX-Y.NYB": -0.4, "HG=F": +0.2},
        "news_query": "silver price OR silver ETF",
    },
    "COPPER": {
        "ticker": "HINDCOPPER.NS", "name": "Hindustan Copper Ltd",
        "max_weight": 0.20, "kind": "equity",
        "drivers": {"HG=F": +1.0, "^NSEI": +0.3, "DX-Y.NYB": -0.2},
        "news_query": "copper price OR Hindustan Copper OR LME copper",
    },
}
MACRO_TICKERS = sorted({t for a in ASSETS.values() for t in a["drivers"]})
GENERAL_NEWS_QUERIES = [
    "Federal Reserve interest rate", "RBI repo rate", "US dollar index",
    "rupee dollar", "central bank gold buying", "China copper demand",
]


@dataclass
class Risk:
    min_cash: float = 0.10              # always keep ≥10% cash
    target_vol: float = 0.15            # annualised portfolio vol per asset sleeve
    rebalance_band: float = 0.03        # ignore weight drifts < 3% of equity (saves costs)
    atr_stop_mult: float = 2.5          # trailing stop = high-water − 2.5×ATR
    stop_cooldown_days: int = 3
    trade_cooldown_min: int = 30        # live mode: one trade per asset per 30 min
    dd_soft: float = 0.06               # −6% from peak → half exposure
    dd_hard: float = 0.10               # −10% from peak → flat for 5 days
    month_lock_gain: float = 0.04       # MTD ≥ +4% → cap gross exposure at 30%
    month_cut_loss: float = -0.03       # MTD ≤ −3% → cap gross exposure at 50%


@dataclass
class Weights:
    technical: float = 0.55
    macro: float = 0.15
    news: float = 0.30


@dataclass
class Costs:
    brokerage_pct: float = 0.0003       # 0.03%, capped ₹20/order (discount broker)
    brokerage_cap: float = 20.0
    exchange_pct: float = 0.0000345
    gst_pct: float = 0.18               # on brokerage + exchange
    stamp_buy_pct: float = 0.00015
    stt_equity_pct: float = 0.001       # delivery STT on equity (HINDCOPPER) both sides
    slippage_pct: float = 0.0005


# Model routing (only used if ANTHROPIC_API_KEY is set; the engine runs fine without it).
MODELS = {
    "news": "claude-haiku-5-5",       # cheap, high-volume headline scoring
    "decide": "claude-opus-5-5",      # deliberate review of proposed trades
    "journal": "claude-sonnet-5-5",   # end-of-day write-up
}
NEWS_REFRESH_MIN = 10                 # pull RSS every 10 min, LLM only on new headlines
NEWS_HALF_LIFE_H = 12                 # news sentiment decays with a 12h half-life

RISK = Risk()
WEIGHTS = Weights()
COSTS = Costs()
