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


def v2_params() -> dict:
    """A deep copy of the params with the v2 trend core switched on (the shipped numbers)."""
    p = copy.deepcopy(config.load_params())
    p["strategy"]["mode"] = "v2_trend"
    return p
