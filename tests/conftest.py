"""Test configuration.

config/params.yaml ships with `strategy.mode: v2_trend` (the growth strategy). The older test files exercise the v1 Green
Guard rules (floor, sizing, score entries, month-end), which are intentionally unchanged under `mode: v1_scores`. So the
cached params every test module gets from config.load_params() are switched to v1_scores here, once, before any test
module is imported. v2 tests build their own copy with `v2_params()` (tests/test_trend.py, tests/test_engine_v2.py); a
test that reads the shipped YAML file directly checks that the shipped mode really is v2_trend.
"""
from __future__ import annotations

import copy

from simgold import config

config.load_params()["strategy"]["mode"] = "v1_scores"
# The shipped file has `capital.idle: cash` (owner decision 2026-10-22). The older tests exercise the LIQUIDBEES path (the
# one-line rollback `capital.idle: liquidbees`), so the cached params keep that; cash mode has its own tests (tests/test_idle_cash.py).
config.load_params()["capital"]["idle"] = "liquidbees"


def d5_params() -> dict:
    """A deep copy of the shipped params with the v2 trend core on: the D5 "stay invested" profile (50% gold floor, stop to gold)."""
    p = copy.deepcopy(config.load_params())
    p["strategy"]["mode"] = "v2_trend"
    return p


def v2_params() -> dict:
    """The plain v2 trend core (the numbers of the original PR): D5's floor, stop-to-gold and RSI-free entries switched off, so
    the tests of the trend rules themselves keep their meaning. D5 has its own tests (tests/test_d5.py)."""
    p = d5_params()
    st = p["strategy"]
    st["min_invested"], st["stop_to_gold"], st["entry"]["rsi_timing"] = 0.0, False, True
    return p


def cash_params(base: dict | None = None) -> dict:
    """A deep copy of the params with idle money as plain cash (the shipped setting)."""
    p = copy.deepcopy(base or config.load_params())
    p["capital"]["idle"] = "cash"
    return p
