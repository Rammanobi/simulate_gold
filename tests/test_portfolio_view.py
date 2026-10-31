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
STYLES = ("AB", "A", "B")


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
    assert summary.portfolio_style({}) == "AB" and summary.portfolio_style({"report": {"portfolio_style": "b"}}) == "B"
    assert summary.portfolio_style({"report": {"portfolio_style": "a"}}) == "A"
    assert summary.portfolio_style({"report": {"portfolio_style": "zzz"}}) == "AB"
    assert config.load_params()["report"]["portfolio_style"] == "AB"
    assert summary.portfolio_style(config.load_params()) in STYLES


def test_style_is_carried_in_the_summary(days):
    assert days["AB"]["bought_today"]["money_summary"]["funds"]["style"] == "AB"
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
        labels = ([report.FUND_NAMES[r["metal"]]] if style in ("B", "AB") else []) + ([r["symbol"]] if style in ("A", "AB") and r["kind"] == "fund" else [])
        assert labels or style == "A" and r["kind"] == "cash"
        for label in labels:
            assert label in html, (style, name, label)
        if r["kind"] == "fund" and r["held"]:
            assert report.inr(r["value"]) in html and report.inr(r["invested"]) in html and f'{r["units"]:,}' in html
            assert report.sg(r["profit"]) in html and report.pc(r["profit_pct"], 2) in html
            assert report.inr(r["price"], 2) in html
        if r["kind"] == "fund" and not r["held"]:
            assert "not held" in html.lower()
    t = f["total"]
    assert "Total money now" not in html and "Today's profit" not in html and "Overall profit" not in html      # shown once, in the glance block
    assert abs(sum(_p(r["value"]) for r in f["rows"]) - _p(ms["cash"]) - _p(ms["metals_value"] if "metals_value" in ms else
              sum(m["value"] for m in ms["metals"]))) < 2
    assert t["value"] == ms["total_now"]
    if style in ("A", "AB"):
        assert f["sentence"] in html.replace("&amp;", "&")
        assert 1 <= html.count("bgcolor=") < 100                      # the stacked bar (and card strips), never the waffle
    if style in ("B", "AB"):
        assert "Shares</td>" in html and "Buy price" in html and "Price now" in html
        assert f"on {report.inr(t['invested'])} invested" in html and "Profit on the money invested" in html
    if style == "B":
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
    assert len(html) < 50_000                                         # the whole email must stay under Gmail's ~102 KB clip


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


def test_ab_order_sentence_bar_cards_table_tiles_trades(days):
    html = report._funds_html(days["AB"]["bought_today"]) + report._trades_html(days["AB"]["bought_today"])
    marks = ["is in gold", "Gold</b>", "GOLDBEES", "Buy price", report.TRADES_TITLE]
    pos = [html.index(m) for m in marks]
    assert pos == sorted(pos), dict(zip(marks, pos))
    assert "bgcolor=\"#c9a227\"" in html and html.count("bgcolor=") < 100


def test_total_row_percent_is_labelled_as_on_money_invested(days):
    html = report._funds_html(days["AB"]["three_holdings"], "AB")
    f = days["AB"]["three_holdings"]["money_summary"]["funds"]["total"]
    assert f"{report.pc(f['profit_pct'], 2)} </b>" not in html
    assert f"{report.sg(f['profit'])} · {report.pc(f['profit_pct'], 2)}</b> on {report.inr(f['invested'])} invested" in html


@pytest.mark.parametrize("style", STYLES)
def test_full_email_has_no_duplicate_sections_and_tiles_stack(days, style):
    page = report.render_email(days[style]["bought_today"])
    assert "Every purchase so far" not in page and "Your holdings" not in page
    assert report.FUNDS_TITLE in page and report.TRADES_TITLE in page
    assert 'width="33%"' not in page and "display:inline-block;vertical-align:top;width:100%;max-width:186px" in page
    closed = dict(days[style]["bought_today"], closed=True)
    assert "Every purchase so far" not in report.render_email(closed)


@pytest.mark.parametrize("style", STYLES)
def test_one_flow_no_number_shown_twice(days, style):
    day = days[style]["bought_today"]
    page = report.render_email(day)
    ms = day["money_summary"]
    assert page.count("Total money now") == 1 and page.count("Overall profit<div") == 1
    assert "Invested in metals now" not in page and "Metals held, valued at" not in page
    assert page.count(report.inr(ms["total_now"], 2)) == 1
    glance = report._summary_html(ms)
    for lab in ("Total money now", "Today's profit", "Overall profit", "Live profit right now", "Today's investment", "Overall investment so far"):
        assert lab in glance
    assert glance.index("Total money now") < glance.index("Today's profit") < glance.index("Overall profit") < glance.index("Live profit") \
        < glance.index("Today's investment") < glance.index("Overall investment")
    assert page.index(report.SUMMARY_TITLE) < page.index(report.FUNDS_TITLE) < page.index(report.TRADES_TITLE)
    txt = report.render_text(day)
    assert "Invested in metals now" not in txt and txt.count("Total money now") == 1


@pytest.mark.parametrize("style", STYLES)
def test_header_weekday_matches_the_date(days, style):
    day = days[style]["bought_today"]
    assert day["date"] == "2026-10-29"
    day["weekday"] = "Monday"                                         # a stale stored weekday must not leak into the header
    page = report.render_email(day)
    assert "Thursday, 29 October 2026" in page and "Monday, 29 October" not in page
    assert "Thursday 29 Oct 2026" in report.render_text(day)
