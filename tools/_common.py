"""Shared bits for the tools/ scripts: put the repo root on sys.path and parse an optional --now / --root."""
from __future__ import annotations

import sys
from datetime import datetime
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
