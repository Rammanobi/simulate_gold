"""simgold/regime.py: trend label, hysteresis, short-term state and the alignment table (no network)."""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memfix import series  # noqa: E402

from simgold import regime  # noqa: E402

UP, FLAT, DOWN = "UP", "FLAT", "DOWN"
STM = regime.STM_STATES
RANK = regime.BIAS_RANK


def test_uptrend_is_up_downtrend_is_down_noise_is_flat():
    assert regime.ltm_regime(series("up"))["label"] == UP
    assert regime.ltm_regime(series("down"))["label"] == DOWN
    assert regime.ltm_regime(series("flat"))["label"] == FLAT


def test_label_changes_only_after_three_confirming_sessions():
    raw = [FLAT] * 5 + [UP] * 2 + [FLAT] * 2 + [UP] * 4
    labels, since, pend, days = regime.apply_hysteresis(raw, FLAT)
    assert labels[:5] == [FLAT] * 5
    assert labels[5:7] == [FLAT, FLAT]            # two UP sessions are not enough
    assert labels[-4:] == [FLAT, FLAT, UP, UP]    # the last run flips on its 3rd consecutive UP
    assert since == len(raw) - 2


def test_label_flips_on_exactly_the_third_raw_session():
    for new in (UP, DOWN):
        raw = [FLAT] * 3 + [new] * 6
        labels, since, _, _ = regime.apply_hysteresis(raw, FLAT)
        first = labels.index(new)
        assert first == 3 + 2 and since == first
        assert raw[first - 2:first + 1] == [new] * 3


def test_ltm_label_on_a_real_series_obeys_hysteresis_everywhere():
    c = series("up", n=500)
    c.iloc[300:] = c.iloc[300:] * 0.55          # a regime break
    raw = regime.raw_label_series(c).tolist()
    labels, *_ = regime.apply_hysteresis(raw, None)
    for i in range(1, len(labels)):
        if labels[i] != labels[i - 1]:
            assert raw[i] == raw[i - 1] == raw[i - 2] == labels[i]
    assert regime.ltm_regime(c)["label"] == labels[-1]


def test_prev_state_continues_instead_of_replaying():
    c = series("up", n=400)
    full = regime.ltm_regime(c)
    part = regime.ltm_regime(c.iloc[:-5])
    cont = regime.ltm_regime(c, prev_state=part)
    assert cont["label"] == full["label"] == UP and cont["asof"] == full["asof"]
    again = regime.ltm_regime(c, prev_state=full)         # nothing new: unchanged
    assert again["label"] == full["label"] and again["since"] == full["since"]


def test_short_history_is_flat_and_stale():
    r = regime.ltm_regime(series("up", n=15))
    assert r["label"] == FLAT and r["stale"]
    r = regime.ltm_regime(series("up", n=80))
    assert r["label"] == FLAT and r["stale"] and r["votes"] == r["tests"] == 1
    r = regime.ltm_regime(pd.Series([], dtype=float))
    assert r["label"] == FLAT and r["stale"] and r["asof"] is None
    r = regime.ltm_regime(pd.Series([float("nan")] * 50))
    assert r["label"] == FLAT and r["stale"]


def test_vol_regime_and_returns():
    c = series("up", n=400)
    r = regime.ltm_regime(c)
    assert r["vol_regime"] in ("LOW", "NORMAL", "HIGH") and r["r21"] is not None and r["r252"] is not None
    wild = c.copy()
    rng = np.random.default_rng(3)
    wild.iloc[-15:] = wild.iloc[-16] * np.exp(np.cumsum(rng.normal(0, 0.05, 15)))
    assert regime.ltm_regime(wild)["vol_regime"] == "HIGH"


