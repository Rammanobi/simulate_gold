"""The minute tick of the Green Guard paper-trading engine.

    run_tick(pf, mkt, env) -> list[decision dicts]

Order inside one tick (rulebook 3.3 / 5.3 / 7):
  1. new day housekeeping (LIQUIDBEES interest, counters), mark the portfolio, ratchet the floor
  2. fast-lane triggers (price shocks, portfolio drop, news): block buys at once, write the request, fire the AI
  3. exits: tripwire, AI defence advice, stop, take half, trailing, time stop, score exit, event and month-end exits,
     the 15:20 overnight check.  Exits are never limited by trade counts or cooldowns.
  4. entries: score -> threshold -> 3 minute confirmation -> gates -> sizing -> paper fill
  5. tick snapshot (logs/ticks-DATE.csv); the caller saves the portfolio.

Every BUY / SELL / SKIP / HOLD / BLOCK is one JSON line in logs/decisions-DATE.jsonl.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Mapping

from . import alerts, budget, config, fastlane, guard, market, trend
from .broker import Position, Portfolio, trade_costs

log = logging.getLogger(__name__)

try:   # the news matcher is written by another module; the engine runs without it
    from simgold.triggers import match_headline as _match_headline
except Exception:   # pragma: no cover - import guard
    _match_headline = None

DECISION_FIELDS = ["ts", "time", "action", "symbol", "qty", "price", "amount", "pnl", "reason", "trigger",
                   "stop", "risk", "value_after", "floor"]
# Data hygiene (not a trading rule): a minute close this far from the median of the previous few closes is treated as
# a bad print and ignored for this tick. A real move that big keeps printing, so it is accepted once it dominates the
# recent closes (after about 3 minutes). NSE ETF price bands are 20% a day, so 25% inside a few minutes is a glitch.
MAX_TICK_JUMP = 0.25
MAX_OPEN_JUMP = 0.50      # with < 3 closes today (the open): vs yesterday's close / the last known price (bands are 20%)
FUTURE_SLACK = timedelta(minutes=5)     # state files dated further ahead than this are not trusted
TRIGGERS = {"Minute check", "Target rule", "Trailing stop", "Stop-loss", "Time stop", "Score exit", "Overnight check",
            "Month-end rule", "Tripwire", "News trigger", "Price shock", "Opus guidance", "Event rule", "Safe mode",
            # strategy v2 (trend core)
            "Trend check", "Trend exit", "Rebalance", "Drawdown brake", "Account stop", "Strategy mode", "Tripwire"}


@dataclass
class Market:
    """Everything the tick needs to know about the outside world, already fetched."""
    now: datetime
    prices: dict[str, float]
    price_ts: dict[str, datetime]
    scores: dict[str, float]
    series: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict)   # today's minute closes
    prev_close: dict[str, float] = field(default_factory=dict)
    headlines: list[dict] = field(default_factory=list)                              # NEW headlines since last tick
    hist: dict[str, trend.Bars] = field(default_factory=dict)                        # strategy v2: completed daily bars per symbol


@dataclass
class Env:
    state_dir: Path
    logs_dir: Path
    params: dict = field(default_factory=config.load_params)
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    post: Callable = fastlane.http_post
    match_headline: Callable | None = _match_headline
    tick_seconds: int = 60


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _good_px(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _as_ist(d: datetime) -> datetime:
    """The engine's day and clock are IST. A UTC (or naive) timestamp is converted, never reinterpreted."""
    return d.astimezone(market.IST) if d.tzinfo else d.replace(tzinfo=market.IST)


def clean_market(mkt: Market, last_known: Mapping[str, float] | None = None) -> list[tuple[str, str]]:
    """Drop prices that cannot be real (NaN, inf, zero, negative) and single-minute spikes (MAX_TICK_JUMP).
    Mutates mkt; returns [(symbol, plain reason)] for what was dropped."""
    dropped: list[tuple[str, str]] = []
    mkt.now = _as_ist(mkt.now)
    for s, ser in list(mkt.series.items()):
        mkt.series[s] = [(t, float(v)) for t, v in (ser or []) if _good_px(v)]
    for d in (mkt.prev_close, mkt.scores):
        for s, v in list(d.items()):
            if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)) or (d is mkt.prev_close and v <= 0):
                d.pop(s)
    for s, px in list(mkt.prices.items()):
        if not _good_px(px):
            mkt.prices.pop(s)
            dropped.append((s, f"The {s} price feed returned an impossible value ({px!r}), so it was ignored this minute."))
            continue
        ser = mkt.series.get(s) or []
        prev = [v for _, v in (ser[:-1] if ser and ser[-1][1] == px else ser)][-5:]
        if len(prev) >= 3:
            med, limit = statistics.median(prev), MAX_TICK_JUMP
        else:                    # the first minutes of a session: compare with yesterday's close or the last known price
            med, limit = mkt.prev_close.get(s) or (last_known or {}).get(s), MAX_OPEN_JUMP
        if med and _good_px(med):
            if abs(px / med - 1) > limit:
                mkt.prices.pop(s)
                if ser and ser[-1][1] == px:          # and out of the shock detector: a glitch must not wake the AI
                    mkt.series[s] = ser[:-1]
                dropped.append((s, f"The {s} price {px:.2f} is {(px / med - 1) * 100:+.0f}% away from the recent price "
                                   f"({med:.2f}), which looks like a bad print, so it was ignored this minute."))
    return dropped


def _dt(s, tz) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=tz)


