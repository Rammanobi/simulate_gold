"""simgold/trust.py + tools/trust.py: the recommended ai_trust from the calibration ledger. Recommends only; read-only."""
from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from v3tools_fix import changed, tree

import calibration as cal
import trust as trust_tool
from simgold import trust as tr
from simgold.market import IST

NOW = datetime(2026, 12, 4, 16, 0, tzinfo=IST)
GOOD_VAL = {"n": 60, "invalid": 0, "rate": 1.0, "invalid_days": []}
METALS = ("GOLD", "SILVER", "COPPER")


def ledger(root, n, pp_mean=0.2, pp_sd=0.1, skill=0.3, brier_noise=0.0, fc_bad=0.0, last_pp=None, seed=1, desks=True):
    """A synthetic ledger of n scored business days ending 2026-12-04. skill: model Brier = (1 - skill) x the base-rate Brier."""
    rng = random.Random(seed)
    days = [d.date().isoformat() for d in pd.bdate_range(end="2026-12-04", periods=n)]
    oc = {d: "".join(rng.choices("ufd", weights=(4, 3, 3), k=3)) for d in days}
    rates = {}
    for j, m in enumerate(METALS):
        o = [v[j] for v in oc.values()]
        rates[m] = (o.count("u") / n, o.count("d") / n)
    base = lambda d, j: tr._brier(*rates[METALS[j]], oc[d][j])                                   # noqa: E731
    pairs, syn, desk_rows, sets = [], [], [], []
    for i, d in enumerate(days):
        pp = pp_mean + rng.gauss(0, pp_sd)
        if last_pp is not None and i >= n - 20:
            pp = last_pp
        pairs.append({"d": d, "f": "f", "pp": round(pp, 4), "ai": 0.0, "df": 0.0, "rs": 0.0})
        bs = [base(d, j) for j in range(3)]
        syn.append([d, round(max(0.0, (1 - skill) * sum(bs) / 3 + rng.gauss(0, brier_noise)), 4)])
        if desks:
            for j, m in enumerate(METALS):
                desk_rows.append([d, m, round(max(0.0, (1 - skill) * bs[j] + rng.gauss(0, brier_noise)), 4)])
        sets.append({"d": d, "ph": "f", "t": d + "T08:00:00", "c": 0.6, "w": [1, 0, 0], "dw": [1, 0, 0], "px": [1, 1, 1], "h": 5, "sc": [],
                     "st": "s", "fc": [0, 1] if rng.random() < fc_bad else [1, 0]})
    led = {"v": 1, "updated": None, "sets": sets, "calls": [], "pairs": pairs, "oc": oc,
           "scores": {"synthesis": syn, "gold": [r for r in desk_rows if r[1] == "GOLD"]}, "dk": {}}
    f = Path(root) / "memory" / "ltm" / "calibration.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(led))
    return led


@pytest.fixture
def root(tmp_path):
    return tmp_path


def rec(root, **kw):
    return tr.compute(root, NOW, validity=kw.pop("validity", GOOD_VAL), params={})


@pytest.mark.parametrize("n,want", [(25, 0.25), (45, 0.5), (65, 0.75), (125, 1.0)])
def test_skilled_ai_climbs_the_table_with_n(root, n, want):
    ledger(root, n)
    t = rec(root)
    assert t["recommended"] == want, t["reasons"]
    assert t["brier_skill"]["synthesis"]["skill"] > 0.2 and t["paired"]["ci95"][0] > 0


def test_no_skill_ai_gets_zero(root):
    ledger(root, 130, pp_mean=0.0, pp_sd=0.3, skill=0.0, brier_noise=0.02)
    t = rec(root)
    assert t["recommended"] == 0.0 and "not reached" in " ".join(t["reasons"])


def test_noisy_ai_gets_zero_even_with_a_positive_mean(root):
    ledger(root, 130, pp_mean=0.05, pp_sd=3.0, skill=0.05, brier_noise=0.2)
    t = rec(root)
    assert t["paired"]["ci95"][0] < 0 < t["paired"]["ci95"][1] and t["recommended"] == 0.0


def test_short_history_says_too_few_to_rank(root):
    ledger(root, 15)
    t = rec(root)
    assert t["recommended"] == 0.0 and "15 days too few to rank" in t["power_note"] and "too few to rank" in tr.summary_line(t)


def test_brier_needs_a_positive_ci_from_half_trust(root):
    ledger(root, 45, skill=0.01, brier_noise=0.15)
    t = rec(root)
    assert t["recommended"] <= 0.25


def test_validity_and_misquote_gates(root):
    ledger(root, 65)
    assert tr.compute(root, NOW, validity={"n": 60, "invalid": 6, "rate": 0.9, "invalid_days": []}, params={})["recommended"] == 0.0
    assert tr.compute(root, NOW, validity=None, params={})["recommended"] == 0.0                          # unknown validity is not a pass
    ledger(root, 65, fc_bad=0.3)
    t = rec(root)
    assert t["recommended"] == 0.0 and t["factcheck"]["rate"] > 0.05
    ledger(root, 65)
    assert rec(root)["recommended"] == 0.75


def test_unknown_factcheck_is_not_a_pass(root):
    led = ledger(root, 65)
    for s in led["sets"]:
        s.pop("fc")
    (Path(root) / "memory/ltm/calibration.json").write_text(json.dumps(led))
    assert rec(root)["recommended"] == 0.0