def test_driver_scores_weighted_sign():
    idx = pd.bdate_range(end="2026-10-15", periods=100)
    closes = pd.DataFrame({"GC=F": np.linspace(100, 120, 100), "DX-Y.NYB": np.linspace(100, 90, 100)}, index=idx)
    out = regime.driver_scores(closes, {"GC=F": 1.0, "DX-Y.NYB": -0.4, "MISSING": 1.0})
    assert out["driver_score"] == 1.0 and set(out["drivers"]) == {"GC=F", "DX-Y.NYB"}
    assert regime.driver_scores(None, {"GC=F": 1})["driver_score"] is None


# ------------------------------------------------------------------ short-term state
def _flat(n=40):
    return series("flat", n=n).values


def test_stm_shock_down_then_decays_after_one_more_session():
    base = _flat()
    shock = np.append(base, base[-1] * 0.93)
    assert regime.stm_state(shock)["state"] == "SHOCK_DOWN"
    assert regime.stm_state(np.append(shock, shock[-1] * 1.001))["state"] == "SHOCK_DOWN"   # shock day + 1
    assert regime.stm_state(np.append(shock, [shock[-1] * 1.001, shock[-1] * 1.002]))["state"] != "SHOCK_DOWN"


def test_stm_shock_up_gap_and_live_price():
    base = _flat()
    assert regime.stm_state(np.append(base, base[-1] * 1.07))["state"] == "SHOCK_UP"
    assert regime.stm_state(base, live_px=base[-1] * 0.93)["state"] == "SHOCK_DOWN"      # live price counts as today
    sd = regime.stm_state(base)["sigma_d"]
    assert regime.stm_state(base, open_gap=-2 * sd)["state"] == "SHOCK_DOWN"
    assert regime.stm_state(base, open_gap=2 * sd)["state"] == "SHOCK_UP"


def test_stm_up_down_flat():
    n = 40
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 0.002, n)
    up = 100 * np.exp(np.cumsum(noise))
    up[-6:] = up[-7] * np.cumprod(np.full(6, 1.006))
    assert regime.stm_state(up)["state"] == "UP"
    dn = 100 * np.exp(np.cumsum(noise))
    dn[-6:] = dn[-7] * np.cumprod(np.full(6, 0.994))
    assert regime.stm_state(dn)["state"] == "DOWN"
    assert regime.stm_state(_flat(5))["state"] == "FLAT" and "reason" in regime.stm_state(_flat(5))
    assert regime.stm_state([])["state"] == "FLAT"


# ------------------------------------------------------------------ alignment table
EXPECT = {
    (UP, "UP"): ("ALIGNED_UP", 1.0, "favour", "favour"),
    (UP, FLAT): ("TREND_PAUSE", 1.0, "favour", "neutral"),
    (UP, DOWN): ("PULLBACK", 1.0, "favour", "neutral"),
    (UP, "SHOCK_DOWN"): ("SHOCK_IN_UPTREND", 0.75, "neutral", "block"),
    (UP, "SHOCK_UP"): ("EXTENDED", 1.0, "neutral", "neutral"),
    (FLAT, "UP"): ("EMERGING", 0.5, "neutral", "neutral"),
    (FLAT, FLAT): ("RANGE", 0.5, "neutral", "neutral"),
    (FLAT, DOWN): ("WEAKENING", 0.25, "neutral", "neutral"),
    (FLAT, "SHOCK_DOWN"): ("BREAKDOWN_RISK", 0.25, "block", "block"),
    (FLAT, "SHOCK_UP"): ("SPIKE", 0.5, "neutral", "neutral"),
    (DOWN, "UP"): ("COUNTER_TREND", 0.0, "neutral", "neutral"),
    (DOWN, FLAT): ("DOWNTREND", 0.0, "neutral", "neutral"),
    (DOWN, DOWN): ("DEFENSIVE", 0.0, "block", "block"),
    (DOWN, "SHOCK_DOWN"): ("DEFENSIVE_SHOCK", 0.0, "block", "block"),
    (DOWN, "SHOCK_UP"): ("SHORT_SQUEEZE", 0.0, "neutral", "neutral"),
}


