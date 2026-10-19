"""Under v2 the reports, the brief and should_think talk about the account stop and the brakes, not the Green Guard floor."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from conftest import d5_params
from simgold import brief, report
from simgold.broker import Portfolio, Position
from simgold.market import IST

ROOT = Path(__file__).resolve().parent.parent


def v2_day():
    day = json.loads((ROOT / "templates" / "sample_day.json").read_text(encoding="utf-8"))
    p = day["portfolio"]
    p.update(v2=True, floor=75000, distance_to_floor=p["close"] - 75000, drawdown_from_peak=-0.062, brake_level=0, peak=106000,
             floor_next_step="Brake level 0: full size. Value is 6.2% below its peak. At -15% every target is halved, at -25% everything is sold, "
                             "and at ₹75,000 the hard account stop sells everything and halts buys.", next_value=106000)
    day["targets"] = [{"symbol": "GOLDBEES", "state": "OFF", "sigma": 0.161, "target_pct": 50.0, "actual_pct": 49.0, "reason": "x"}]
    return day


def test_text_and_html_reports_describe_the_brakes_and_the_account_stop():
    day = v2_day()
    txt = report.render_text(copy.deepcopy(day), sample=True)
    assert "PORTFOLIO AND RISK BRAKES" in txt and "Account stop:" in txt and "Drawdown from peak: -6.2%" in txt
    assert "TREND TARGETS" in txt and "GOLDBEES" in txt and "PORTFOLIO AND FLOOR" not in txt and "Next step" not in txt
    html = report.render_email(copy.deepcopy(day), sample=True) if hasattr(report, "render_email") else report.render_html(copy.deepcopy(day), sample=True)
    assert "Above the account stop" in html and "Distance to safety floor" not in html and "Trend targets" in html
    assert "floor moves up" not in html
    s = report.summarize(copy.deepcopy(day))
    assert "account stop" in s and "brake level 0" in s and "floor is" not in s


def test_v1_reports_keep_the_floor_wording():
    day = json.loads((ROOT / "templates" / "sample_day.json").read_text(encoding="utf-8"))
    txt = report.render_text(day, sample=True)
    assert "PORTFOLIO AND FLOOR" in txt and "Next step" in txt


def test_brief_and_should_think_use_stop_and_drawdown_under_v2(tmp_path):
    from datetime import datetime
    p = d5_params()
    (tmp_path / "state").mkdir()
    pf = Portfolio()
    pf.cash, pf.liquidbees, pf.floor = 0.0, 80000.0, 75000.0
    pf.positions["GOLDBEES"] = Position(qty=100, avg_cost=100.0, last_price=100.0, ai_stop=95.0, exit_level=90.0)
    pf.runtime = {"v2": {"brake": {"peak": 100000.0, "level": 1}}}
    pf.save(tmp_path / "state" / "portfolio.json")
    pv = brief.portfolio_view(tmp_path, p, datetime(2026, 10, 14, 12, 0, tzinfo=IST))
    assert pv["v2"] and pv["account_stop"] == 75000 and pv["brake_level"] == 1 and pv["dd"] < -0.09
    assert pv["positions"][0]["stop"] == 95.0 and pv["positions"][0]["exit_level"] == 90.0
    text = "\n".join(brief.assemble(tmp_path, datetime(2026, 10, 14, 12, 0, tzinfo=IST), p, 5, []))
    assert "account stop" in text and "brake level 1" in text and "cushion" not in text
    assert "AI stop 95.00" in text and "trend turns OFF at 90.00" in text
