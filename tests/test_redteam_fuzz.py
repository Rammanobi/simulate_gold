"""Money-invariant fuzz (tests/redteam_fuzz.py) at CI size, plus mutation checks that the oracle is not vacuous.

Scale it up with SIMGOLD_FUZZ_PATHS / SIMGOLD_FUZZ_DAYS (docs/REDTEAM.md used 10 x 500 = 5,000 trading days)."""
from __future__ import annotations

import logging
import os
from dataclasses import replace

import pytest

import redteam_fuzz as F
from simgold import guard

PATHS = int(os.environ.get("SIMGOLD_FUZZ_PATHS", "2"))
DAYS = int(os.environ.get("SIMGOLD_FUZZ_DAYS", "45"))


@pytest.fixture(autouse=True)
def quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


def test_money_invariants_hold_over_random_days():
    st = F.simulate(PATHS, DAYS, seed=7)
    assert st.days == PATHS * DAYS
    assert st.buys > 10 and st.sells > 10                     # the run really traded
    assert st.months >= PATHS                                 # and crossed month ends


def test_oracle_catches_an_oversized_buy(monkeypatch):
    real = guard.size_buy

    def greedy(*a, **k):
        z = real(*a, **k)
        return replace(z, qty=z.qty * 3 + 50) if z.qty > 0 else z
    monkeypatch.setattr(guard, "size_buy", greedy)
    with pytest.raises(F.InvariantError, match="cap|budget|LIQUIDBEES"):
        F.simulate(1, 60, seed=7)


def test_oracle_catches_a_floor_that_goes_down(monkeypatch):
    real = guard.ratchet
    calls = {"n": 0}

    def leaky(prev_floor, best, value, p=None):
        calls["n"] += 1
        f, b = real(prev_floor, best, value, p)
        return (f - 1.0 if calls["n"] == 500 else f), b
    monkeypatch.setattr(guard, "ratchet", leaky)
    with pytest.raises(F.InvariantError, match="floor decreased"):
        F.simulate(1, 10, seed=7)


def test_oracle_catches_a_buy_in_the_last_three_days(monkeypatch):
    monkeypatch.setattr(guard, "week_factor", lambda td, left, p=None: 0.5)
    with pytest.raises(F.InvariantError):
        F.simulate(1, 60, seed=7)