@pytest.mark.parametrize("cell", sorted(EXPECT))
def test_every_table_cell(cell):
    state, core, ceiling, default = EXPECT[cell]
    a = regime.align(*cell, metal="SILVER")
    assert (a["state"], a["core_mult"], a["ceiling"], a["default"]) == (state, core, ceiling, default)
    assert a["metal"] == "SILVER" and a["ltm"] == cell[0] and a["stm"] == cell[1]
    assert RANK[a["default"]] <= RANK[a["ceiling"]]


def test_table_has_exactly_the_15_cells():
    assert len(regime.ALIGN_TABLE) == 15 and set(regime.ALIGN_TABLE) == set(EXPECT)


OVERRIDES = [{"stale": True}, {"event": "neutral"}, {"event": "block"}, {"thesis_cautious": True},
             {"month_end": True}, {"cushion_low": True}, {"vol_high": True}]


def test_overrides_only_lower_ceiling_default_and_core_for_every_combination():
    n = 0
    for ltm, stm in itertools.product((UP, FLAT, DOWN), STM):
        base = regime.align(ltm, stm)
        for k in range(len(OVERRIDES) + 1):
            for combo in itertools.combinations(OVERRIDES, k):
                ov = {}
                for c in combo:
                    ov.update(c)
                a = regime.align(ltm, stm, ov)
                assert RANK[a["ceiling"]] <= RANK[base["ceiling"]]
                assert RANK[a["default"]] <= RANK[a["ceiling"]]
                assert a["core_mult"] <= base["core_mult"] + 1e-9
                n += 1
    assert n > 1000


def test_stale_ltm_is_treated_as_flat_and_unknowns_are_cautious():
    a = regime.align(UP, "UP", {"stale": True})
    assert a["state"] == "EMERGING" and a["ceiling"] == "neutral"
    u = regime.align(None, None)
    assert u["state"] == "RANGE" and u["ltm"] == "UNKNOWN" and u["ceiling"] == "neutral" and u["core_mult"] == 0.5
    assert regime.align("???", "SIDEWAYS")["state"] == "RANGE"


def test_override_effects():
    assert regime.align(UP, "UP", {"event": "block"})["ceiling"] == "block"
    assert regime.align(UP, "UP", {"event": "neutral"})["ceiling"] == "neutral"
    assert regime.align(UP, "UP", {"month_end": True})["ceiling"] == "neutral"
    c = regime.align(UP, "UP", {"cushion_low": True})
    assert c["ceiling"] == "neutral" and c["core_mult"] == 0.5
    t = regime.align(UP, "UP", {"thesis_cautious": True})
    assert t["ceiling"] == "neutral" and t["core_mult"] == 0.5
    assert regime.align(UP, "UP", {"vol_high": True})["core_mult"] == 0.75
    both = regime.align(UP, "UP", {"cushion_low": True, "vol_high": True})
    assert both["core_mult"] == 0.375


def test_ceiling_only_lowers():
    assert regime.apply_ceiling("favour", "neutral") == ("neutral", True)
    assert regime.apply_ceiling("favour", "block") == ("block", True)
    assert regime.apply_ceiling("neutral", "block") == ("block", True)
    assert regime.apply_ceiling("block", "favour") == ("block", False)
    assert regime.apply_ceiling("neutral", "favour") == ("neutral", False)
    assert regime.apply_ceiling("neutral", "neutral") == ("neutral", False)
    for b, c in itertools.product(regime.BIAS_RANK, regime.BIAS_RANK):
        new, _ = regime.apply_ceiling(b, c)
        assert RANK[new] <= RANK[b] and RANK[new] <= RANK[c]
    assert regime.lower_bias("favour", "block") == "block" and regime.lower_bias("favour", "weird") == "block"


def test_format_line_is_short():
    line = regime.format_line(regime.align(UP, "SHOCK_DOWN", {"event": "block", "event_note": "FOMC 2026-10-28"}, metal="GOLD"))
    assert line.startswith("GOLD[SHOCK_IN_UPTREND]") and len(line) < 220
