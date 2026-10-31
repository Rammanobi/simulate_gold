"""Green Guard: the pure risk rules of docs/RULEBOOK.md.

Everything here is a plain function of its inputs (no clock, no files, no network), so each rule is
unit-tested on its own (tests/test_guard.py). simgold/engine.py wires these into the minute tick.

Section numbers in comments refer to the rulebook.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import config, market

INF = float("inf")


def _p(p: dict | None) -> dict:
    return p if p is not None else config.load_params()


def hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


def rupees(x: float) -> str:
    return f"₹{x:,.0f}"


# ---------------------------------------------------------------- 1. floor, tripwire, caution
def floor_for_best_profit(best_profit: float, p: dict | None = None) -> float:
    """Floor implied by the best profit seen so far (before the never-decreases rule)."""
    g = _p(p)["green_guard"]
    if best_profit < g["lock_trigger_profit"]:
        return float(g["initial_floor"])
    return g["locked_floor"] + g["ratchet_share"] * (best_profit - g["ratchet_offset"])


def ratchet(prev_floor: float, prev_best_profit: float, value: float, p: dict | None = None) -> tuple[float, float]:
    """One minute of the floor ratchet. Returns (floor, best_profit). The floor never goes down."""
    pp = _p(p)
    best = max(prev_best_profit, value - pp["capital"]["start"])
    floor = max(prev_floor, floor_for_best_profit(best, pp))
    return floor, best


def is_tripwire(value: float, floor: float, p: dict | None = None) -> bool:
    return value <= floor + _p(p)["green_guard"]["tripwire_buffer"]


def in_caution_zone(value: float, floor: float, p: dict | None = None) -> bool:
    return value <= floor + _p(p)["green_guard"]["caution_buffer"]


# ---------------------------------------------------------------- 2. sizing
def risk_rate(sym: str, gap_mult: float = 1.0, p: dict | None = None) -> float:
    """gap% x multiplier + round-trip cost% (2.1)."""
    i = _p(p)["instruments"][sym]
    return i["gap"] * gap_mult + i["cost"]


def position_risk(sym: str, qty: float, price: float, gap_mult: float = 1.0, p: dict | None = None) -> float:
    """Worst-case cost of a position: qty x price x (gap% x multiplier + cost%). A stop does not lower it."""
    return qty * price * risk_rate(sym, gap_mult, p)


def gap_multiplier(d: date, event_mult: float = 1.0, p: dict | None = None) -> float:
    """Multiplier for the night after trading day d: weekend 1.25, holiday 1.5, times any event multiplier.
    The jobs-report example (1.5 x 1.25 on a Friday) falls out of this product."""
    gm = _p(p)["sizing"]["gap_multiplier"]
    kind = market.overnight_gap_kind(d)
    base = {"weekend": gm["weekend"], "holiday": gm["nse_holiday_global_open"]}.get(kind, 1.0)
    return base * event_mult


def week_factor(td_of_month: int, td_left: int, p: dict | None = None) -> float:
    """0.5 / 0.75 / 1.0 by trading day of the month; 0.0 in the last 3 trading days (no new buys)."""
    s = _p(p)
    if td_left < s["month_end"]["protect_last_trading_days"]:
        return 0.0
    wd = s["sizing"]["week_days"]
    wf = s["sizing"]["week_factor"]
    if td_of_month <= wd["week1"][1]:
        return wf["week1"]
    if td_of_month <= wd["week2"][1]:
        return wf["week2"]
    return wf["rest"]


def max_gross_pct(td_of_month: int, p: dict | None = None) -> float:
    s = _p(p)["sizing"]
    wd = s["week_days"]
    if td_of_month <= wd["week1"][1]:
        return s["max_gross_pct"]["week1"]
    if td_of_month <= wd["week2"][1]:
        return s["max_gross_pct"]["week2"]
    return s["max_gross_pct"]["rest"]


def liquid_carry(liquid: float, d: date, p: dict | None = None) -> float:
    """LIQUIDBEES interest expected from today to month end (calendar days, today included)."""
    c = _p(p)["capital"]
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    days = (nxt - d).days
    return liquid * c["liquid_yield_annual"] * days / 365.0


def green_budget(value: float, liquid: float, floor: float, d: date, p: dict | None = None) -> float:
    """Room such that a worst-case loss can be earned back by LIQUIDBEES carry before month end (2.2).
    Only binding while the floor is still below 1,00,000 (the first month); otherwise unlimited."""
    s = _p(p)
    if floor >= s["green_guard"]["locked_floor"]:
        return INF
    sz = s["sizing"]
    carry = liquid_carry(liquid, d, s)
    return (value + s["capital"]["liquid_carry_haircut"] * carry
            - sz["green_budget_target"] - sz["green_budget_buffer"])


def usable_risk(value: float, floor: float, liquid: float, d: date, p: dict | None = None,
                for_overnight: bool = False) -> float:
    """usable_risk = min(cushion - 100, green_budget) x week_factor (2.2).
    for_overnight=True: in the last 3 days (where week_factor is 0 because buying stops) use 1.0, so that the
    15:20 budget check is not trivially failed; the month-end check is the tighter test there."""
    s = _p(p)
    td, left = market.trading_day_of_month(d), market.trading_days_left_in_month(d)
    wf = week_factor(td, left, s)
    if wf == 0.0 and for_overnight:
        wf = s["sizing"]["week_factor"]["rest"]
    cushion = value - floor
    base = min(cushion - s["sizing"]["reserve"], green_budget(value, liquid, floor, d, s))
    return max(0.0, base) * wf


@dataclass
class Sizing:
    qty: int
    limiter: str          # which rule set the size: risk | instrument cap | gross cap | cash | none
    usable: float = 0.0
    used: float = 0.0


def size_buy(sym: str, fill_price: float, value: float, floor: float, free_cash: float, liquid: float,
             holdings: dict[str, tuple[float, float]], d: date, gap_mult_of, p: dict | None = None) -> Sizing:
    """Largest whole-share buy that passes every sizing rule (2.2 and 2.4).

    holdings: {sym: (qty, price)} of open positions.  gap_mult_of(sym) -> tonight's gap multiplier.
    free_cash: cash + LIQUIDBEES available to spend."""
    s = _p(p)
    td, left = market.trading_day_of_month(d), market.trading_days_left_in_month(d)
    if week_factor(td, left, s) == 0.0:
        return Sizing(0, "last 3 trading days")
    usable = usable_risk(value, floor, liquid, d, s)
    used = sum(position_risk(k, q, px, gap_mult_of(k), s) for k, (q, px) in holdings.items())
    inst = s["instruments"][sym]
    rate = risk_rate(sym, gap_mult_of(sym), s)
    cands = {"risk": math.floor(max(0.0, usable - used) / (fill_price * rate))}
    held_val = sum(q * px for q, px in holdings.values())
    own_val = holdings.get(sym, (0, 0))[0] * fill_price
    cands["instrument cap"] = math.floor(max(0.0, inst["cap_pct"] * value - own_val) / fill_price)
    cands["gross cap"] = math.floor(max(0.0, max_gross_pct(td, s) * value - held_val) / fill_price)
    spendable = free_cash - s["capital"]["min_cash_pct"] * value
    cands["cash"] = math.floor(max(0.0, spendable) / (fill_price * (1 + inst["cost"])))
    limiter = min(cands, key=cands.get)
    return Sizing(max(0, cands[limiter]), limiter, usable, used)


# ---------------------------------------------------------------- 3. score and thresholds
def score_threshold(sym: str, bias: str, guidance_fresh: bool, p: dict | None = None) -> float | None:
    """Buy threshold (3.2). None means no buys. Stale or missing guidance counts as neutral + 0.05."""
    e = _p(p)["entry"]
    b = (bias or "neutral").lower()
    if guidance_fresh and b == "block":
        return None
    if not guidance_fresh:
        return round(e["thresholds"]["neutral"][sym] + e["stale_guidance_penalty"], 4)
    return e["thresholds"]["favour" if b == "favour" else "neutral"][sym]


def normalise_bias(v) -> str:
    s = str(v or "neutral").strip().lower().replace("_", "-")
    if s in ("favour", "favor"):
        return "favour"
    if s in ("block", "block-new-buys", "block-new-buy", "blocked"):
        return "block"
    return "neutral"


BIAS_RANK = {"favour": 0, "neutral": 1, "block": 2}


# ---------------------------------------------------------------- 4. buy windows and limits
def in_buy_window(t: time, p: dict | None = None) -> tuple[bool, str]:
    dl = _p(p)["daily_limits"]
    if t < hhmm(dl["no_buys_before"]):
        return False, f"New buys are not allowed before {dl['no_buys_before']}, the first 15 minutes after the open."
    if t > hhmm(dl["no_buys_after"]):
        return False, f"New buys are not allowed after {dl['no_buys_after']}, too close to the close."
    return True, ""


def fresh_counters(day: date) -> dict:
    return {"date": day.isoformat(), "buys_today": 0, "buys_sym_today": {}, "losses_today": 0}


def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def check_buy_limits(sym: str, now: datetime, counters: dict, cooldowns: dict, buys_week: dict,
                     p: dict | None = None) -> tuple[str, str] | None:
    """Daily/weekly counts, spacing, re-buy and two-loss rules (section 4). Returns (code, plain reason) or None."""
    dl = _p(p)["daily_limits"]
    today = now.date().isoformat()
    if counters.get("losses_today", 0) >= dl["max_losing_trades_then_stop"]:
        return "limit", (f"Two trades lost money today, so no new buys are allowed for the rest of the day.")
    if counters.get("buys_today", 0) >= dl["max_buys_per_day"]:
        return "limit", f"The limit of {dl['max_buys_per_day']} new buys per day has been used."
    if counters.get("buys_sym_today", {}).get(sym, 0) >= dl["max_buys_per_instrument_day"]:
        return "limit", f"{sym} has already been bought once today, which is the daily limit per instrument."
    if buys_week.get(sym, 0) >= dl["max_buys_per_instrument_week"]:
        return "limit", f"{sym} has already been bought {dl['max_buys_per_instrument_week']} times this week, which is the weekly limit."
    last = cooldowns.get("last_buy_ts")
    if last:
        gap = (now - datetime.fromisoformat(last)).total_seconds() / 60
        if gap < dl["min_minutes_between_buys"]:
            return "limit", (f"The last buy was {gap:.0f} minutes ago and at least "
                             f"{dl['min_minutes_between_buys']} minutes must pass between buys.")
    if dl["no_rebuy_after_stop_same_day"] and cooldowns.get("stopped_out", {}).get(sym) == today:
        return "limit", f"{sym} was stopped out today, so it cannot be bought again until tomorrow."
    return None


# ---------------------------------------------------------------- 3.3 exits
@dataclass
class ExitAction:
    trigger: str
    qty: int
    reason: str
    stop_out: bool = False       # counts as a stop-out for the same-day re-buy ban


def initial_stop_price(sym: str, entry: float, p: dict | None = None) -> float:
    return entry * (1 - _p(p)["instruments"][sym]["stop"])


def breakeven_stop_price(sym: str, entry: float, p: dict | None = None) -> float:
    """Entry plus costs: the stop on the remaining half after the target is hit."""
    return entry * (1 + _p(p)["instruments"][sym]["cost"])


def target_progress(sym: str, entry: float, high: float, p: dict | None = None) -> float:
    """Fraction of the take-half target already reached by the highest price since entry."""
    t = _p(p)["instruments"][sym]["target_half"]
    return (high / entry - 1) / t if entry > 0 else 0.0


def next_exit(sym: str, pos, price: float, now: datetime, score: float | None, days_held: int,
              p: dict | None = None) -> ExitAction | None:
    """The first exit rule (in rulebook order, 3.3 items 2 to 6) that fires for this position, or None.
    pos needs: qty, avg_cost (entry), high_water, stop, half_sold. The caller applies the result and
    calls again, so e.g. 'sell half' is followed by the trailing check on the rest."""
    s = _p(p)
    i = s["instruments"][sym]
    ps = s["positions"]
    entry, qty = pos.avg_cost, pos.qty
    if qty <= 0:
        return None
    chg = price / entry - 1
    stop = pos.stop or initial_stop_price(sym, entry, s)
    # 2. stop (initial, or entry + costs after the half is sold, or a tightened stop)
    if price <= stop:
        what = "the break-even stop" if pos.half_sold else "the stop-loss"
        if abs(stop - initial_stop_price(sym, entry, s)) < 1e-9:
            why = (f"Price fell to ₹{price:.2f}, {abs(chg) * 100:.1f}% below the entry price of ₹{entry:.2f} "
                   f"and through the stop-loss at ₹{stop:.2f}, so the whole position was sold.")
        else:
            why = (f"Price fell to ₹{price:.2f}, through {what} that had been moved up to ₹{stop:.2f} "
                   f"to protect the gain, so the rest of the position was sold.")
        return ExitAction("Stop-loss", qty, why, stop_out=True)
    # 3. take half profit
    if not pos.half_sold and price >= entry * (1 + i["target_half"]):
        sell = qty // 2 if qty >= 2 else qty
        why = (f"Price rose {chg * 100:.1f}% above the entry price of ₹{entry:.2f}, reaching the "
               f"{i['target_half'] * 100:.1f}% profit target, so "
               + ("half the position was sold and the stop on the rest moves to break-even."
                  if sell < qty else "the single share held was sold."))
        return ExitAction("Target rule", sell, why)
    # 4. trailing stop (starts after the half has been sold)
    if pos.half_sold:
        trail = pos.high_water * (1 - i["trail"])
        if price <= trail:
            why = (f"After the profit target, price fell {i['trail'] * 100:.1f}% from its high of "
                   f"₹{pos.high_water:.2f} to ₹{price:.2f}, so the trailing stop sold the rest.")
            return ExitAction("Trailing stop", qty, why)
    # 5./6. 15:15 exits
    if now.time() >= hhmm(ps["time_stop_exit_time"]):
        if (not pos.half_sold and days_held >= i["time_stop_days"]
                and target_progress(sym, entry, pos.high_water, s) < 0.5):
            why = (f"The position is {days_held} trading days old and never got half way to its profit target "
                   f"(limit {i['time_stop_days']} days), so it was sold to free the money.")
            return ExitAction("Time stop", qty, why)
        if score is not None and score < ps["score_exit_below"] and now.time() >= hhmm(ps["score_exit_time"]):
            why = (f"The buy score dropped to {score:.2f}, below the {ps['score_exit_below']:.2f} "
                   f"exit level, so the position was sold before the close.")
            return ExitAction("Score exit", qty, why)
    return None


# ---------------------------------------------------------------- 3.4 / 7: overnight and month-end checks
def _drop_until_ok(items: list[tuple[str, float]], excess_fn) -> list[str]:
    """items: (sym, weight). Sell highest weight first until excess_fn(remaining_items) <= 0."""
    left = sorted(items, key=lambda kv: kv[1], reverse=True)
    sold: list[str] = []
    while left and excess_fn(left) > 1e-9:
        sold.append(left.pop(0)[0])
    return sold


def overnight_sells(positions: dict[str, tuple[float, float]], value: float, floor: float, liquid: float,
                    d: date, gap_mult_of, p: dict | None = None) -> list[str]:
    """3.4: with tonight's gap multiplier the budget rule must still pass; otherwise sell the highest risk first.
    positions: {sym: (qty, price)}."""
    s = _p(p)
    usable = usable_risk(value, floor, liquid, d, s, for_overnight=True)
    items = [(k, position_risk(k, q, px, gap_mult_of(k), s)) for k, (q, px) in positions.items()]
    return _drop_until_ok(items, lambda left: sum(w for _, w in left) - usable)


def month_end_required(floor: float, p: dict | None = None) -> float:
    s = _p(p)
    return max(floor, s["engine"]["month_end_floor_base"]) + s["engine"]["required_min_buffer"]


def in_month_end_window(d: date, p: dict | None = None) -> bool:
    return market.is_trading_day(d) and market.trading_days_left_in_month(d) < _p(p)["month_end"]["protect_last_trading_days"]


def month_end_sells(positions: dict[str, tuple[float, float]], value: float, floor: float,
                    gap_mult_of, p: dict | None = None) -> list[str]:
    """7.1.2: value - sum(qty x price x gap% x 1.5 x event/weekend multiplier) >= max(floor, 1,00,000) + 200."""
    s = _p(p)
    req = month_end_required(floor, s)
    items = [(k, q * px * s["instruments"][k]["gap"] * s["month_end"]["gap_mult"] * gap_mult_of(k))
             for k, (q, px) in positions.items()]
    return _drop_until_ok(items, lambda left: req - (value - sum(w for _, w in left)))


def month_end_flatten(value: float, p: dict | None = None) -> bool:
    """7.1.3: value under 1,00,300 in the last 3 days means sell everything."""
    return value < _p(p)["month_end"]["flatten_if_value_below"]


def last_day_stop_sells(positions: dict[str, tuple[float, float, float]], value: float,
                        p: dict | None = None) -> list[str]:
    """7.1.4: on the last trading day, sell positions whose stop-out could pull value under 1,00,200.
    positions: {sym: (qty, price, stop_price)}.  Greedy by largest stop loss, cumulative (safer than one by one)."""
    floor_val = _p(p)["month_end"]["last_day_min_after_stop"]
    items = [(k, max(0.0, q * (px - st))) for k, (q, px, st) in positions.items()]
    return _drop_until_ok(items, lambda left: floor_val - (value - sum(w for _, w in left)))


# ---------------------------------------------------------------- 6. event days
_EVENT_ALIASES = {
    "us_cpi": "us_cpi", "cpi": "us_cpi", "us cpi": "us_cpi",
    "us_jobs": "us_jobs", "jobs": "us_jobs", "nfp": "us_jobs", "us jobs": "us_jobs", "us_nfp": "us_jobs",
    "fomc": "fomc", "fed": "fomc",
    "rbi": "rbi", "rbi_policy": "rbi",
    "union_budget": "union_budget", "budget": "union_budget",
    "hindcopper_results": "hindcopper_results", "hindustan_copper": "hindcopper_results",
    "hindcopper": "hindcopper_results", "hindcopper_board": "hindcopper_results",
}


def _clock_or_none(v) -> str | None:
    """'18:00' (or '18:00:00') -> '18:00'; anything else (bad text, numbers, '25:99') -> None."""
    try:
        h, m = str(v).strip().split(":")[:2]
        return time(int(h), int(m)).strftime("%H:%M")
    except (TypeError, ValueError):
        return None


def normalise_events(raw) -> list[dict]:
    """events.json is a list of {date, type}; tolerate {"events": [...]}; drop malformed rows.
    A malformed time is dropped (not the row): for Hindustan Copper results that means the safer 'sell the day before'."""
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    out = []
    for e in raw if isinstance(raw, list) else []:
        try:
            typ = _EVENT_ALIASES[str(e["type"]).strip().lower().replace("-", "_")]
            d = date.fromisoformat(str(e["date"])[:10])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"date": d, "type": typ, "time": _clock_or_none(e.get("time")) if e.get("time") else None})
    return out


@dataclass
class EventRules:
    """What today's events mean for buying and for tonight's gap multiplier."""
    today: date
    rules: list[dict] = field(default_factory=list)   # {sym, kind, ...} flattened
    overnight: dict[str, tuple[float, str]] = field(default_factory=dict)   # sym -> (mult, why)
    forced_exits: dict[str, tuple[time, str]] = field(default_factory=dict)  # sym -> (clock time, why)

    def buy_block(self, sym: str, t: time) -> str | None:
        for r in self.rules:
            if r["sym"] not in (sym, "*"):
                continue
            k = r["kind"]
            if k == "after" and t > r["t"]:
                return r["why"]
            if k == "before" and t < r["t"]:
                return r["why"]
            if k == "all_day":
                return r["why"]
        return None

    def night_mult(self, sym: str) -> float:
        return self.overnight.get(sym, (1.0, ""))[0]


def event_rules(day: date, events: list[dict], p: dict | None = None) -> EventRules:
    s = _p(p)
    ev = s["events"]
    er = EventRules(day)
    metals = ev["us_cpi"]["applies"]
    for e in events:
        d, typ = e["date"], e["type"]
        label = {"us_cpi": "US CPI inflation data", "us_jobs": "the US jobs report", "fomc": "the US Fed (FOMC) decision",
                 "rbi": "the RBI policy decision", "union_budget": "the Union Budget",
                 "hindcopper_results": "Hindustan Copper results"}[typ]
        if typ in ("us_cpi", "us_jobs", "fomc", "union_budget"):
            rule = ev["fomc" if typ in ("fomc", "union_budget") else typ]
            if d == day:
                for m in metals:
                    if rule.get("no_metal_buys_all_day"):
                        er.rules.append({"sym": m, "kind": "all_day",
                                         "why": f"{label.capitalize()} is due tonight, so gold and silver are not bought today."})
                    else:
                        t = hhmm(rule["no_metal_buys_after"])
                        er.rules.append({"sym": m, "kind": "after", "t": t,
                                         "why": f"{label.capitalize()} is due tonight, so gold and silver are not bought after {t:%H:%M}."})
                    cur = er.overnight.get(m, (1.0, ""))
                    if rule["overnight_gap_mult"] > cur[0]:
                        er.overnight[m] = (rule["overnight_gap_mult"], label)
            if market.next_trading_day(d) == day:
                t = hhmm(rule["next_day_no_buys_before"])
                for m in metals:
                    er.rules.append({"sym": m, "kind": "before", "t": t,
                                     "why": f"The morning after {label}, gold and silver are not bought before {t:%H:%M}."})
        elif typ == "rbi" and d == day:
            a, b = ev["rbi"]["no_buys_window"]
            er.rules.append({"sym": "*", "kind": "before", "t": hhmm(b),
                             "why": f"The RBI policy decision is due, so nothing is bought before {b} while the market settles."})
        elif typ == "hindcopper_results":
            sym = "HINDCOPPER"
            b2 = market.add_trading_days(d, -ev["hindcopper_results"]["no_buys_from_days_before"])
            r2 = market.add_trading_days(d, 2)
            if b2 <= day < r2:
                er.rules.append({"sym": sym, "kind": "all_day",
                                 "why": f"Hindustan Copper results are due on {d:%d %b}, so HINDCOPPER is not bought around them."})
            elif day == r2:
                t = hhmm("10:15")
                er.rules.append({"sym": sym, "kind": "before", "t": t,
                                 "why": "Hindustan Copper results came out two days ago, so HINDCOPPER stays blocked until 10:15."})
            during = False
            if e.get("time"):
                tt = hhmm(e["time"])
                during = hhmm(s["engine"]["market_open"]) <= tt < hhmm(s["engine"]["market_close"])
            exit_day = d if during else market.prev_trading_day(d)
            if day == exit_day:
                lead = s["engine"]["hindcopper_intraday_exit_lead_min"] if during else s["engine"]["hindcopper_exit_lead_min"]
                base = (datetime.combine(day, hhmm(e["time"])) if during
                        else datetime.combine(day, hhmm(ev["hindcopper_results"]["exit_by"])))
                er.forced_exits[sym] = ((base - timedelta(minutes=lead)).time(),
                                        "Hindustan Copper results are due " + ("today during market hours" if during else "tomorrow")
                                        + ", so the position is sold before the news.")
    return er


# ---------------------------------------------------------------- 5.3 fast lane: defence only
FORBIDDEN_KEYS = {"buy", "buys", "favour", "favor", "set_bias_favour", "loosen_stop", "raise_cap", "cap", "size", "qty",
                  "change_floor", "floor", "change_gap", "gap", "skip_limits", "skip_cooldown", "tripwire", "target"}
_SYM_KEYS_OK = {"block_until", "sell_fraction", "tighten_stop", "bias", "note", "reason"}


def _parse_dt(v, tz) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=tz)


def sanitize_fastlane(raw: dict, now: datetime, positions: dict[str, dict], p: dict | None = None) -> tuple[list[dict], list[str]]:
    """Keep only the defence-only parts of an AI fast-lane reply (5.3).

    positions: {sym: {"qty", "price", "stop"}}.
    Returns (accepted, rejected). accepted items:
      {"type": "block_until", "sym", "until": datetime}      (clamped to the end of today)
      {"type": "sell_fraction", "sym", "fraction": float}
      {"type": "tighten_stop", "sym", "stop": float}         (strictly above the current stop and below price)
      {"type": "set_bias", "sym", "bias": "neutral"|"block"}   (never favour)
      {"type": "halt_buys_today"}
    rejected: plain-English strings (they are logged and ignored)."""
    acc: list[dict] = []
    rej: list[str] = []
    if not isinstance(raw, dict):
        return acc, ["fast-lane file is not a JSON object"]
    end_of_day = datetime.combine(now.date(), time(23, 59, 59), tzinfo=now.tzinfo)

    def handle_sym(sym: str, spec: dict):
        if not isinstance(spec, dict):
            rej.append(f"{sym}: expected an object")
            return
        pos = positions.get(sym)
        for k, v in spec.items():
            kl = k.lower()
            if kl in FORBIDDEN_KEYS:
                rej.append(f"{sym}.{k}: not allowed, the fast lane is defence only")
            elif kl not in _SYM_KEYS_OK:
                rej.append(f"{sym}.{k}: unknown field ignored")
            elif kl == "block_until":
                t = _parse_dt(v, now.tzinfo)
                if t is None:
                    rej.append(f"{sym}.block_until: not a valid time")
                elif t <= now:
                    rej.append(f"{sym}.block_until: already in the past")
                else:
                    acc.append({"type": "block_until", "sym": sym, "until": min(t, end_of_day)})
            elif kl == "sell_fraction":
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    f = -1
                if not math.isfinite(f):
                    f = -1
                if not (0 < f <= 1):
                    rej.append(f"{sym}.sell_fraction: must be between 0 and 1")
                elif not pos or pos["qty"] <= 0:
                    rej.append(f"{sym}.sell_fraction: no open position to sell")
                else:
                    acc.append({"type": "sell_fraction", "sym": sym, "fraction": f})
            elif kl == "tighten_stop":
                try:
                    x = float(v)
                except (TypeError, ValueError):
                    x = -1
                if not math.isfinite(x):          # NaN compares False with everything, so it would pass every check below
                    x = -1
                if not pos or pos["qty"] <= 0:
                    rej.append(f"{sym}.tighten_stop: no open position")
                elif x <= 0:
                    rej.append(f"{sym}.tighten_stop: not a valid number")
                else:
                    new = pos["price"] * (1 - x) if x < 0.5 else x   # <0.5: distance below price; else a price
                    if new <= pos["stop"]:
                        rej.append(f"{sym}.tighten_stop: {new:.2f} would loosen the stop (now {pos['stop']:.2f}); ignored")
                    elif new >= pos["price"]:
                        rej.append(f"{sym}.tighten_stop: {new:.2f} is not below the price {pos['price']:.2f}; use sell_fraction instead")
                    else:
                        acc.append({"type": "tighten_stop", "sym": sym, "stop": new})
            elif kl == "bias":
                b = normalise_bias(v)
                if str(v).strip().lower() in ("favour", "favor") or b == "favour":
                    rej.append(f"{sym}.bias: 'favour' is not allowed in the fast lane")
                else:
                    acc.append({"type": "set_bias", "sym": sym, "bias": b})

    for k, v in raw.items():
        kl = str(k).lower()
        if kl in ("updated", "note", "reason", "request_id", "trigger"):
            continue
        if kl in FORBIDDEN_KEYS:
            rej.append(f"{k}: not allowed, the fast lane is defence only")
        elif kl == "halt_buys_today":
            if v:
                acc.append({"type": "halt_buys_today"})
        elif kl == "bias" and isinstance(v, dict):
            for ks, bv in v.items():
                handle_sym(config.symbol_of(ks), {"bias": bv})
        elif kl in ("symbols", "instruments") and isinstance(v, dict):
            for ks, spec in v.items():
                handle_sym(config.symbol_of(ks), spec)
        elif config.symbol_of(k) in _p(p)["instruments"]:
            handle_sym(config.symbol_of(k), v)
        else:
            rej.append(f"{k}: unknown field ignored")
    return acc, rej
