"""Paper broker: whole-share fills with Indian delivery-trade costs. Never touches a real account."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import config

TRADES_KEEP = 200      # trades kept in portfolio.json; older ones go to logs/trades-archive.jsonl


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


_NUM_FIELDS = ("cash", "liquidbees", "floor", "best_profit", "realised_pnl", "total_costs", "peak_equity",
               "month_start_equity", "day_costs", "liquidbees_income_today", "day_open_value")
_DICT_FIELDS = ("counters", "cooldowns", "runtime", "stop_until", "last_trade_at")
_GREEN_GUARD_KEYS = ("floor", "liquidbees", "best_profit")
_REQUIRED_V1 = ("cash", "liquidbees", "floor", "positions")


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_portfolio_dict(d) -> bool:
    """Check a loaded portfolio.json; raise ValueError if it is not sane. Returns True for a legacy (pre-Green-Guard) file.
    A file with Green Guard keys but no `schema` is NOT legacy: migrating it would move LIQUIDBEES and reset the floor."""
    if not isinstance(d, dict):
        raise ValueError("portfolio.json top level is not an object")
    has_gg = any(k in d for k in _GREEN_GUARD_KEYS)
    schema = d.get("schema", 1 if has_gg else 0)
    if not _finite(schema):
        raise ValueError(f"schema is not a number: {schema!r}")
    legacy = schema < 1 and not has_gg
    if not legacy:
        miss = [k for k in _REQUIRED_V1 if k not in d]
        if miss:
            raise ValueError(f"portfolio.json is missing keys {miss}")
    for k in _NUM_FIELDS:
        if k in d and not _finite(d[k]):
            raise ValueError(f"{k} is not a finite number: {d[k]!r}")
    for k in ("cash", "liquidbees"):
        if k in d and d[k] < -1.0:
            raise ValueError(f"{k} is negative: {d[k]}")
    for k in _DICT_FIELDS:
        if k in d and not isinstance(d[k], dict):
            raise ValueError(f"{k} is not an object")
    if "trades" in d and not isinstance(d["trades"], list):
        raise ValueError("trades is not a list")
    pos = d.get("positions", {})
    if not isinstance(pos, dict):
        raise ValueError("positions is not an object")
    for sym, v in pos.items():
        if not isinstance(v, dict):
            raise ValueError(f"position {sym} is not an object")
        q = v.get("qty", 0)
        if not _finite(q) or q < 0 or q != int(q):
            raise ValueError(f"position {sym} has a bad quantity {q!r}")
        for k in ("avg_cost", "high_water", "stop", "last_price", "buy_costs"):
            if k in v and (not _finite(v[k]) or v[k] < 0):
                raise ValueError(f"position {sym}.{k} is not a sane number: {v[k]!r}")
        if q > 0 and not (v.get("avg_cost", 0) > 0):
            raise ValueError(f"position {sym} has no entry price")
    if not legacy:
        d["schema"] = max(1, int(schema))
    return legacy


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
        """Raises ValueError on anything that is not a sane portfolio, so the caller restores a backup
        (simgold.incidents.load_portfolio_safe) instead of trading on garbage."""
        if not path.exists():
            return cls()
        d = json.loads(path.read_text())
        legacy = validate_portfolio_dict(d)
        pos_fields = set(Position.__dataclass_fields__)
        d["positions"] = {k: Position(**{a: b for a, b in v.items() if a in pos_fields})
                          for k, v in d.get("positions", {}).items()}
        known = set(cls.__dataclass_fields__)
        pf = cls(**{a: b for a, b in d.items() if a in known})
        if legacy:  # migrate the pre-Green-Guard file: idle cash moves into LIQUIDBEES
            pf.liquidbees, pf.cash = pf.cash, 0.0
            pf.floor = 99500.0
            pf.schema = 1
        return pf

    def save(self, path: Path) -> None:
        """Atomic (temp file + rename), and ONE line on purpose: a git rebase that meets a concurrent change can
        then only keep one whole version, never splice lines of two portfolios together."""
        path = Path(path)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(self), default=str, separators=(",", ":")) + "\n")
        os.replace(tmp, path)

    def trim_trades(self, archive: Path, keep: int = TRADES_KEEP) -> int:
        """Keep the last `keep` trades in portfolio.json; append the older ones to `archive` (JSON lines) FIRST, then
        drop them from memory, so a crash in between can only duplicate, never lose, a trade. Lines already in the
        archive are not written twice. Returns how many trades were moved. Never raises: on an I/O error the trades stay."""
        if len(self.trades) <= keep:
            return 0
        old = self.trades[:len(self.trades) - keep]
        try:
            archive = Path(archive)
            archive.parent.mkdir(parents=True, exist_ok=True)
            have = set(archive.read_text(encoding="utf-8").splitlines()) if archive.exists() else set()
            lines = [json.dumps(t, ensure_ascii=False, separators=(",", ":"), default=str) for t in old]
            with open(archive, "a", encoding="utf-8") as fh:
                fh.write("".join(x + "\n" for x in lines if x not in have))
        except OSError:
            return 0
        self.trades = self.trades[len(old):]
        return len(old)

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