def test_step_down_after_a_losing_month(root):
    ledger(root, 65, last_pp=-0.6)                                            # most of November at -0.6 pp a day
    t = rec(root)
    assert t["recommended"] == 0.0 and any("STEP DOWN" in r and "lost" in r for r in t["reasons"])


def test_step_down_after_five_breaches(root):
    ledger(root, 65)
    v = {"n": 60, "invalid": 5, "rate": 0.99, "invalid_days": ["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"]}
    t = tr.compute(root, NOW, validity=v, params={})
    assert t["recommended"] == 0.0 and any("5 rule breaches" in r for r in t["reasons"])
    v["invalid_days"] = v["invalid_days"][:4]
    assert tr.compute(root, NOW, validity=v, params={})["recommended"] == 0.75


def test_no_ledger_is_insufficient_data(root, capsys):
    t = tr.compute(root, NOW, params={})
    assert t["recommended"] == 0.0 and "insufficient data, recommend 0" in tr.summary_line(t)
    assert trust_tool.main(["--root", str(root), "--now", NOW.isoformat(), "show"]) == 0
    assert "insufficient data, recommend 0" in capsys.readouterr().out


def test_empty_or_damaged_ledger_never_raises(root):
    f = Path(root) / "memory" / "ltm" / "calibration.json"
    f.parent.mkdir(parents=True)
    for txt in ("", "not json", "[]", "{}", json.dumps({"sets": 5, "pairs": [None, 3, {"d": 1}], "scores": {"synthesis": [1, "x"]}, "oc": 4})):
        f.write_text(txt)
        t = tr.compute(root, NOW, params={})
        assert t["recommended"] == 0.0
        tr.text(t)


def test_week_option_uses_only_data_up_to_that_week(root, capsys):
    ledger(root, 125)
    full = tr.compute(root, NOW, validity=GOOD_VAL, params={})
    early = tr.compute(root, NOW, week="2026-W40", validity=GOOD_VAL, params={})
    assert early["paired"]["n"] < full["paired"]["n"] and early["asof"] == "2026-10-02"
    assert trust_tool.main(["--root", str(root), "--now", NOW.isoformat(), "show", "--week", "bad"]) == 2


def test_math_is_transparent_and_deterministic(root):
    p = tr.paired([1.0, 2.0, 3.0, 4.0], 200, 7)
    assert p["mean_pp"] == 2.5 and p["std_pp"] == pytest.approx(1.291, abs=1e-3) and p["t"] == pytest.approx(3.873, abs=1e-3)
    assert tr.paired([1.0, 2.0, 3.0, 4.0], 200, 7) == p
    s = tr.skill([("a", 0.1, 0.2), ("b", 0.1, 0.2), ("c", 0.1, 0.2)], 100, 1)
    assert s["skill"] == 0.5 and tr.block_len(130) == 5 and tr.block_len(12) == 2 and tr.block_len(3) == 1
    assert tr._brier(0.5, 0.5, "up"[0]) == 0.25


def test_read_only_and_json(root, capsys):
    ledger(root, 65)
    before = tree(root)
    assert trust_tool.main(["--root", str(root), "--now", NOW.isoformat(), "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["asof"] == "2026-12-04"
    tr.compute(root, NOW, params={})
    assert changed(before, tree(root)) == set()


def test_thresholds_can_be_overridden_by_the_trust_block():
    s = tr.settings({"trust": {"min_days": [5, 10, 15, 20], "min_validity": 0.9, "max_mismatch": "x", "step_down_breaches": 0}})
    assert s["min_days"] == [5, 10, 15, 20] and s["min_validity"] == 0.9 and s["max_mismatch"] == 0.05 and s["step_down_breaches"] == 5
    assert tr.settings(None)["min_days"] == [20, 40, 60, 120] and tr.settings({"trust": {"min_days": [3, 2, 1, 0]}})["min_days"] == [20, 40, 60, 120]


# ------------------------------------------------------------------ the weekly outputs
def test_week_output_is_unchanged_without_a_ledger_and_gets_one_line_with_one(root, monkeypatch):
    (root / "state").mkdir()
    w = cal.week_stats(root, "2026-W48", NOW, closes={})
    assert "trust_line" not in w and "trust:" not in cal.week_md(w)
    ledger(root, 30)
    w2 = cal.week_stats(root, "2026-W48", NOW, closes={})
    assert w2["trust_line"].startswith("trust: ") and cal.week_md(w2).splitlines()[-1] == w2["trust_line"]
    assert {k: v for k, v in w2.items() if k not in ("trust_line", "desk_brier", "ai_vs_default", "ai_vs_default_pairs", "ai_vs_default_week_pp")} == \
        {k: v for k, v in w.items() if k not in ("desk_brier", "ai_vs_default", "ai_vs_default_pairs", "ai_vs_default_week_pp")}


def test_scoring_records_outcomes_for_the_base_rate(tmp_path, monkeypatch):
    from test_calibration import MOVES, clean, closes
    from v3tools_fix import mkroot
    r = mkroot(tmp_path / "x", monkeypatch)
    cal.record_proposal(r, datetime(2026, 11, 2, 8, 55, tzinfo=IST), clean())
    cal.score(r, datetime(2026, 11, 20, 10, 0, tzinfo=IST), closes=closes(MOVES))
    led = cal.load(r)
    assert led["oc"] == {"2026-11-02": "udf"}
    assert tr.compute(r, NOW, params={}, validity=GOOD_VAL)["brier_skill"]["synthesis"]["n_obs"] == 1
