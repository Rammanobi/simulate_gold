"""NSE market-hours calendar (IST)."""
from datetime import datetime, time, date
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 15), time(15, 30)

# Best-effort NSE trading holidays for 2026 (weekday closures only) — verify against the
# official NSE circular each year. The live loop also treats a stale price feed as
# "closed", so a wrong entry here only costs a few idle minutes.
# TODO(owner): re-check every date below against the NSE 2026 holiday circular.
# Notes on the entries the owner asked about:
#   * Diwali Laxmi Pujan 2026 falls on Sunday 2026-11-08, so it is NOT a weekday session
#     and needs no entry (the special Muhurat evening session is not modelled).
#   * Diwali Balipratipada 2026-11-10, Guru Nanak Jayanti 2026-11-24 and Christmas
#     2026-12-25 are all weekdays and are listed.
#   * 2026-08-15 (Independence Day) is a Saturday, so no entry is needed.
#   * Id-ul-Fitr (about 2026-03-21) is a Saturday, so no entry is needed.
NSE_HOLIDAYS = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
    date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
}

# Weekday NSE holidays on which the big world markets are ALSO shut, so the 1.5x
# "holiday while world markets stay open" gap multiplier does not apply.
# TODO(owner): confirm (Good Friday 2026-04-03 and Christmas 2026-12-25 are US/EU holidays).
GLOBAL_CLOSED_HOLIDAYS = {date(2026, 4, 3), date(2026, 12, 25)}


def holidays_known(year: int) -> bool:
    """True if NSE_HOLIDAYS has been filled in for this year (the healthcheck warns from 1 Dec when next year is missing)."""
    return any(d.year == year for d in NSE_HOLIDAYS)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def is_market_open(ts: datetime | None = None) -> bool:
    ts = (ts or now_ist()).astimezone(IST)
    return is_trading_day(ts.date()) and OPEN <= ts.time() <= CLOSE


def minutes_to_close(ts: datetime | None = None) -> float:
    ts = (ts or now_ist()).astimezone(IST)
    close_dt = ts.replace(hour=CLOSE.hour, minute=CLOSE.minute, second=0, microsecond=0)
    return (close_dt - ts).total_seconds() / 60


# ---- trading-day calendar helpers (used by the Green Guard rules) -------------------------
from datetime import timedelta


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def add_trading_days(d: date, n: int) -> date:
    step = next_trading_day if n >= 0 else prev_trading_day
    for _ in range(abs(n)):
        d = step(d)
    return d


def trading_days_in_month(year: int, month: int) -> list[date]:
    d, out = date(year, month, 1), []
    while d.month == month:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def trading_day_of_month(d: date) -> int:
    """1-based index of d among the trading days of its month (works for non-trading d too: counts those up to d)."""
    return sum(1 for x in trading_days_in_month(d.year, d.month) if x <= d)


def trading_days_left_in_month(d: date) -> int:
    """Trading days remaining in d's month AFTER d. 0 on the last trading day."""
    return sum(1 for x in trading_days_in_month(d.year, d.month) if x > d)


def is_last_trading_day_of_month(d: date) -> bool:
    return is_trading_day(d) and trading_days_left_in_month(d) == 0


def trading_days_between(a: date, b: date) -> int:
    """Number of trading days from a to b, both inclusive (0 if b < a)."""
    n, d = 0, a
    while d <= b:
        n += is_trading_day(d)
        d += timedelta(days=1)
    return n


def overnight_gap_kind(d: date) -> str | None:
    """What kind of closure follows the night after trading day d: 'holiday', 'weekend' or None."""
    nxt = d + timedelta(days=1)
    if is_trading_day(nxt):
        return None
    skipped, x = [], nxt
    while not is_trading_day(x):
        skipped.append(x)
        x += timedelta(days=1)
    if any(s.weekday() < 5 and s in NSE_HOLIDAYS and s not in GLOBAL_CLOSED_HOLIDAYS for s in skipped):
        return "holiday"
    return "weekend"
