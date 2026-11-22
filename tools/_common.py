"""Shared bits for the tools/ scripts: put the repo root on sys.path and parse an optional --now / --root."""
from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simgold.market import IST, now_ist  # noqa: E402


def parse_now(s: str | None) -> datetime:
    """--now takes an ISO timestamp (testing); default is the real IST time."""
    if not s:
        return now_ist()
    d = datetime.fromisoformat(s)
    return (d if d.tzinfo else d.replace(tzinfo=IST)).astimezone(IST)


def utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def iso_day(s) -> date:
    """A date written exactly YYYY-MM-DD (ASCII digits). date.fromisoformat on Python 3.11 also takes 20261118 and 2026-W47-3, which
    a routine must not be able to type by accident. Raises ValueError with a plain reason."""
    if not isinstance(s, str) or not _ISO_DAY.fullmatch(s):
        raise ValueError(f"{str(s)[:20]!r} is not a date written YYYY-MM-DD")
    return date.fromisoformat(s)
