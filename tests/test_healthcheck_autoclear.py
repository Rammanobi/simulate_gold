import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import healthcheck as H  # noqa: E402


def _p(cat):
    return H.problem(cat, cat, "x")


def test_only_monitoring_noise_still_lets_safe_mode_clear():
    assert H.clean_for_safe_mode([])
    assert H.clean_for_safe_mode([_p("routines_silent"), _p("stale_guidance"), _p("watchdog_stale")])


def test_a_real_problem_keeps_safe_mode_on():
    assert not H.clean_for_safe_mode([_p("routines_silent"), _p("engine_not_running")])
    assert not H.clean_for_safe_mode([_p("data_source_down")])