class Engine:
    def __init__(self, pf: Portfolio, mkt: Market, env: Env):
        self.pf, self.mkt, self.env, self.p = pf, mkt, env, env.params
        self.bad_prices = clean_market(mkt, {k: v.last_price for k, v in pf.positions.items() if v.last_price})
        self.now = mkt.now
        self.day = self.now.date()
        self.syms = list(self.p["instruments"])
        self.log: list[dict] = []
        self.check: dict[str, str] = {}     # one plain line per symbol per minute: logs/checks-DATE.jsonl
        self.rt = pf.runtime
        self.v2 = trend.is_v2(self.p)                       # strategy v2 (trend core) instead of the v1 score rules
        self.st = self.p.get("strategy") or {}
        self.v2_detail: dict = {}
        self.v2_halted = False                              # hard account stop in force this tick
        self.safe_mode = alerts.safe_mode_on(env.state_dir)   # re-read from disk on every tick
        self.fast_cap = budget.fastlane_cap(self.p, env.state_dir)   # thrift mode (state/budget.json) lowers it
        self.cd = pf.cooldowns
        self.er = guard.event_rules(self.day, guard.normalise_events(
            _load_json(env.state_dir / "events.json") or []), self.p)

    # ------------------------------------------------------------------ valuation and logging
    def price(self, sym: str) -> float | None:
        px = self.mkt.prices.get(sym)
        if px:
            return px
        pos = self.pf.positions.get(sym)
        return pos.last_price if pos and pos.last_price else None

    def value(self) -> float:
        """cash + LIQUIDBEES + positions at last price minus the cost to sell them (rulebook: Value)."""
        v = self.pf.cash + self.pf.liquidbees
        for sym, pos in self.pf.positions.items():
            px = pos.last_price or pos.avg_cost
            v += pos.qty * px * (1 - self.p["instruments"][sym]["cost"] / 2)
        return v

    def holdings(self) -> dict[str, tuple[float, float]]:
        return {s: (pos.qty, pos.last_price or pos.avg_cost) for s, pos in self.pf.positions.items()}

    def night_mult(self, sym: str) -> float:
        return guard.gap_multiplier(self.day, self.er.night_mult(sym), self.p)

    def record(self, action: str, sym: str, qty: int, price: float | None, reason: str, trigger: str,
               pnl: float | None = None, extra: dict | None = None) -> dict:
        pos = self.pf.positions.get(sym)
        rec = {
            "ts": self.now.isoformat(timespec="seconds"), "time": self.now.strftime("%H:%M"), "action": action,
            "symbol": sym, "qty": int(qty), "price": round(price, 2) if price else None,
            "amount": round(qty * price, 2) if price else 0.0,
            "pnl": round(pnl, 2) if pnl is not None else None, "reason": reason, "trigger": trigger,
            "stop": round(pos.ai_stop if self.v2 else pos.stop, 2) if pos and (pos.ai_stop if self.v2 else pos.stop) else None,
            "risk": round(guard.position_risk(sym, pos.qty, pos.last_price or pos.avg_cost, self.night_mult(sym), self.p), 2)
            if pos and sym in self.p["instruments"] else 0.0,
            "value_after": round(self.value(), 2), "floor": round(self.pf.floor, 2),
        }
        if self.v2 and pos and pos.exit_level:
            rec["exit_level"] = round(pos.exit_level, 2)
        if extra:
            rec.update(extra)
        self.log.append(rec)
        path = self.env.logs_dir / f"decisions-{self.day.isoformat()}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("could not write decision log")
        return rec

    # ------------------------------------------------------------------ paper fills
    def _fill(self, sym: str, side: str, qty: int, px: float, reason: str, trigger: str,
              stop_out: bool = False) -> dict | None:
        slip = self.p["valuation"]["paper_slippage"][sym]
        kind = self.p["engine"]["kinds"][sym]
        pf = self.pf
        if side == "BUY":
            fill = px * (1 + slip)
            while qty > 0 and qty * fill + trade_costs(qty * fill, "BUY", kind) > pf.cash + pf.liquidbees:
                qty -= 1
            if qty <= 0:
                return None
            amount = qty * fill
            cost = trade_costs(amount, "BUY", kind)
            pay = amount + cost
            from_cash = min(pf.cash, pay)
            pf.cash -= from_cash
            pf.liquidbees -= pay - from_cash
            pos = pf.positions.get(sym)
            if pos is None:
                pos = pf.positions[sym] = Position(opened=self.day.isoformat(), entry_ts=self.now.isoformat(timespec="seconds"))
                pos.avg_cost = fill
                pos.qty = qty
                pos.high_water = px
                pos.stop = 0.0 if self.v2 else guard.initial_stop_price(sym, fill, self.p)
            else:
                pos.avg_cost = (pos.avg_cost * pos.qty + amount) / (pos.qty + qty)
                pos.qty += qty
                pos.adds += 1
                if not pos.half_sold and not self.v2:
                    pos.stop = max(pos.stop, guard.initial_stop_price(sym, pos.avg_cost, self.p))
            pos.buy_costs += cost
            pos.last_price = px
            pf.total_costs += cost
            pf.day_costs += cost
            pf.trades.append({"ts": self.now.isoformat(timespec="seconds"), "asset": sym, "side": "BUY", "qty": qty,
                              "price": round(fill, 4), "value": round(amount, 2), "costs": round(cost, 2), "pnl": 0.0,
                              "reason": reason})
            return {"qty": qty, "fill": fill, "pnl": None}
        pos = pf.positions.get(sym)
        if not pos or qty <= 0:
            return None
        qty = min(qty, pos.qty)
        fill = px * (1 - slip)
        amount = qty * fill
        cost = trade_costs(amount, "SELL", kind)
        share = qty / pos.qty
        pnl = (fill - pos.avg_cost) * qty - cost - pos.buy_costs * share
        pf.liquidbees += amount - cost
        pf.realised_pnl += pnl
        pf.total_costs += cost
        pf.day_costs += cost
        pos.buy_costs *= 1 - share
        pos.qty -= qty
        pf.trades.append({"ts": self.now.isoformat(timespec="seconds"), "asset": sym, "side": "SELL", "qty": qty,
                          "price": round(fill, 4), "value": round(amount, 2), "costs": round(cost, 2),
                          "pnl": round(pnl, 2), "reason": reason})
        if pos.qty == 0:
            pf.positions.pop(sym)
        if pnl < 0:
            self.pf.counters["losses_today"] = self.pf.counters.get("losses_today", 0) + 1
        if stop_out:
            self.cd.setdefault("stopped_out", {})[sym] = self.day.isoformat()
        return {"qty": qty, "fill": fill, "pnl": pnl}

    def sell(self, sym: str, qty: int, reason: str, trigger: str, stop_out: bool = False) -> bool:
        px = self.price(sym)
        if not px:
            return False
        r = self._fill(sym, "SELL", qty, px, reason, trigger, stop_out)
        if not r:
            return False
        self.record("SELL", sym, r["qty"], r["fill"], reason, trigger, pnl=r["pnl"])
        return True

    # ------------------------------------------------------------------ 1. housekeeping
    def housekeeping(self):
        pf, c = self.pf, self.pf.counters
        self.v2_mode_switch()
        if c.get("date") != self.day.isoformat():
            old_week = c.get("buys_week", {})
            c.clear()
            c.update(guard.fresh_counters(self.day))
            wk = guard.week_key(self.day)
            same = old_week.get("key") == wk and old_week.get("month") == self.day.strftime("%Y-%m")
            c["buys_week"] = old_week if same else {"key": wk, "month": self.day.strftime("%Y-%m"), "n": {}}
            self.rt["shock_close_done"] = {}
            self.rt["value_hist"] = []
            self.rt.pop("overnight_done", None)
        for sym, pos in pf.positions.items():
            px = self.mkt.prices.get(sym)
            if px:
                pos.last_price = px
                pos.high_water = max(pos.high_water, px)
        if pf.day_costs_date != self.day.isoformat():     # first tick of the day: for the end-of-day report
            pf.day_costs_date = self.day.isoformat()
            pf.day_costs, pf.liquidbees_income_today = 0.0, 0.0
            pf.day_open_value = round(self.value(), 2)
        # LIQUIDBEES interest: yield/365 per calendar day, credited daily (compounded)
        y = self.p["capital"]["liquid_yield_annual"] / 365.0
        if not pf.last_accrual:
            pf.last_accrual = self.day.isoformat()
        else:
            days = (self.day - date.fromisoformat(pf.last_accrual)).days
            if days > 0:
                before = pf.liquidbees
                pf.liquidbees *= (1 + y) ** days
                pf.liquidbees_income_today += pf.liquidbees - before
                pf.last_accrual = self.day.isoformat()
        self.ratchet()
        pf.month = self.day.strftime("%Y-%m")

    def ratchet(self):
        """v1: the Green Guard floor ratchet. v2: no ratchet; the 'floor' is the fixed hard account stop."""
        pf = self.pf
        if self.v2:
            pf.floor = float(self.st["account_stop"])
            pf.best_profit = max(pf.best_profit, self.value() - self.p["capital"]["start"])
        else:
            pf.floor, pf.best_profit = guard.ratchet(pf.floor, pf.best_profit, self.value(), self.p)

    # ------------------------------------------------------------------ 2. triggers and AI hand-off
    def _block(self, sym: str, minutes: int, src: str, why: str):
        until = self.now + timedelta(minutes=minutes)
        syms = self.syms if sym == "ALL" else [sym]
        for s in syms:
            cur = _dt(self.cd.setdefault("block_until", {}).get(s), self.now.tzinfo)
            if not cur or until > cur:
                self.cd["block_until"][s] = until.isoformat()
                self.cd.setdefault("block_src", {})[s] = src
        self.record("BLOCK", sym, 0, self.price(sym) if sym != "ALL" else None, why, src)

    def triggers(self):
        pf, now = self.pf, self.now
        events: list[tuple[dict, str]] = []     # (trigger, src)
        for sym in self.syms:
            series = self.mkt.series.get(sym) or []
            done = bool(self.rt.setdefault("shock_close_done", {}).get(sym))
            active = _dt(self.cd.get("block_until", {}).get(sym), now.tzinfo)
            for t in fastlane.detect_price_shocks(sym, series, self.mkt.prev_close.get(sym), now, done, self.p):
                if t["kind"] == "from_close":
                    self.rt["shock_close_done"][sym] = True
                elif active and active > now and self.cd.get("block_src", {}).get(sym) == "Price shock":
                    continue            # same shock still in the window: already handled
                events.append((t, "Price shock"))
        hist = [(_dt(t, now.tzinfo), v) for t, v in self.rt.get("value_hist", [])]
        pt = fastlane.detect_portfolio_trigger(hist, self.value(), pf.floor, now, self.p)
        if pt and not (_dt(self.rt.get("portfolio_trigger_until"), now.tzinfo) or now) > now:
            events.append((pt, "Price shock"))
            self.rt["portfolio_trigger_until"] = (now + timedelta(minutes=30)).isoformat()
        if self.env.match_headline:
            cool = self.rt.setdefault("news_cooldown", {})
            for k in [k for k, v in cool.items() if not (_dt(v, now.tzinfo) or now) > now]:
                cool.pop(k, None)                      # expired
            for h in self.mkt.headlines:
                try:
                    m = self.env.match_headline(h.get("title", ""))
                except Exception:
                    log.exception("match_headline failed")
                    continue
                if m and m.get("wake"):
                    syms = [config.symbol_of(s) for s in m.get("symbols") or []] or ["ALL"]
                    cd_min = int(m.get("cooldown_minutes") or 0)
                    levels = m.get("levels") or {}
                    cats = m.get("wake_categories") or m.get("categories") or []
                    for s in syms:
                        if s == "ALL" or s in self.syms:
                            # a category (at the same urgency) blocks a symbol at most once per cooldown: the same
                            # Fed/RBI story from ten outlets no longer re-blocks every 10 minutes (14 Oct 2026)
                            keys = [f"{c}@{levels.get(c, '')}|{s}" for c in cats]
                            if cd_min > 0 and keys and all(k in cool for k in keys):
                                continue
                            if cd_min > 0:
                                for k in keys:
                                    cool[k] = (now + timedelta(minutes=cd_min)).isoformat()
                            events.append(({"kind": "news", "symbol": s, "headline": h.get("title", "")[:140],
                                            "urgency": m.get("urgency"), "categories": m.get("categories"),
                                            "detail": f"News headline matched a trigger ({', '.join(m.get('categories') or [])}): "
                                                      f"{h.get('title', '')[:120]}"}, "News trigger"))
        if not events:
            return
        runs = self.rt.setdefault("fast_runs", {})
        if runs.get("date") != self.day.isoformat():
            runs.clear()
            runs.update({"date": self.day.isoformat(), "n": 0, "last_ts": None})
        mins = (self.p["fast_lane"]["auto_block_when_limit_hit_min"] if fastlane.limit_used_up(runs, now, self.p, self.fast_cap)
                else self.p["fast_lane"]["auto_block_on_shock_min"])
        seen = set()
        for t, src in events:
            key = (t["symbol"], src)
            if key in seen:
                continue
            seen.add(key)
            where = "all instruments" if t["symbol"] == "ALL" else t["symbol"]
            if src == "News trigger":
                head = " ".join(str(t.get("headline") or "").split())[:140]
                cats = ", ".join(str(c) for c in (t.get("categories") or []))
                why = (f"A news headline looks market-moving, so new buys in {where} are paused for {mins} minutes "
                       f"while the AI reviews it." + (f' Headline: "{head}"' if head else "")
                       + (f" (category: {cats})." if cats else ""))
            elif t["kind"] == "portfolio":
                why = ((f"The portfolio value fell sharply within 15 minutes, so new buys are paused for {mins} minutes." if self.v2 else
                        f"The portfolio lost a large part of its safety cushion within 15 minutes, so new buys are paused "
                        f"for {mins} minutes."))
            else:
                why = f"{t['detail']} New buys in {where} are paused for {mins} minutes while the AI reviews it."
            self._block(t["symbol"], mins, src, why)
        ok, note = fastlane.can_fire(runs, now, self.p, self.fast_cap)
        rec = {"ts": now.isoformat(timespec="seconds"),
               "triggers": [{"source": s, **{k: v for k, v in t.items() if k != "detail"}, "detail": t["detail"]}
                            for t, s in events],
               "prices": {k: round(v, 2) for k, v in self.mkt.prices.items()},
               "positions": {s: {"qty": p.qty, "entry": round(p.avg_cost, 2), "stop": round(p.stop, 2)}
                             for s, p in pf.positions.items()},
               "value": round(self.value(), 2), "floor": round(pf.floor, 2), "fired": False}
        if self.v2:
            peak = float((self.v2_state().get("brake") or {}).get("peak") or self.p["capital"]["start"])
            rec["account_stop"], rec["drawdown_from_peak"] = rec["floor"], round(rec["value"] / max(peak, rec["value"]) - 1, 4)
        if ok:
            summary = {"trigger": [t["detail"] for t, _ in events][:4], "prices": rec["prices"], "positions": rec["positions"],
                       "value": rec["value"], "floor": rec["floor"], "time": now.strftime("%H:%M")}
            if self.v2:
                summary.update(account_stop=rec["account_stop"], drawdown_from_peak=rec["drawdown_from_peak"])
            attempted, note = fastlane.fire(self.env.environ, summary, self.env.post)
            if attempted:
                runs["n"] = runs.get("n", 0) + 1
                runs["last_ts"] = now.isoformat()
                rec["fired"] = True
        rec["fire_note"] = note
        fastlane.append_request(self.env.state_dir / "fastlane_requests.jsonl", rec)

    def fastlane_advice(self) -> list[dict]:
        """Read state/fastlane.json and keep only what the rulebook allows (5.3, defence only)."""
        path = self.env.state_dir / "fastlane.json"
        raw = _load_json(path)
        if not isinstance(raw, dict):
            return []
        upd = _dt(raw.get("updated"), self.now.tzinfo)
        if upd is None:
            try:
                upd = datetime.fromtimestamp(path.stat().st_mtime, self.now.tzinfo)
            except OSError:
                return []
        last = _dt(self.rt.get("fl_applied_updated"), self.now.tzinfo)
        if last and last > self.now + FUTURE_SLACK:      # poisoned by an earlier future-dated file: forget it
            last = None
        if upd > self.now + FUTURE_SLACK:
            if self.rt.get("fl_future_logged") != upd.isoformat():
                self.rt["fl_future_logged"] = upd.isoformat()
                self._log_rejected([f"updated {upd.isoformat()} is in the future; the whole file is ignored"])
            return []
        if last and upd <= last:
            return []
        if (self.now - upd) > timedelta(minutes=self.p["engine"]["fastlane_max_age_min"]):
            return []
        self.rt["fl_applied_updated"] = upd.isoformat()
        pos = {s: {"qty": p.qty, "price": p.last_price or p.avg_cost, "stop": p.ai_stop if self.v2 else p.stop}
               for s, p in self.pf.positions.items()}
        acc, rej = guard.sanitize_fastlane(raw, self.now, pos, self.p)
        if rej:
            self._log_rejected(rej)
        keep = []
        for a in acc:
            t = a["type"]
            if t == "block_until":
                mins = max(1, int((a["until"] - self.now).total_seconds() // 60))
                self._block(a["sym"], mins, "Opus guidance",
                            f"The AI fast lane asked to pause new buys in {a['sym']} until {a['until']:%H:%M}.")
            elif t == "set_bias":
                self.cd.setdefault("fl_bias", {})[a["sym"]] = {"bias": a["bias"], "set_at": self.now.isoformat()}
                if a["bias"] == "block":
                    self.record("BLOCK", a["sym"], 0, self.price(a["sym"]),
                                f"The AI fast lane set {a['sym']} to no new buys until the next hourly review.", "Opus guidance")
            elif t == "halt_buys_today":
                self.cd["halt_date"] = self.day.isoformat()
                self.record("BLOCK", "ALL", 0, None, "The AI fast lane stopped all new buys for the rest of the day.",
                            "Opus guidance")
            else:
                keep.append(a)
        return keep

    def _log_rejected(self, rej: list[str]) -> None:
        log.warning("fast-lane advice ignored: %s", "; ".join(rej))
        try:
            self.env.logs_dir.mkdir(parents=True, exist_ok=True)
            with open(self.env.logs_dir / f"advice-rejected-{self.day.isoformat()}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": self.now.isoformat(timespec="seconds"), "source": "fastlane.json",
                                     "rejected": rej}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ 3. exits
    def exits(self, advice: list[dict]):
        pf, now = self.pf, self.now
        value = self.value()
        if guard.is_tripwire(value, pf.floor, self.p):
            self.cd["no_buys_until"] = market.next_trading_day(self.day).isoformat()
            for sym in list(pf.positions):
                self.sell(sym, pf.positions[sym].qty,
                          f"Portfolio value ₹{value:,.0f} fell to within ₹{self.p['green_guard']['tripwire_buffer']} of the "
                          f"safety floor ₹{pf.floor:,.0f}, so everything was sold and moved to LIQUIDBEES.", "Tripwire")
            return
        for a in advice:                                # AI defence advice (already filtered)
            pos = pf.positions.get(a["sym"])
            if not pos:
                continue
            if a["type"] == "tighten_stop" and a["stop"] > pos.stop:
                pos.stop = a["stop"]
            elif a["type"] == "sell_fraction":
                q = pos.qty if a["fraction"] >= 0.99 else max(1, int(pos.qty * a["fraction"]))
                self.sell(a["sym"], q, f"The AI fast lane advised selling {a['fraction'] * 100:.0f}% of {a['sym']} "
                                       f"as a precaution, and the rules allow selling.", "Opus guidance")
        for sym in list(pf.positions):
            for _ in range(4):
                pos = pf.positions.get(sym)
                px = self.price(sym)
                if not pos or not px:
                    break
                pos.high_water = max(pos.high_water, px)
                held = market.trading_days_between(date.fromisoformat(pos.opened), self.day)
                act = guard.next_exit(sym, pos, px, now, self.mkt.scores.get(sym), held, self.p)
                if not act:
                    break
                if not self.sell(sym, act.qty, act.reason, act.trigger, stop_out=act.stop_out):
                    break
                left = pf.positions.get(sym)
                if left and act.trigger == "Target rule":      # rest of the position: break-even stop + trailing
                    left.half_sold = True
                    left.stop = max(left.stop, guard.breakeven_stop_price(sym, left.avg_cost, self.p))
        self.event_and_month_end_exits()

    def event_and_month_end_exits(self):
        pf, now, t = self.pf, self.now, self.now.time()
        for sym, (at, why) in self.er.forced_exits.items():
            if sym in pf.positions and t >= at:
                self.sell(sym, pf.positions[sym].qty, why, "Event rule")
        in_window = guard.in_month_end_window(self.day, self.p)
        if in_window and pf.positions and guard.month_end_flatten(self.value(), self.p):
            self.rt["month_flat"] = self.day.strftime("%Y-%m")
            for sym in list(pf.positions):
                self.sell(sym, pf.positions[sym].qty,
                          f"Value fell under ₹{self.p['month_end']['flatten_if_value_below']:,} in the last days of the month, "
                          f"so everything was sold to protect a green month end.", "Month-end rule")
        me = self.p["month_end"]
        if market.is_last_trading_day_of_month(self.day) and t >= guard.hhmm(me["last_day_exit_time"]) and pf.positions:
            held = {s: (p.qty, p.last_price, p.stop) for s, p in pf.positions.items()}
            for sym in guard.last_day_stop_sells(held, self.value(), self.p):
                self.sell(sym, pf.positions[sym].qty,
                          f"It is the last trading day of the month and a stop-out on {sym} could pull the month-end "
                          f"value under ₹{me['last_day_min_after_stop']:,}, so it was sold.", "Month-end rule")
        if t >= guard.hhmm(self.p["daily_limits"]["overnight_check_time"]) and self.rt.get("overnight_done") != self.day.isoformat():
            self.rt["overnight_done"] = self.day.isoformat()
            self.overnight_check(in_window)

    def overnight_check(self, in_window: bool):
        pf = self.pf
        value = self.value()
        liquid = pf.cash + pf.liquidbees
        for sym in guard.overnight_sells(self.holdings(), value, pf.floor, liquid, self.day, self.night_mult, self.p):
            if sym in pf.positions:
                self.sell(sym, pf.positions[sym].qty,
                          f"Holding {sym} overnight with tonight's gap allowance would use more risk budget than is "
                          f"left, so it was sold at the 15:20 check.", "Overnight check")
        if in_window and pf.positions:
            for sym in guard.month_end_sells(self.holdings(), self.value(), pf.floor, self.night_mult, self.p):
                if sym in pf.positions:
                    self.sell(sym, pf.positions[sym].qty,
                              f"A worst-case overnight gap on {sym} could pull the month-end value under "
                              f"₹{guard.month_end_required(pf.floor, self.p):,.0f}, so it was sold.", "Month-end rule")
        for sym, pos in pf.positions.items():
            m = self.night_mult(sym)
            self.record("HOLD", sym, pos.qty, pos.last_price,
                        f"The 15:20 overnight check passed: the worst-case overnight loss on {sym} "
                        f"(₹{guard.position_risk(sym, pos.qty, pos.last_price, m, self.p):,.0f}) fits inside the risk budget, "
                        f"so it is kept.", "Overnight check")

    # ------------------------------------------------------------------ 4. entries
    def guidance(self) -> tuple[dict, bool, datetime | None]:
        g = _load_json(self.env.state_dir / "guidance.json")
        if not isinstance(g, dict):
            return {}, False, None
        upd = _dt(g.get("updated"), self.now.tzinfo)
        fresh = bool(upd) and (self.now - upd) <= timedelta(minutes=self.p["entry"]["stale_guidance_minutes"]) \
            and (upd - self.now) <= timedelta(minutes=5)
        raw = g.get("bias")
        bias = {config.symbol_of(k): guard.normalise_bias(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        return bias, fresh, upd

    def effective_bias(self, sym: str, bias: dict, fresh: bool, upd: datetime | None) -> str:
        b = bias.get(sym, "neutral") if fresh else "neutral"
        fl = self.cd.get("fl_bias", {}).get(sym)
        if fl:
            set_at = _dt(fl["set_at"], self.now.tzinfo)
            if not upd or (set_at and set_at > upd):
                if guard.BIAS_RANK[fl["bias"]] > guard.BIAS_RANK[b]:
                    b = fl["bias"]
        return b

    def _noisy_skip(self, kind: str, sym: str, reason: str, trigger: str, px: float | None):
        last = _dt(self.cd.setdefault("skip_ts", {}).get(sym), self.now.tzinfo)
        if last and (self.now - last) < timedelta(minutes=30):
            return
        self.cd["skip_ts"][sym] = self.now.isoformat()
        self.record(kind, sym, 0, px, reason, trigger)

    def entries(self):
        pf, now, p = self.pf, self.now, self.p
        bias, fresh, upd = self.guidance()
        streaks = self.rt.setdefault("streaks", {})
        for sym in self.syms:
            px = self.mkt.prices.get(sym)
            score = self.mkt.scores.get(sym)
            if px is None or score is None:
                streaks.pop(sym, None)
                self.check[sym] = "No fresh price or score this minute, so no buy check."
                continue
            b = self.effective_bias(sym, bias, fresh, upd)
            thr = guard.score_threshold(sym, b, fresh, p)
            if thr is None:
                streaks.pop(sym, None)
                self.check[sym] = f"Score {score:.2f}; Claude's bias for this metal is block, so no buys."
                continue
            if score < thr - 1e-9:
                streaks.pop(sym, None)
                self.check[sym] = (f"Score {score:.2f} is below the {thr:.2f} needed to buy ({b} bias"
                                   f"{'' if fresh else ', guidance stale'}), so no buy.")
                continue
            st = streaks.get(sym)
            max_gap = max(p["engine"]["streak_max_gap_sec"], 2.5 * self.env.tick_seconds)
            last_seen = _dt(st["last"], now.tzinfo) if st else None
            if not st or (now - last_seen).total_seconds() > max_gap:
                st = streaks[sym] = {"start": now.isoformat(), "last": now.isoformat()}
            st["last"] = now.isoformat()
            held_secs = (now - _dt(st["start"], now.tzinfo)).total_seconds()
            need = p["entry"]["score_confirm_minutes"]
            if held_secs < need * 60 - 5:
                self.check[sym] = (f"Score {score:.2f} reached the {thr:.2f} needed; waiting for it to hold "
                                   f"{need} minutes ({int(held_secs // 60) + 1} of {need}).")
                continue
            self.check[sym] = f"Score {score:.2f} held above {thr:.2f} for {need} minutes; buy checks run."
            self.try_buy(sym, px, score, thr)

    def try_buy(self, sym: str, px: float, score: float, thr: float):
        pf, now, p = self.pf, self.now, self.p
        pos = pf.positions.get(sym)
        if pos:   # adding: at most once, only after reaching half the target
            if pos.adds >= p["positions"]["max_adds"] or guard.target_progress(
                    sym, pos.avg_cost, pos.high_water, p) < p["positions"]["add_requires_progress"]:
                return
        value, floor = self.value(), pf.floor
        c = pf.counters
        t = now.time()

        def skip(why, trig="Minute check", kind="SKIP"):
            self.check[sym] = why
            self._noisy_skip(kind, sym, why, trig, px)

        age = (now - self.mkt.price_ts[sym]).total_seconds() / 60 if sym in self.mkt.price_ts else 0.0
        if age > p["entry"]["max_price_age_min"]:
            return skip(f"The latest {sym} price is {age:.0f} minutes old, older than the {p['entry']['max_price_age_min']} minute limit.")
        ym = now.strftime("%Y-%m")
        if self.rt.get("month_flat") == ym:
            return skip("Everything was sold earlier this month to protect a green month end, so no more buys this month.")
        td, left = market.trading_day_of_month(self.day), market.trading_days_left_in_month(self.day)
        if guard.week_factor(td, left, p) == 0.0:
            return skip("These are the last 3 trading days of the month, so no new buys are made.")
        if self.safe_mode:
            return skip("The system is in safe mode after an incident, so no new buys (exits, stops and the floor still work).",
                        "Safe mode", "BLOCK")
        nb = self.cd.get("no_buys_until")
        if nb and self.day.isoformat() < nb:
            return skip(f"The tripwire sold everything recently, so no buys until {nb}.")
        if guard.in_caution_zone(value, floor, p):
            return skip(f"Portfolio value ₹{value:,.0f} is within ₹{p['green_guard']['caution_buffer']} of the safety floor "
                        f"₹{floor:,.0f}, so no new buys.")
        if self.cd.get("halt_date") == self.day.isoformat():
            return skip("The AI fast lane stopped all new buys for today.", "Opus guidance", "BLOCK")
        for key in (sym, "ALL"):
            until = _dt(self.cd.get("block_until", {}).get(key), now.tzinfo)
            if until and until > now:
                src = self.cd.get("block_src", {}).get(key, "Price shock")
                what = {"News trigger": "a market-moving news headline was flagged",
                        "Opus guidance": "the AI fast lane asked for a pause"}.get(src, "a fast price move was flagged")
                return skip(f"Buying {sym} is paused until {until:%H:%M} because {what}.", src, "BLOCK")
        ok, why = guard.in_buy_window(t, p)
        if not ok:
            return skip(why)
        why = self.er.buy_block(sym, t)
        if why:
            return skip(why, "Event rule", "BLOCK")
        if not pos and len(pf.positions) >= p["positions"]["max_open"]:
            return skip("The maximum of 3 open positions is already reached.")
        lim = guard.check_buy_limits(sym, now, c, self.cd, c.get("buys_week", {}).get("n", {}), p)
        if lim:
            return skip(lim[1])
        slip = p["valuation"]["paper_slippage"][sym]
        fill_px = px * (1 + slip)
        sz = guard.size_buy(sym, fill_px, value, floor, pf.cash + pf.liquidbees, pf.cash + pf.liquidbees,
                            self.holdings(), self.day, self.night_mult, p)
        if sz.qty < 1:
            left = max(0.0, sz.usable - sz.used)
            what = (f"only ₹{left:,.0f} of risk budget is left" if sz.limiter == "risk"
                    else f"the {sz.limiter} leaves no room")
            return skip(f"The score is high enough, but {what}, which is not enough for even one share of {sym}.")
        why = (f"The buy score for {sym} stayed at {score:.2f} or higher (needs {thr:.2f}) for 3 minutes, and "
               f"the risk budget allows {sz.qty} shares.")
        r = self._fill(sym, "BUY", sz.qty, px, why, "Minute check")
        if not r:
            return skip("There was not enough free cash to place the buy.")
        c["buys_today"] = c.get("buys_today", 0) + 1
        c.setdefault("buys_sym_today", {})[sym] = c["buys_sym_today"].get(sym, 0) + 1
        c.setdefault("buys_week", {"key": guard.week_key(self.day), "month": ym, "n": {}})["n"][sym] = \
            c["buys_week"]["n"].get(sym, 0) + 1
        self.cd["last_buy_ts"] = now.isoformat()
        self.rt.get("streaks", {}).pop(sym, None)
        self.record("BUY", sym, r["qty"], r["fill"], why, "Minute check")


    # ------------------------------------------------------------------ strategy v2: trend core (docs/RULEBOOK.md "Strategy v2")
    def v2_state(self) -> dict:
        return self.rt.setdefault("v2", {})

    def v2_mode_switch(self):
        """Mode-specific state is reset when strategy.mode changes, in either direction (one log line)."""
        cur = "v2_trend" if self.v2 else "v1_scores"
        if self.rt.get("strategy_mode", "v1_scores") == cur:
            return
        self.rt["strategy_mode"] = cur
        if self.v2:
            self.rt.pop("v2", None)                     # an old brake peak from months ago must not come back
            for pos in self.pf.positions.values():      # v1 stops keep protecting the position from the very first v2 tick
                pos.ai_stop = max(pos.ai_stop, pos.stop)
                pos.stop, pos.exit_level = 0.0, 0.0
            msg = ("Strategy mode changed to v2 trend core: strategy v2 active. The fixed account stop is now the floor, "
                   "the drawdown brake starts from today's value, and any v1 stop on an open position keeps protecting it every minute.")
        else:
            for sym, pos in self.pf.positions.items():  # v2's far-away trend level must not become a loosened v1 stop
                pos.stop, pos.ai_stop, pos.exit_level, pos.half_sold = guard.initial_stop_price(sym, pos.avg_cost, self.p), 0.0, 0.0, False
            msg = "Strategy mode changed to v1 score rules: every open position got its v1 initial stop back."
        self.record("HOLD", "ALL", 0, None, msg, "Strategy mode")

    def v2_infos(self) -> dict:
        """Trend state per symbol from completed daily closes plus the live price as today's provisional close.
        None for a symbol without a good live price or enough daily history."""
        out = {}
        for sym in self.syms:
            px, bars = self.mkt.prices.get(sym), self.mkt.hist.get(sym)
            kind = self.st["symbols"][sym]["signal"]
            try:
                out[sym] = trend.compute(sym, kind, bars, px, self.st) if (px and bars) else None
            except Exception:
                log.exception("trend state failed for %s", sym)
                out[sym] = None
        return out

    def price_age(self, sym: str) -> float:
        ts = self.mkt.price_ts.get(sym)
        return (self.now - ts).total_seconds() / 60 if ts else 0.0

    def fresh(self, sym: str) -> bool:
        return sym in self.mkt.prices and self.price_age(sym) <= self.p["entry"]["max_price_age_min"]

    def v2_weights(self, infos: dict, level: int, reconfirm) -> tuple[dict, float, float]:
        mult = trend.brake_mult(level, self.st)
        w, scale, self.v2_detail = trend.target_weights_ex({s: (i.on, i.sigma) for s, i in infos.items() if i}, self.st, mult, reconfirm)
        return w, scale, mult

    def cur_weight(self, sym: str, value: float) -> float:
        pos, px = self.pf.positions.get(sym), self.price(sym)
        return pos.qty * px / value if pos and px and value > 0 else 0.0

    def v2_sell(self, sym: str, qty: int, reason: str, trigger: str, stop_out: bool = False) -> bool:
        return self.sell(sym, qty, f"{reason} [price age {self.price_age(sym):.0f} min]", trigger, stop_out)

    def v2_no_rebuy(self, sym: str):
        """After a fast-lane, AI-stop or event sell the engine must not undo it: no buys of that symbol until the next trading day."""
        self.cd.setdefault("v2_no_rebuy", {})[sym] = market.next_trading_day(self.day).isoformat()

    def v2_account_suspended(self) -> bool:
        clear = self.st.get("account_stop_clear_after")
        return bool(clear) and str(clear) >= self.day.isoformat()

    def v2_account_latched(self) -> bool:
        lat = self.cd.get("account_stop_latched")
        if not isinstance(lat, dict):
            return False
        clear = self.st.get("account_stop_clear_after")
        return not (clear and str(clear) >= str(lat.get("date")))

    def v2_exits(self, advice: list[dict]):
        """Every tick: defence that does not wait for the daily check. The fast lane's sell advice, the AI stops
        (tighten_stop, enforced every minute), the HINDCOPPER results exit and the hard account stop. The trend exits
        happen at the daily check (v2_tick). No tripwire, no overnight or month-end rules in v2."""
        pf = self.pf
        for a in advice:
            pos = pf.positions.get(a["sym"])
            if not pos:
                continue
            if a["type"] == "sell_fraction":
                q = pos.qty if a["fraction"] >= 0.99 else max(1, int(pos.qty * a["fraction"]))
                if self.v2_sell(a["sym"], q, f"The AI fast lane advised selling {a['fraction'] * 100:.0f}% of {a['sym']} "
                                             f"as a precaution, and the rules allow selling.", "Opus guidance"):
                    self.v2_no_rebuy(a["sym"])
            elif a["type"] == "tighten_stop" and a["stop"] > pos.ai_stop:
                pos.ai_stop = a["stop"]
                self.record("HOLD", a["sym"], pos.qty, pos.last_price,
                            f"The AI fast lane tightened the {a['sym']} stop to ₹{a['stop']:,.2f}; it is checked every minute and the "
                            f"whole position is sold at or under it.", "Opus guidance")
        for sym, pos in list(pf.positions.items()):
            px = self.mkt.prices.get(sym)
            if pos.ai_stop > 0 and px and px <= pos.ai_stop:
                if self.v2_sell(sym, pos.qty, f"{sym} price ₹{px:,.2f} is at or under the stop ₹{pos.ai_stop:,.2f} "
                                              f"(set by the AI fast lane or inherited from v1), so it was sold.", "Opus guidance", stop_out=True):
                    self.v2_no_rebuy(sym)
        for sym, (at, why) in self.er.forced_exits.items():
            if sym in pf.positions and self.now.time() >= at:
                if self.v2_sell(sym, pf.positions[sym].qty, why, "Event rule"):
                    self.v2_no_rebuy(sym)
        self.v2_account_stop()

    def v2_account_stop(self):
        """Value at or under the hard account stop: SELL EVERYTHING at once, halt buys, HIGH alert. It latches until the owner
        clears it (strategy.account_stop_clear_after, an ISO date on or after the day it fired, in a config commit)."""
        stop, value = float(self.st["account_stop"]), self.value()
        fired = False
        if value <= stop and not self.v2_account_suspended() and not self.v2_account_latched():
            self.cd["account_stop_latched"] = {"date": self.day.isoformat(), "value": round(value, 2)}
            why = (f"Portfolio value ₹{value:,.0f} is at or under the hard account stop ₹{stop:,.0f}, so everything is sold and all "
                   f"buys stay halted until the owner clears the stop (strategy.account_stop_clear_after).")
            self.record("BLOCK", "ALL", 0, None, why, "Account stop")
            try:
                alerts.raise_alert(self.env.state_dir, "engine", "account_stop", "account_stop", why)
            except Exception:       # noqa: BLE001 - an alert problem must never stop trading
                log.exception("could not raise the account-stop alert")
            fired = True
        self.v2_halted = fired or self.v2_account_latched()
        if self.v2_halted:
            for sym in list(self.pf.positions):
                self.v2_sell(sym, self.pf.positions[sym].qty, f"The hard account stop ₹{stop:,.0f} is in force, so {sym} was sold.", "Account stop")

    def v2_buy_blocker(self, sym: str, px: float) -> tuple[str, str, bool] | None:
        """Why a NEW buy or add must wait: (plain reason, trigger, retry_today) or None. Exits are never blocked.
        retry_today is True for a pause that ends before the check window closes: the symbol stays pending."""
        now = self.now
        if not self.fresh(sym):
            return (f"The latest {sym} price is {self.price_age(sym):.0f} minutes old, older than the "
                    f"{self.p['entry']['max_price_age_min']} minute limit.", "Trend check", True)
        if self.safe_mode:
            return "The system is in safe mode after an incident, so no new buys (exits still work).", "Safe mode", False
        if self.v2_halted:
            return f"The hard account stop ₹{self.st['account_stop']:,.0f} is in force, so no new buys.", "Account stop", False
        nb = self.cd.get("no_buys_until")
        if nb and self.day.isoformat() < nb:
            return f"A v1 tripwire sold everything recently, so no buys until {nb}.", "Tripwire", False
        nr = self.cd.get("v2_no_rebuy", {}).get(sym)
        if nr and self.day.isoformat() < nr:
            return (f"{sym} was sold today by the fast lane, a stop or an event rule, so it is not bought back before {nr}.",
                    "Opus guidance", False)
        if self.cd.get("halt_date") == self.day.isoformat():
            return "The AI fast lane stopped all new buys for today.", "Opus guidance", False
        end = datetime.combine(self.day, guard.hhmm(self.st["check_until"]), tzinfo=now.tzinfo)
        for key in (sym, "ALL"):
            until = _dt(self.cd.get("block_until", {}).get(key), now.tzinfo)
            if until and until > now:
                src = self.cd.get("block_src", {}).get(key, "Price shock")
                what = {"News trigger": "a market-moving news headline was flagged",
                        "Opus guidance": "the AI fast lane asked for a pause"}.get(src, "a fast price move was flagged")
                return f"Buying {sym} is paused until {until:%H:%M} because {what}.", src, until < end
        why = self.er.buy_block(sym, now.time())
        if why:
            return why, "Event rule", False
        return None

    def v2_tick(self):
        """Every minute: refresh the per-symbol check line, then run the daily trend check.
        Per symbol: 15:10 to 15:25 a symbol is checked as soon as it has a FRESH price (a stale or missing price waits and
        retries every minute). After 15:25 only exits and trims are made, until the close. If a whole day was missed, an
        exits-only catch-up runs on the first tick of the next day."""
        value = self.value()
        infos = self.v2_infos()
        v2 = self.v2_state()
        br = v2.get("brake") or {}
        targets, scale, mult = self.v2_weights(infos, int(br.get("level") or 0), br.get("reconfirm") or [])
        today = self.day.isoformat()
        t = self.now.time()
        t0, t1 = guard.hhmm(self.st["check_time"]), guard.hhmm(self.st["check_until"])
        for sym, i in infos.items():
            px = self.mkt.prices.get(sym)
            if i is None:
                self.check[sym] = ("No fresh price this minute, so no trend check." if not px
                                   else "Not enough daily history yet for the trend check.")
                continue
            state, detail = trend.describe(i, self.st)
            self.check[sym] = (f"Trend {state}: {detail}, target {targets.get(sym, 0.0) * 100:.0f}% "
                               f"(now {self.cur_weight(sym, value) * 100:.0f}%).")
        done = v2.get("done")
        if not isinstance(done, dict) or done.get("date") != today:
            done = v2["done"] = {"date": today, "syms": {}, "blk": [], "skip": []}
        lf, prev_day = v2.get("last_full"), market.prev_trading_day(self.day).isoformat()
        if lf and lf < prev_day and self.pf.positions and not (t0 <= t <= t1):
            # the previous day's check was missed (outage, late cron): exits and trims only, on the first tick
            held = [s for s in self.pf.positions if infos.get(s)]
            blind = [s for s in self.pf.positions if not infos.get(s)]          # held but no price / history yet: retried next tick
            if held:
                self.v2_decide(infos, value, held, "exits", {"date": today, "syms": {}, "blk": [], "skip": []}, log_target=False)
            if not blind:
                v2["last_full"] = prev_day
        if t0 <= t <= t1:
            ready = [s for s in self.syms if infos[s] and self.fresh(s) and s not in done["syms"]]
            if ready:
                self.v2_decide(infos, value, ready, "full", done)
            if t >= t1:
                for s in self.syms:
                    if s not in done["syms"] and s not in done["skip"]:
                        done["skip"].append(s)
                        self.record("SKIP", s, 0, self.mkt.prices.get(s),
                                    f"No fresh price and daily history for {s} by the end of the check window, so no trend check was made "
                                    f"today; exits are retried every minute until the close.", "Trend check")
        elif t > t1:
            late = [s for s in self.syms if infos[s] and s not in done["syms"]]
            if late:
                self.v2_decide(infos, value, late, "exits", done)
            for s in self.syms:
                if s not in done["syms"] and s not in done["skip"] and not infos[s]:
                    done["skip"].append(s)
                    self.record("SKIP", s, 0, self.mkt.prices.get(s),
                                f"No price and daily history for {s} at the check, so no trend check was made; exits retry every minute.",
                                "Trend check")
        if len(done["syms"]) == len(self.syms):
            v2["last_full"] = today

    def v2_why(self, sym: str, i, w: float, scale: float, mult: float, dd: float) -> str:
        """'GOLDBEES trend ON (close 121.40 > SMA200 118.90); target 87% = min(1, 0.14/0.161)'"""
        st = self.st
        state, detail = trend.describe(i, st)
        cap = st["symbols"][sym]["cap"]
        out = f"{sym} trend {state} ({detail}); "
        dt = self.v2_detail
        if sym == st.get("floor_symbol", "GOLDBEES") and dt.get("floor"):
            mi = st["min_invested"]
            return (f"{sym} floor: metals {dt['metals_before'] * 100:.0f}% < min_invested {mi * 100:.0f}% → target {w * 100:.0f}% gold "
                    f"(trend {state}, floor holding; {detail})"
                    + (f"; includes {dt['to_gold'] * 100:.0f}% handed over by {', '.join(dt['to_gold_from'])}" if dt["to_gold"] else "")
                    + (f" x {mult:g} (drawdown brake, value {dd * 100:.1f}% from its peak)" if mult != 1.0 else ""))
        if sym == st.get("floor_symbol", "GOLDBEES") and dt.get("to_gold"):
            out += (f"stop moves to gold: {', '.join(dt['to_gold_from'])} OFF hands over {dt['to_gold'] * 100:.0f}% while gold is above its SMA; ")
        if i.on and i.sigma:
            out += f"target {w * 100:.0f}% = min({cap:g}, {st['vol_target']:g}/{i.sigma:.3f})"
            if scale < 1 - 1e-9:
                out += f" x {scale:.2f} (scaled so the total stays within 100%)"
            if mult != 1.0:
                out += f" x {mult:g} (drawdown brake, value {dd * 100:.1f}% from its peak)"
        else:
            out += f"target {w * 100:.0f}%"
        return out

    def v2_brake(self, infos: dict, value: float) -> dict:
        """The drawdown brake, advanced once per trading day (it counts days)."""
        v2, st = self.v2_state(), self.st
        if v2.get("brake_day") == self.day.isoformat() and isinstance(v2.get("brake"), dict):
            return v2["brake"]
        on = {s: i.on for s, i in infos.items() if i}
        br = trend.brake_update(v2.get("brake"), value, on, st, self.p["capital"]["start"],
                                account_stop=self.v2_halted or self.v2_account_latched())
        v2["brake"] = {k: v for k, v in br.items() if k != "changed"}
        v2["brake_day"] = self.day.isoformat()
        if br["changed"]:
            lvl, dd, bk = br["level"], br["dd"], st["brake"]
            msg = {0: f"The drawdown brake is released: value ₹{value:,.0f} is {dd * 100:.1f}% from its peak ₹{br['peak']:,.0f}, so targets are back to full size.",
                   1: f"Drawdown brake level 1: value ₹{value:,.0f} is {dd * 100:.1f}% from its peak ₹{br['peak']:,.0f}, so every target is cut to {bk['level1_mult']:g}x"
                      + (" (the peak was re-based to today's value after a long pause at level 2)." if br.get("rebase_value") else "."),
                   2: f"Drawdown brake level 2: value ₹{value:,.0f} is {dd * 100:.1f}% from its peak ₹{br['peak']:,.0f}, so all targets are 0 until value recovers above "
                      f"-{bk['level2_release_dd'] * 100:.0f}% or {bk['level2_min_days']} trading days pass and a trend re-confirms."}[lvl]
            self.record("BLOCK" if lvl else "HOLD", "ALL", 0, None, msg, "Drawdown brake")
        return v2["brake"]

    def v2_decide(self, infos: dict, value: float, run_syms: list, mode: str, done: dict, log_target: bool = True):
        """Decide and trade `run_syms`. mode "full": entries, adds, trims and exits. mode "exits": trims and exits only."""
        pf, st = self.pf, self.st
        br = self.v2_brake(infos, value)
        dd = value / max(float(br.get("peak") or value), 1.0) - 1.0
        targets, scale, mult = self.v2_weights(infos, br["level"], br.get("reconfirm") or [])
        bias, fresh, upd = self.guidance()
        plan = []
        for sym in run_syms:
            i = infos[sym]
            pos = pf.positions.get(sym)
            cur = self.cur_weight(sym, value)
            d = trend.decide(on=i.on, target=targets[sym], cur=cur, flat=not pos or pos.qty <= 0, rsi_val=i.rsi,
                             bias=self.effective_bias(sym, bias, fresh, upd), st=st, brake_level=br["level"],
                             eligible=sym not in (br.get("reconfirm") or []), sigma_ok=bool(i.sigma and i.sigma > 0))
            plan.append((sym, i, cur, d))
        for sym, i, cur, d in plan:                                     # sells first: they free the money for the buys
            if d.action != "SELL":
                continue
            pos = pf.positions.get(sym)
            px = self.price(sym)
            if not pos or not px:
                continue
            qty = pos.qty if d.kind == "exit" else max(0, int((cur - d.to_w) * value / px))
            if qty < 1:
                continue
            base = self.v2_why(sym, i, targets[sym], scale, mult, dd)
            what = f"selling all {qty} units" if qty >= pos.qty else f"selling {qty} units to get back to the target"
            trig = "Drawdown brake" if br["level"] == 2 else ("Trend exit" if not i.on and targets[sym] <= 0 else
                                                              ("Drawdown brake" if br["level"] else "Rebalance"))
            self.v2_sell(sym, qty, f"{base}; {what} ({d.why})", trig)
        retry = set()
        for sym, i, cur, d in plan:
            pos = pf.positions.get(sym)
            if pos:
                pos.exit_level = i.exit_level or 0.0                    # informational: where the trend turns OFF
            if mode != "full":
                continue
            px = self.mkt.prices.get(sym)
            if d.action == "SKIP":
                self.record("SKIP", sym, 0, px, f"{self.v2_why(sym, i, targets[sym], scale, mult, dd)}; no buy: {d.why}", "Trend check")
                continue
            if d.action != "BUY" or not px:
                continue
            base = self.v2_why(sym, i, targets[sym], scale, mult, dd)
            blk = self.v2_buy_blocker(sym, px)
            if blk:
                if sym not in done["blk"]:
                    done["blk"].append(sym)
                    self.record("BLOCK", sym, 0, px, f"{base}; no buy: {blk[0]}", blk[1])
                if blk[2] and self.now.time() < guard.hhmm(st["check_until"]):
                    retry.add(sym)                                       # the pause ends before the window closes: try again
                continue
            fill_px = px * (1 + self.p["valuation"]["paper_slippage"][sym])
            qty = int((d.to_w - cur) * value / fill_px)
            if qty < 1:
                continue
            why = f"{base}; buying {qty} units to reach target ({d.why})"
            r = self._fill(sym, "BUY", qty, px, why, "Trend check")
            if not r:
                self.record("SKIP", sym, 0, px, f"{base}; there was not enough free cash to place the buy.", "Trend check")
                continue
            c = pf.counters
            c["buys_today"] = c.get("buys_today", 0) + 1
            c.setdefault("buys_sym_today", {})[sym] = c["buys_sym_today"].get(sym, 0) + 1
            self.cd["last_buy_ts"] = self.now.isoformat()
            if sym in pf.positions:
                pf.positions[sym].exit_level = i.exit_level or 0.0
            self.record("BUY", sym, r["qty"], r["fill"], why, "Trend check")
        value = self.value()
        for sym, i, cur, d in plan:                                     # the daily TARGET record per symbol
            if sym in retry:
                continue
            done["syms"][sym] = "full" if mode == "full" else "exits"
            if not log_target:
                continue
            state, detail = trend.describe(i, st)
            act = self.cur_weight(sym, value)
            sg = i.sigma
            floored = sym == st.get("floor_symbol", "GOLDBEES") and (self.v2_detail.get("floor") or self.v2_detail.get("to_gold"))
            head = (self.v2_why(sym, i, targets[sym], scale, mult, dd) if floored else f"{sym} trend {state} ({detail})")
            self.record("TARGET", sym, 0, self.price(sym),
                        f"{head}; sigma60 {('%.3f' % sg) if sg else 'n/a'}; "
                        f"target {targets[sym] * 100:.0f}%, actual {act * 100:.0f}%" + ("" if mode == "full" else " (exits-only check)"),
                        "Trend check", extra={"state": state, "sigma": round(sg, 4) if sg else None,
                                              "target_pct": round(targets[sym] * 100, 1), "actual_pct": round(act * 100, 1)})

    # ------------------------------------------------------------------ 5. snapshot
    def snapshot(self):
        pf = self.pf
        path = self.env.logs_dir / f"ticks-{self.day.isoformat()}.csv"
        px_cols = list(self.syms)
        try:
            new = not path.exists()
            if not new:      # a file started before the price columns existed keeps its old shape
                with open(path, encoding="utf-8") as fh:
                    if fh.readline().strip().split(",")[-1:] != [px_cols[-1]]:
                        px_cols = []
            with open(path, "a", encoding="utf-8") as fh:
                if new:
                    fh.write("time,value,floor,cash,liquidbees," + ",".join(px_cols) + "\n")
                row = f"{self.now:%H:%M},{self.value():.2f},{pf.floor:.2f},{pf.cash:.2f},{pf.liquidbees:.2f}"
                for s in px_cols:
                    px = self.mkt.prices.get(s)
                    row += f",{px:.2f}" if px else ","
                fh.write(row + "\n")
        except OSError:
            log.exception("could not write tick snapshot")
        self.write_checks()
        self.write_last_prices()
        vh = self.rt.setdefault("value_hist", [])
        vh.append([self.now.isoformat(timespec="seconds"), round(self.value(), 2)])
        cutoff = self.now - timedelta(minutes=self.p["fast_lane"]["portfolio_drop_window_min"] + 5)
        self.rt["value_hist"] = [x for x in vh if _dt(x[0], self.now.tzinfo) >= cutoff]

    def write_checks(self):
        """logs/checks-DATE.jsonl: what the engine concluded for each symbol this minute, even when it did nothing.
        Decisions (BUY/SELL/SKIP/BLOCK/HOLD) stay in decisions-DATE.jsonl; this is the 'why nothing happened' trail."""
        now, out = self.now, {}
        for sym in self.syms:
            parts = []
            px = self.mkt.prices.get(sym)
            if px:
                parts.append(f"Price ₹{px:,.2f}.")
            pos = self.pf.positions.get(sym)
            if pos and pos.qty > 0:
                parts.append(f"Holding {pos.qty} at avg {pos.avg_cost:.2f}, stop {pos.stop:.2f}.")
            for key in (sym, "ALL"):
                until = _dt(self.cd.get("block_until", {}).get(key), now.tzinfo)
                if until and until > now and not self.check.get(sym, "").startswith("Buying"):
                    src = self.cd.get("block_src", {}).get(key, "Price shock")
                    parts.append(f"Buys paused until {until:%H:%M} ({src}).")
                    break
            for d in self.log:
                if d.get("symbol") == sym:
                    parts.append(f"{d['action']}: see decision.")
            parts.append(self.check.get(sym) or "Checked; nothing to do.")
            out[sym] = " ".join(parts)[:300]
        rec = {"ts": now.isoformat(timespec="seconds"), "time": f"{now:%H:%M}", "value": round(self.value(), 2), "checks": out}
        try:
            with open(self.env.logs_dir / f"checks-{self.day.isoformat()}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("could not write the minute checks")

    def write_last_prices(self):
        """state/last_prices.json: {symbol: {price, ts, prev_close}}, read by the memory brief and tools/should_think.py."""
        path = self.env.state_dir / "last_prices.json"
        data = _load_json(path)
        data = data if isinstance(data, dict) else {}
        for s, px in self.mkt.prices.items():
            ts = self.mkt.price_ts.get(s)
            data[s] = {"price": round(px, 2), "ts": (ts or self.now).isoformat(timespec="seconds"),
                       "prev_close": round(self.mkt.prev_close[s], 2) if self.mkt.prev_close.get(s) else None}
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        except OSError:
            log.exception("could not write last_prices.json")

    def run(self) -> list[dict]:
        self.env.logs_dir.mkdir(parents=True, exist_ok=True)
        self.env.state_dir.mkdir(parents=True, exist_ok=True)
        self.housekeeping()
        for sym, why in self.bad_prices:
            self._noisy_skip("SKIP", sym, why, "Minute check", None)
        self.triggers()
        advice = self.fastlane_advice()
        if self.v2:
            self.v2_exits(advice)
            self.ratchet()
            self.v2_tick()
        else:
            self.exits(advice)
            self.ratchet()
            self.entries()
        self.snapshot()
        return self.log


def run_tick(pf: Portfolio, mkt: Market, env: Env) -> list[dict]:
    return Engine(pf, mkt, env).run()
