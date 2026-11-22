"""The v3-mode twin T1 must trade like the real engine's v3 default. The stored ticks of 13-20 Nov 2026 are replayed through the real tick with
`allocation.phase: live_default` (the engine's fully invested D5-FI default trades, ramping in under the turnover limits and the dust sweep), and
after every daily check the twin T1 is compared with the real book (T0 mirrors it): every weight within half a point."""
from __future__ import annotations

import pytest

import v3_replay as R
from simgold import twins

DAYS = ["2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
SYMS = ("w_GOLDBEES", "w_SILVERBEES", "w_HINDCOPPER")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("parity")
    p = R.shipped_params()
    p["allocation"]["phase"] = "live_default"
    p["twins"]["enabled"] = True
    days = []

    def hook(pf, mkt, env):
        rows = twins.after_tick(pf, mkt, env.params, env.state_dir, env.logs_dir, nifty_feed=lambda t: 271.0)
        if rows:
            days.append({r["twin"]: r for r in rows})
    pf, env = R.replay(tmp, p, days=DAYS, after_tick=hook)
    return pf, days


def test_t1_follows_the_real_v3_default_within_half_a_point(run):
    _pf, days = run
    assert [d["T0"]["date"] for d in days] == DAYS
    for d in days:
        for k in SYMS + ("w_cash",):
            assert abs(d["T1"][k] - d["T0"][k]) <= 0.005, (d["T0"]["date"], k, d["T0"][k], d["T1"][k])


def test_the_comparison_is_about_a_real_ramp_in_with_the_dust_sweep(run):
    _pf, days = run
    gold = [d["T0"]["w_GOLDBEES"] for d in days]
    assert 0.21 < gold[0] < 0.24 and gold[-1] > 0.55                                                   # 20% ramp limit plus the ~3% dust sweep on day 1
    assert days[0]["T1"]["w_GOLDBEES"] > 0.21                                                          # the twin sweeps during the ramp-in too (was 19.9%)
    assert days[-1]["T1"]["w_GOLDBEES"] > 0.60                                                         # 20 Nov: 57.6% before the fix, the real book 63.4%
