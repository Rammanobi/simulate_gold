"""17 Nov 2026: the reminders run at 09:15:43 IST raised HIGH `ticks_missing` at 09:16 (the engine's first ticks push to main is its first
5-min sync, about 09:20), nothing resolved it (only watchdog.yml ran `resolve-missing`), and the engine's evaluate_safe_mode ("HIGH open
> 30 min") switched safe mode on at 09:46. Fixes: grace until 09:15 + TICK_MAX_AGE_MIN, reminders.yml resolves too, and a pytest failure
sets safe mode for one engine process only (nothing written to state/safe_mode.json)."""
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import healthcheck as H  # noqa: E402

from simgold import alerts as A  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
IST = H.IST
TUE = (2026, 11, 17)                                   # a Tuesday, NSE trading day
CFG = dict(A.DEFAULTS)


def at(h, m, s=0):
    return datetime(*TUE, h, m, s, tzinfo=IST)


def gh_running(args): return json.dumps({"total_count": 1})


def ticks_missing(root, now):
    return [p for p in H.check_engine(root, now, gh_running, "o/r") if p["check"] == "ticks_missing"]


# ---- 1. time grace ---------------------------------------------------------------
def test_grace_constant_is_0927():
    assert H.TICKS_MISSING_FROM.strftime("%H:%M") == "09:27"


def test_no_ticks_missing_before_0927(tmp_path):
    (tmp_path / "logs").mkdir()
    assert ticks_missing(tmp_path, at(9, 14)) == []
    assert ticks_missing(tmp_path, at(9, 16)) == []
    assert ticks_missing(tmp_path, at(9, 26, 59)) == []


def test_ticks_missing_from_0927(tmp_path):
    (tmp_path / "logs").mkdir()
    for h, m in ((9, 27), (9, 28), (11, 0)):
        p = ticks_missing(tmp_path, at(h, m))
        assert len(p) == 1 and p[0]["severity"] == "high" and p[0]["category"] == "engine_not_running"


def test_run_checks_at_0916_has_no_ticks_missing(tmp_path):
    (tmp_path / "state").mkdir(); (tmp_path / "logs").mkdir()
    probs = H.run_checks(tmp_path, at(9, 16), gh_running, "o/r", lambda r, a: "")
    assert "ticks_missing" not in {p["check"] for p in probs}


def test_grace_does_not_hide_a_file_that_exists_but_is_stale(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "ticks-2026-11-17.csv").write_text("time,value,floor,cash,liquidbees\n09:15,100000,75000,0,0\n")
    p = H.check_engine(tmp_path, at(9, 40), gh_running, "o/r")
    assert [x["check"] for x in p] == ["ticks_stale"]


# ---- 2. reminders.yml resolves what went away ---------------------------------------
def test_reminders_workflow_resolves_like_watchdog():
    text = (ROOT / ".github" / "workflows" / "reminders.yml").read_text(encoding="utf-8")
    yaml.safe_load(text)
    assert "resolve-missing --component watchdog" in text
    assert text.index("resolve-missing --component watchdog") > text.index("simgold.alerts problems")
    assert text.index("resolve-missing --component watchdog") < text.index("Record acknowledgements")


def test_resolve_missing_closes_ticks_missing_when_current_is_clean(tmp_path):
    A.raise_alert(tmp_path, "watchdog", "ticks_missing", "engine_not_running", "x", now=at(9, 16).timestamp(), cfg=CFG)
    A.main(["--state", str(tmp_path), "resolve-missing", "--component", "watchdog", "--current", ""])
    e = A.load_state(tmp_path)["alerts"]["watchdog|ticks_missing|"]
    assert e["status"] == "resolved"


def test_resolve_missing_keeps_still_current_alerts_open(tmp_path):
    A.raise_alert(tmp_path, "watchdog", "ticks_missing", "engine_not_running", "x", now=at(9, 30).timestamp(), cfg=CFG)
    A.main(["--state", str(tmp_path), "resolve-missing", "--component", "watchdog", "--current", "ticks_missing"])
    assert A.load_state(tmp_path)["alerts"]["watchdog|ticks_missing|"]["status"] == "open"


# ---- 3. the exact 17 Nov sequence ------------------------------------------------------
def test_17_nov_sequence_old_behaviour_would_set_safe_mode_new_does_not(tmp_path):
    root = tmp_path
    (root / "state").mkdir(); (root / "logs").mkdir()
    t0 = at(9, 16).timestamp()
    # old behaviour: HIGH raised at 09:16 and never resolved -> safe mode at 09:46 (kept as the proof of the false positive)
    A.raise_alert(root / "state", "watchdog", "ticks_missing", "engine_not_running", "x", now=t0, cfg=CFG)
    assert A.evaluate_safe_mode(root / "state", t0 + 31 * 60, CFG)
    A.clear_safe_mode(root / "state")
    (root / "state" / "alerts.json").unlink()
    # new behaviour: the 09:16 healthcheck raises nothing (grace)
    probs = [p for p in H.check_engine(root, at(9, 16), gh_running, "o/r")]
    assert probs == []
    # even if one had been raised (e.g. a stale reminders run), the reminders step resolves it once the check is clean
    A.raise_alert(root / "state", "watchdog", "ticks_missing", "engine_not_running", "x", now=t0, cfg=CFG)
    A.main(["--state", str(root / "state"), "resolve-missing", "--component", "watchdog", "--current", ""])
    assert not A.evaluate_safe_mode(root / "state", t0 + 31 * 60, CFG) and not A.safe_mode_on(root / "state")


# ---- 4. test failure: safe mode for this process only --------------------------------
def test_session_safe_mode_blocks_buys_but_writes_nothing(tmp_path):
    assert not A.safe_mode_on(tmp_path)
    A.set_session_safe_mode("tests failing")
    assert A.safe_mode_on(tmp_path) and not (tmp_path / "safe_mode.json").exists()
    assert A.safe_mode_info(tmp_path) == {}                       # nothing for the pre-market auto-clear to find
    A.set_session_safe_mode(None)                                  # a later process starts clean
    assert not A.safe_mode_on(tmp_path)


def test_incident_safe_mode_still_persists_next_to_a_session_one(tmp_path):
    A.set_session_safe_mode("tests failing")
    assert A.set_safe_mode(tmp_path, "5 incidents", notify=False)   # not swallowed by the session flag
    assert A.safe_mode_info(tmp_path)["on"] is True
    A.set_session_safe_mode(None)
    assert A.safe_mode_on(tmp_path)                                 # the file one outlives the process, as before
