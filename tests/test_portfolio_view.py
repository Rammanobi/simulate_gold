"""'Where your money is': style A (cards + bar) and style B (donut/waffle + table), email-safe, numbers = the glance block."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio_fixtures as pfx  # noqa: E402

from simgold import config, report, summary  # noqa: E402

NAMES = ("all_cash", "one_holding", "three_holdings", "loss", "bought_today", "many_trades")
STYLES = ("A", "B")


@pytest.fixture(scope="module")
def days():
    return {s: pfx.scenarios(s) for s in STYLES}


def blocks(day, style):
    return report._funds_html(day, style) + report._trades_html(day, style)


def test_split_cells_adds_up():
    rows = [{"value": 49576}, {"value": 22064}, {"value": 0}, {"value": 29083}]
    c = report.split_cells(rows)
    assert sum(c) == 100 and c[2] == 0 and c[0] == 49
    assert report.split_cells([{"value": 0}, {"value": 0}]) == [0, 0]
    assert sum(report.split_cells([{"value": 1}, {"value": 1}, {"value": 1}])) == 100


def test_style_comes_from_config_and_defaults_to_a():
    assert summary.portfolio_style({}) == "A" and summary.portfolio_style({"report": {"portfolio_style": "b"}}) == "B"
    assert summary.portfolio_style({"report": {"portfolio_style": "zzz"}}) == "A"
    assert summary.portfolio_style(config.load_params()) in STYLES


def test_style_is_carried_in_the_summary(days):
    assert days["A"]["bought_today"]["money_summary"]["funds"]["style"] == "A"
    assert days["B"]["bought_today"]["money_summary"]["funds"]["style"] == "B"
    d = days["B"]["bought_today"]
    assert report.portfolio_style_of(d) == "B" and report.portfolio_style_of(d, "A") == "A"


def test_sentence_is_plain_and_complete(days):
    f = days["A"]["bought_today"]["money_summary"]["funds"]
    assert re.fullmatch(r"₹[\d,]+ \(\d+%\) is in gold, ₹[\d,]+ \(\d+%\) in silver, ₹0 in copper, ₹[\d,]+ \(\d+%\) is cash\. "
                        r"Overall [+−]₹[\d,]+ since start\.", f["sentence"]), f["sentence"]
    assert days["A"]["all_cash"]["money_summary"]["funds"]["sentence"].startswith("₹0 in gold") is False
    assert "Overall ₹0 since start." in days["A"]["all_cash"]["money_summary"]["funds"]["sentence"]


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("name", NAMES)
def test_renders_every_fund_and_reconciles(days, style, name):
    day = days[style][name]
    ms, f = day["money_summary"], day["money_summary"]["funds"]
    html = blocks(day, style)
    assert report.FUNDS_TITLE in html and "<svg" not in html.lower() and "<script" not in html.lower()
    for r in f["rows"]:
        label = report.FUND_NAMES[r["metal"]] if style == "B" else (r["symbol"] if r["kind"] == "fund" else "Cash")
        assert label in html, (style, name, label)
        if r["kind"] == "fund" and r["held"]:
            assert report.inr(r["value"]) in html and report.inr(r["invested"]) in html and f'{r["units"]:,}' in html
            assert report.sg(r["profit"]) in html and report.pc(r["profit_pct"], 2) in html
            assert report.inr(r["price"], 2) in html
        if r["kind"] == "fund" and not r["held"]:
            assert "not held" in html.lower()
    t = f["total"]
    assert report.inr(ms["today_profit"]) in html and report.sg(ms["overall_profit"]) in html
    assert abs(sum(_p(r["value"]) for r in f["rows"]) - _p(ms["cash"]) - _p(ms["metals_value"] if "metals_value" in ms else
              sum(m["value"] for m in ms["metals"]))) < 2
    assert t["value"] == ms["total_now"]
    if style == "A":
        assert report.inr(ms["total_now"], 2) in html and f["sentence"] in html.replace("&amp;", "&")
        assert html.count("bgcolor=") >= 1
    else:
        assert html.count('bgcolor="#') >= 100 or name == "all_cash"           # the 10 x 10 waffle


def _p(x):
    return round(float(x) * 100)


@pytest.mark.parametrize("style", STYLES)
def test_shares_add_up_to_the_money(days, style):
    for name in NAMES:
        f = days[style][name]["money_summary"]["funds"]
        assert abs(sum(r["share_pct"] for r in f["rows"]) - 100) < 0.5 or name == "all_cash" and abs(f["rows"][-1]["share_pct"] - 100) < 0.01


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("name", NAMES)
def test_width_and_email_safety(days, style, name):
    html = blocks(days[style][name], style)
    assert all(int(x) <= 720 for x in re.findall(r'(?<![-\w])width="(\d+)"', html))
    assert all(int(x) <= 720 for x in re.findall(r"(?<![-\w])width:(\d+)px", html))
    for bad in ("conic-gradient", "<style", "position:absolute", "display:flex", "display:grid", "<img", "<canvas"):
        assert bad not in html
    assert len(html) < 45_000                                         # the whole email must stay under Gmail's ~102 KB clip


@pytest.mark.parametrize("style", STYLES)
def test_trades_list_is_capped_and_says_so(days, style):
    day = days[style]["many_trades"]
    assert day["holdings_view"]["ledger_total"] >= 30
    html = report._trades_html(day, style)
    assert html.count("BUY</span>") + html.count("SELL</span>") == report.TRADES_SHOWN
    assert f"and {day['holdings_view']['ledger_total'] - report.TRADES_SHOWN} older" in html
    sells = [e for e in day["holdings_view"]["ledger"][: report.TRADES_SHOWN] if e["side"] == "SELL"]
    assert sells and all(report.sg(e["pnl"]) in html for e in sells)


def test_no_trades_message(days):
    assert "No buys or sells yet." in report._trades_html(days["A"]["all_cash"], "A")


@pytest.mark.parametrize("style", STYLES)
def test_full_email_and_text_carry_the_section(days, style):
    day = days[style]["bought_today"]
    page = report.render_email(day)
    assert report.FUNDS_TITLE in page and "<svg" not in page.lower()
    txt = report.render_text(day)
    assert report.FUNDS_TITLE.upper() in txt and day["money_summary"]["funds"]["sentence"] in txt
    assert len(page) < 102_000


@pytest.mark.parametrize("style", STYLES)
def test_busiest_email_stays_under_gmail_clip(days, style):
    assert len(report.render_email(days[style]["many_trades"])) < 102_000


def test_bought_today_numbers(days):
    r = {x["symbol"]: x for x in days["A"]["bought_today"]["money_summary"]["funds"]["rows"]}
    g = r["GOLDBEES"]
    assert g["units"] == 423 and g["avg_price"] == 116.5 and g["price"] == 117.2 and g["value"] == round(423 * 117.2, 2)
    assert g["profit"] == round(g["profit"], 2) and g["profit"] < g["value"] - 423 * 116.5 + 1
    assert r["HINDCOPPER"]["held"] is False and r["CASH"]["kind"] == "cash"


def test_dashboard_template_has_both_styles():
    t = (pfx.REPO / "docs" / "dashboard" / "template.html").read_text(encoding="utf-8")
    assert "f.style===\"B\"" in t and "pfDonut" in t and 'class="bar"' in t
