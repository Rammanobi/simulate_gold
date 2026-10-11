"""NSE market-hours calendar (IST)."""
from datetime import datetime, time, date
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 15), time(15, 30)

# Best-effort NSE trading holidays for 2026 — verify against the official NSE
# circular each year. The live loop also treats a stale price feed as "closed",
# so a wrong entry here only costs a few idle minutes.
NSE_HOLIDAYS = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
    date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
}


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
