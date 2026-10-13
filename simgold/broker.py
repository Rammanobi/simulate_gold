"""Paper broker: whole-share fills with Indian delivery-trade costs. Never touches a real account."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import config


def trade_costs(value: float, side: str, kind: str, c=config.COSTS) -> float:
    brokerage = min(value * c.brokerage_pct, c.brokerage_cap)
    exchange = value * c.exchange_pct
    gst = (brokerage + exchange) * c.gst_pct
    stamp = value * c.stamp_buy_pct if side == "BUY" else 0.0
    stt = value * c.stt_equity_pct if kind == "equity" else 0.0
    return brokerage + exchange + gst + stamp + stt


@dataclass
class Position:
    qty: int = 0
    avg_cost: float = 0.0
    high_water: float = 0.0
    opened: str = ""
    # --- Green Guard fields (engine.py); the legacy brain ignores them -------------------------
    stop: float = 0.0            # current stop price (initial, then break-even, or tightened)
    half_sold: bool = False      # take-half-profit already done -> trailing stop is live
    adds: int = 0                # how many times this position was added to
    buy_costs: float = 0.0       # entry costs of the shares still held (for net P&L)
    last_price: float = 0.0
    entry_ts: str = ""


@dataclass
class Portfolio:
    cash: float = config.STARTING_CAPITAL
    positions: dict[str, Position] = field(default_factory=dict)
    peak_equity: float = config.STARTING_CAPITAL
    month: str = ""
    month_start_equity: float = config.STARTING_CAPITAL
    stop_until: dict[str, str] = field(default_factory=dict)     # asset -> ISO date
    last_trade_at: dict[str, str] = field(default_factory=dict)  # asset -> ISO ts
    flat_until: str = ""
    realised_pnl: float = 0.0
    total_costs: float = 0.0
    trades: list[dict] = field(default_factory=list)
    # --- Green Guard state (engine.py) -----------------------------------------------------------
    floor: float = 99500.0
    best_profit: float = 0.0
    liquidbees: float = 0.0                       # rupees parked in LIQUIDBEES (earns the daily yield)
    last_accrual: str = ""                        # ISO date interest was last credited up to
    counters: dict = field(default_factory=dict)  # day / week trade counts and the 2-loss counter
    cooldowns: dict = field(default_factory=dict) # buy spacing, blocks, stop-outs, tripwire day, skip log times
    runtime: dict = field(default_factory=dict)   # score streaks, price shocks seen, value history, fast-lane runs
    day_costs: float = 0.0                        # fees and taxes paid today (buys and sells)
    day_costs_date: str = ""                      # the date the four day_* / income fields belong to
    liquidbees_income_today: float = 0.0
    day_open_value: float = 0.0                   # value at the first tick of the day
    schema: int = 1

    # ---- persistence -------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "Portfolio":
        if not path.exists():
            return cls()
        d = json.loads(path.read_text())
        pos_fields = set(Position.__dataclass_fields__)
        d["positions"] = {k: Position(**{a: b for a, b in v.items() if a in pos_fields})
                          for k, v in d.get("positions", {}).items()}
        known = set(cls.__dataclass_fields__)
        legacy = d.get("schema", 0) < 1
        pf = cls(**{a: b for a, b in d.items() if a in known})
        if legacy:  # migrate the pre-Green-Guard file: idle cash moves into LIQUIDBEES
            pf.liquidbees, pf.cash = pf.cash, 0.0
            pf.floor = 99500.0
            pf.schema = 1
        return pf

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=1, default=str))

    # ---- accounting --------------------------------------------------------------
    def qty(self, asset: str) -> int:
        p = self.positions.get(asset)
        return p.qty if p else 0

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p.qty * prices.get(a, p.avg_cost) for a, p in self.positions.items())

    def weights(self, prices: dict[str, float]) -> dict[str, float]:
        eq = self.equity(prices)
        return {a: self.qty(a) * prices[a] / eq for a in config.ASSETS if a in prices}

    def execute(self, asset: str, side: str, qty: int, price: float, ts, reason: str) -> dict | None:
        if qty <= 0:
            return None
        kind = config.ASSETS[asset]["kind"]
        slip = config.COSTS.slippage_pct
        fill = price * (1 + slip) if side == "BUY" else price * (1 - slip)
        pos = self.positions.setdefault(asset, Position())
        if side == "BUY":
            # shrink order until cash covers value + costs
            while qty > 0 and qty * fill + trade_costs(qty * fill, side, kind) > self.cash:
                qty -= 1
            if qty <= 0:
                return None
            value = qty * fill
            cost = trade_costs(value, side, kind)
            pos.avg_cost = (pos.avg_cost * pos.qty + value) / (pos.qty + qty)
            pos.qty += qty
            pos.high_water = max(pos.high_water, price)
            pos.opened = pos.opened or str(ts)[:10]
            self.cash -= value + cost
            pnl = 0.0
        else:
            qty = min(qty, pos.qty)
            if qty <= 0:
                return None
            value = qty * fill
            cost = trade_costs(value, side, kind)
            pnl = (fill - pos.avg_cost) * qty - cost
            pos.qty -= qty
            self.cash += value - cost
            self.realised_pnl += pnl
            if pos.qty == 0:
                self.positions.pop(asset)
        self.total_costs += cost
        self.last_trade_at[asset] = str(ts)
        t = {"ts": str(ts), "asset": asset, "side": side, "qty": qty, "price": round(fill, 4),
             "value": round(value, 2), "costs": round(cost, 2), "pnl": round(pnl, 2), "reason": reason}
        self.trades.append(t)
        return t
