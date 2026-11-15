"""The compact daily email (report.portfolio_style: COMPACT): broker card, Why cash?, timeline, you vs gold, safety.

Real fixture: the engine state at the close of Friday 6 Nov 2026 (tests/fixtures/2026-11-06) plus the state files the
compact email reads (last_prices.json for 'today %', events.json for the next event) and a start-day tick file for the
'you vs gold' bars. Every figure must equal money_summary (one definition per metric) and the plain text must carry the
same numbers as the HTML.
"""
import copy
import html as htmllib
import json
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import portfolio_fixtures as pfx  # noqa: E402

from simgold import config, report, summary  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "2026-11-06"
DAY = "2026-11-06"
LINK = "https://claude.ai/artifact/1YVZ1m6rDxCwccPzQtCz73#2026-11-06"
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


@pytest.fixture(scope="module")
def shipped_config():
    p = config.load_params()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(p["strategy"], "mode", "v2_trend")
        mp.setitem(p["capital"], "idle", "cash")
        mp.setitem(p["report"], "portfolio_style", "COMPACT")
        yield p


@pytest.fixture(scope="module")
def root(tmp_path_factory, shipped_config):
    r = tmp_path_factory.mktemp("compact")
    shutil.copytree(FIX, r, dirs_exist_ok=True)
    # a start day for "you vs gold": first engine tick 13 Oct (GOLDBEES 121.0)
    (r / "logs" / "ticks-2026-10-13.csv").write_text("t,value,cash,liquidbees,floor,GOLDBEES\n09:16,100000,100000,0,75000,121.0\n", encoding="utf-8")
    (r / "state" / "last_prices.json").write_text(json.dumps({
        "GOLDBEES": {"price": 116.2, "ts": "2026-11-06T15:29:00+05:30", "prev_close": 118.12},
        "SILVERBEES": {"price": 174.95, "ts": "2026-11-06T15:29:00+05:30", "prev_close": 177.86},
        "HINDCOPPER": {"price": 424.0, "ts": "2026-11-06T15:29:00+05:30", "prev_close": 427.33}}), encoding="utf-8")
    (r / "state" / "events.json").write_text(json.dumps([
        {"date": "2026-11-06", "type": "JOBS", "time": "19:00"}, {"date": "2026-11-09", "type": "CPI", "time": "19:00"}]), encoding="utf-8")
    (r / "state" / "guidance.json").write_text(json.dumps({"updated": "2026-11-06T14:00:00+05:30", "bias": {"GOLD": "neutral", "SILVER": "favour", "COPPER": "block"}}), encoding="utf-8")
    (r / "state" / "budget.json").write_text(json.dumps({"mode": "THRIFT"}), encoding="utf-8")
    return r


@pytest.fixture(scope="module")
def real(root):
    day = report.build_day(DAY, root)
    return {"day": day, "html": report.render_email(day, LINK), "text": report.render_text(day, report_url=LINK)}


@pytest.fixture(scope="module")
def morning(root):
    day = report.build_morning("2026-11-09", root)
    return {"day": day, "html": report.render_email(day, LINK), "text": report.render_text(day, report_url=LINK)}


@pytest.fixture(scope="module")
def examples():
    out = {}
    for k, d in pfx.scenarios("COMPACT").items():
        out[k] = {"day": d, "html": report.render_email(d, LINK), "text": report.render_text(d, report_url=LINK)}
    return out


def all_cases(real, morning, examples):
    return [("real", real), ("morning", morning)] + [(k, v) for k, v in examples.items()]


# ------------------------------------------------------------------ parsing helpers
class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags(h):
    p = Tags()
    p.feed(h)
    return p.tags


def visible(h):
    t = re.sub(r"<style.*?</style>", "", h, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t).replace(chr(160), " "))


FIG = re.compile(r"[↑↓→]?\s?[+−]?₹[\d,]+(?:\.\d+)?|[+−]?\d+(?:\.\d+)?%")


def figures(s):
    return {re.sub(r"\s+", "", m) for m in FIG.findall(s)}


# ------------------------------------------------------------------ config
def test_default_style_is_compact_and_ab_is_the_rollback():
    shipped = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "params.yaml").read_text(encoding="utf-8"))
    assert shipped["report"]["portfolio_style"] == "COMPACT"
    assert summary.portfolio_style({}) == "COMPACT" and summary.portfolio_style({"report": {"portfolio_style": "ab"}}) == "AB"
    assert summary.portfolio_style({"report": {"portfolio_style": "zzz"}}) == "COMPACT"
    d = pfx.make_day({}, [], {"GOLDBEES": 117.2}, "AB")
    h = report.render_email(d)
    assert report.FUNDS_TITLE in h and "WHY CASH" not in h


# ------------------------------------------------------------------ email-safety rules
def test_size_limits(real, morning, examples):
    assert len(real["html"].encode()) <= 60_000 and len(morning["html"].encode()) <= 60_000
    assert len(examples["three_holdings"]["html"].encode()) <= 60_000
    assert len(examples["many_trades"]["html"].encode()) <= 60_000


def test_no_svg_img_emoji_script(real, morning, examples):
    for name, c in all_cases(real, morning, examples):
        for tag, _ in tags(c["html"]):
            assert tag not in ("svg", "img", "script", "link", "iframe"), name
        assert not EMOJI.search(c["html"]) and not EMOJI.search(c["text"]), name
        assert "position:" not in c["html"] and "@import" not in c["html"], name


def test_coloured_cells_carry_bgcolor_and_both_css_backgrounds(real, morning, examples):
    for name, c in all_cases(real, morning, examples):
        n = 0
        for tag, a in tags(c["html"]):
            st = a.get("style") or ""
            if tag in ("td", "table", "body") and ("background" in st or "bgcolor" in a):
                n += 1
                assert a.get("bgcolor"), (name, tag, st)
                assert "background-color:" in st and "linear-gradient(" in st, (name, tag, st)
                col = a["bgcolor"].lower()
                assert f"background-color:{col}" in st.lower() and f"linear-gradient({col},{col})" in st.lower(), (name, st)
        assert n > 10, name
    assert '<meta name="color-scheme" content="light">' in real["html"]


def test_font_sizes_at_least_13px(real, morning, examples):
    for name, c in all_cases(real, morning, examples):
        sizes = [float(x) for x in re.findall(r"font-size:\s*([\d.]+)px", c["html"])]
        assert sizes and min(s for s in sizes if s > 0) >= 13, name
        # a zero font size is only ever an empty spacer cell
        for m in re.finditer(r'<td[^>]*font-size:0;[^>]*>(.*?)</td>', c["html"]):
            assert m.group(1) in ("&nbsp;",), (name, m.group(1)[:40])


def test_every_pnl_figure_has_arrow_and_sign(real, examples):
    ms = real["day"]["money_summary"]
    bd = ms["breakdown"]
    h = real["html"]
    for v in (ms["overall_profit"], ms["today_profit"], bd["booked"], ms["funds"]["rows"][0]["profit"]):
        arrow = "↑" if round(v) > 0 else "↓"
        assert f"{arrow} {report.sg(v)}" in h, v
    for v in (bd["open"], bd["interest"]):
        assert report.sg(v) in real["html"]
    for name, c in list(examples.items()) + [("real", real)]:
        v = visible(c["html"])
        v = re.sub(r"open [^=]*= overall \S+", "", v)           # the reconcile line is a sum, its terms are labelled
        for m in re.finditer(r"[+−]₹[\d,.]+", v):
            assert re.search(r"[↑↓→] $", v[max(0, m.start() - 2):m.start()]) or v[max(0, m.start() - 2):m.start()].strip() in ("↑", "↓", "→"), (name, v[m.start() - 12:m.end()])


# ------------------------------------------------------------------ numbers
def test_real_figures_are_the_unified_summary(real):
    ms = real["day"]["money_summary"]
    h, t = visible(real["html"]), real["text"]
    assert ms["total_now"] == 100328.32
    for s in ("₹1,00,328", "₹49,574", "+₹328 (+0.33%)", "−₹834", "−₹1,166", "+₹1,348", "+₹147", "₹1,01,198", "₹25,328 above stop", "brake 0"):
        assert s in h and s in t, s
    assert "open +₹1,348 + booked −₹1,166 + interest +₹147 = overall +₹328" in h
    # the holding's value is summary.settle()'s own (paisa-exact), worded like the full report's card
    for x in (h, t):
        assert "437 shares × ₹116.20 = ₹50,779 · ₹50,754 after selling cost" in x
    assert len(real["html"].encode()) <= 60 * 1024
    assert "Gold 51% · Cash 49%" in h and "437 × ₹113.00 · stop ₹115.00" in h and "LTP ₹116.20" in h
    assert abs(ms["total_now"] - (sum(m["value"] for m in ms["metals"]) + ms["cash"])) <= 1.0
    bd = ms["breakdown"]
    assert round(ms["overall_profit"], 2) == round(bd["open"] + bd["booked"] + bd["interest"], 2)


def test_html_and_text_carry_the_same_numbers(real, morning, examples):
    for name, c in all_cases(real, morning, examples):
        a, b = figures(visible(c["html"])), figures(c["text"])
        assert a == b, (name, a ^ b)


def test_examples_match_money_summary(examples):
    for name, c in examples.items():
        ms = c["day"]["money_summary"]
        assert report.inr(ms["total_now"]) in c["html"], name
        assert report.inr(ms["cash"]) in c["html"], name
        assert abs(ms["total_now"] - (sum(m["value"] for m in ms["metals"]) + ms["cash"])) <= 1.0, name
        for r in ms["funds"]["rows"]:
            if r["kind"] == "fund" and r["held"]:
                assert r["symbol"] in c["html"] and report.sg(r["profit"]) in c["html"], (name, r["symbol"])
    h = examples["three_holdings"]["html"]
    assert all(s in h for s in ("GOLDBEES", "SILVERBEES", "HINDCOPPER"))
    assert "Gold " in visible(h) and "Silver " in visible(h) and "Copper " in visible(h)


def test_why_cash_uses_numeric_fields_not_text(real, root, shipped_config):
    day = copy.deepcopy(real["day"])
    for t in day["targets"]:
        t["reason"] = "garbage that must not be parsed"
    assert report.render_email(day, LINK) == real["html"]
    h = visible(real["html"])
    for s in ("₹116.13 / ₹124.48", "₹174.57 / ₹234.28", "₹421.45 / ₹584.53", "needs +7.2% · if ON alone 71% · now 51% / target 50%",
              "min 50% in gold", "silver cap 70%", "copper cap 35%", "trades only if gap > 5 pts", "Gold is held at the 50% floor"):
        assert s in h, s
    day2 = copy.deepcopy(day)
    day2["targets"][0]["level"] = 130.0
    assert "₹116.13 / ₹130.00" in visible(report.render_email(day2, LINK))


def test_today_block_and_stale_text(real):
    day = copy.deepcopy(real["day"])
    day["tomorrow"] = "Stale text about Monday 2 Nov that must not appear"
    h = report.render_email(day, LINK)
    assert "Stale text" not in h
    v = visible(h)
    assert "0 buys" in v and "0 sells" in v and "1 blocked" in v
    assert "No trades · check kept gold at 50%" in v and "blocked GOLDBEES · price shock −1.52%" in v
    assert "Next event: CPI Mon 9 Nov 19:00" in v and "4.3 pts ahead" in v and "since 13 Oct" in v
    assert "engine 372 checks · thrift" in v and "Open dashboard" in h and LINK in h


def test_removed_sections_are_gone(real):
    v = visible(real["html"])
    for old in ("Your money at a glance", "Where your money is", "Every trade so far", "Day at a glance", "Thrift mode", "Where the money is now"):
        assert old not in v, old


def test_alert_strip_only_with_an_owner_action(real):
    assert "Action:" not in visible(real["html"])
    day = copy.deepcopy(real["day"])
    day["owner_top"]["action"] = "Action for you: check the open alert (routines_silent) and acknowledge it"
    assert "Action: check the open alert (routines_silent)" in visible(report.render_email(day, LINK))


# ------------------------------------------------------------------ day types
def test_morning_variant(morning):
    v, t = visible(morning["html"]), morning["text"]
    assert "Mon 9 Nov · pre-open" in v and "PLAN TODAY" in v and "Loss on Fri 6 Nov" in v and "LAST CHECK FRI 15:10" in v
    assert "CPI Mon 9 Nov 19:00" in v and "Copper: block" in v and "Silver: favour" in v
    assert "−1.63% Fri" in v and ">TODAY<" not in morning["html"] and "No trades ·" not in v
    assert "PLAN TODAY" in t and morning["day"]["subject"] == "Metals Daybook · Mon 9 Nov · pre-open plan"
    assert "₹1,00,328" in v


def test_holiday_shows_only_a_b_e_f_g(root, shipped_config):
    day = report.build_day("2026-11-10", root)
    assert day.get("closed")
    h = report.render_email(day, LINK)
    v = visible(h)
    assert "market closed" in v and "NSE closed: Diwali" in v
    assert "Total value" in v and "WHY CASH?" in v and "SAFETY" in v and "Open dashboard" in v and "Gold " in v
    for absent in ("TODAY", "PLAN TODAY", "YOU VS GOLD", "No trades"):
        assert f">{absent}<" not in h and absent + " ·" not in v, absent
    assert "Gold ₹116.20" in v and "ATP" not in v       # closed-day prices are the last session's close, not the cost
    assert figures(v) == figures(report.render_text(day, report_url=LINK))
    assert "Today" in v and "market closed" in v


def test_all_cash_day(examples):
    day = copy.deepcopy(examples["all_cash"]["day"])
    day["decisions"], day["counts"], day["holdings_close"] = [], {}, []
    h = report.render_email(day, LINK)
    v = visible(h)
    assert "Cash 100%" in v and "idle, earns nothing" in v and "0 buys" in v and "No trades ·" in v
    assert " × ₹" not in v and "Invested ₹0" in v
    assert figures(v) == figures(report.render_text(day, report_url=LINK))


def test_loss_day(examples):
    c = examples["loss"]
    ms = c["day"]["money_summary"]
    assert ms["overall_profit"] < 0 and "Overall loss" in c["html"] and "↓ " + report.sg(ms["overall_profit"]) in c["html"]
    assert report.ON_DARK["down"] in c["html"]


def test_long_ledger_keeps_the_timeline_short(examples):
    day = copy.deepcopy(examples["three_holdings"]["day"])
    day["decisions"] = [{"time": f"10:{i:02d}", "action": "BUY" if i % 2 else "SELL", "symbol": "GOLDBEES", "qty": 10 + i, "price": 117.2 + i,
                         "amount": (10 + i) * (117.2 + i), "pnl": 5.0 if i % 2 == 0 else None, "reason": "x", "trigger": "t"} for i in range(14)]
    h = report.render_email(day, LINK)
    v = visible(h)
    assert len(re.findall(r"10:\d\d ", v)) == 5 and "+9 earlier" in v and len(h.encode()) <= 60_000
    assert "units × ₹" in v


def test_timeline_is_chronological(real):
    day = copy.deepcopy(real["day"])
    day["decisions"].insert(0, {"time": "09:15", "action": "BLOCK", "symbol": "GOLDBEES", "qty": 0, "price": 117.0, "amount": 0, "pnl": None,
                                "reason": "GOLDBEES is -2.60% from yesterday's close.", "trigger": "Price shock"})
    v = visible(report.render_email(day, LINK))
    assert v.index("09:15") < v.index("No trades ·") < v.index("15:26")


# ------------------------------------------------------------------ plumbing
def test_send_report_morning_kind_and_backup_banner(root, tmp_path, monkeypatch, shipped_config):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import send_report
    import backup_email
    r2 = tmp_path / "r"
    shutil.copytree(root, r2)
    monkeypatch.setenv("REPORT_EMAIL", "owner@example.com")
    out = send_report.make("2026-11-09", r2, LINK, kind="morning")
    assert out["subject"].startswith("Metals Daybook") and "pre-open" in out["subject"]
    assert out["html_path"].endswith("2026-11-09-morning.email.html") and Path(out["html_path"]).exists() and Path(out["text_path"]).exists()
    ev = send_report.make(DAY, r2, LINK)
    assert ev["html_path"].endswith("2026-11-06.email.html") and "WHY CASH?" in Path(ev["html_path"]).read_text(encoding="utf-8")
    msg = backup_email.build_message(ev["subject"], "a@example.com", "owner@example.com", Path(ev["text_path"]).read_text(encoding="utf-8"),
                                     Path(ev["html_path"]).read_text(encoding="utf-8"), "summary here")
    body = msg.get_body(("html",)).get_content()
    assert body.index("Backup email") < body.index("Total value") and "WHY CASH?" in body


# ------------------------------------------------------------------ phone width
def _browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"chromium not available: {e}")
    return pw, b


def test_no_horizontal_overflow_at_390px(real, morning, examples, tmp_path):
    pw, b = _browser()
    try:
        for name, c in all_cases(real, morning, examples):
            f = tmp_path / f"{name}.html"
            f.write_text(c["html"], encoding="utf-8")
            pg = b.new_page(viewport={"width": 390, "height": 800})
            pg.goto(f.as_uri())
            sw = pg.evaluate("document.documentElement.scrollWidth")
            assert sw <= 390, (name, sw)
            pg.close()
    finally:
        b.close()
        pw.stop()


# ------------------------------------------------------------------ crash safety: an email always goes out
BAD = {"level_kind": "sma200", "level": "n/a", "close": "x", "gap_pct": "?", "weight_if_on": "n/a"}


def test_malformed_target_record_still_gives_an_email(root, tmp_path, shipped_config):
    r = tmp_path / "bad"
    shutil.copytree(root, r)
    f = r / "logs" / "decisions-2026-11-06.jsonl"
    rec = {"ts": "2026-11-06T15:11:00+05:30", "time": "15:11", "action": "TARGET", "symbol": "GOLDBEES", "qty": 0, "price": 116.13, "amount": 0.0,
           "pnl": None, "reason": "x", "trigger": "Trend check", "state": "OFF", "target_pct": 50.0, "actual_pct": 50.6, **BAD}
    f.write_text(f.read_text(encoding="utf-8") + json.dumps(rec) + "\n", encoding="utf-8")
    day = report.build_day(DAY, r)
    h, t = report.render_email(day, LINK), report.render_text(day, report_url=LINK)
    assert "WHY CASH" in h and "₹1,00,328" in visible(h) and "₹1,00,328" in t        # still the compact email, the bad numbers are just missing
    mday = report.build_morning("2026-11-09", r)
    assert "PLAN TODAY" in visible(report.render_email(mday, LINK))


def test_malformed_target_dicts_in_the_day(real, morning):
    for base in (real, morning):
        day = copy.deepcopy(base["day"])
        day["targets"] = [{**t, **BAD} for t in day["targets"]] + [{"symbol": "GOLDBEES", "state": None}, {"symbol": "SILVERBEES", "level": object()}]
        assert "₹1,00,328" in visible(report.render_email(day, LINK))
        assert "₹1,00,328" in report.render_text(day, report_url=LINK)


def test_compact_failure_falls_back_to_ab(real, morning, monkeypatch, capsys):
    def boom(*a, **k):
        raise ValueError("could not convert string to float: 'n/a'")
    monkeypatch.setattr(report, "compact_model", boom)
    for base, expect in ((real, "₹1,00,328"), (morning, "₹1,00,328")):
        h, t = report.render_email(base["day"], LINK), report.render_text(base["day"], report_url=LINK)
        assert report.FUNDS_TITLE in h and "WHY CASH" not in h and expect in h and expect in t, base["day"]["date"]
    assert "compact html render failed" in capsys.readouterr().err


# ------------------------------------------------------------------ unrecorded session: "since" wording
GAP_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def gap13(shipped_config):
    if not any(ln.startswith("2026-11-13") for ln in (GAP_ROOT / "state" / "equity.csv").read_text(encoding="utf-8").splitlines()):
        pytest.skip("13 Nov is not in this checkout's equity.csv")
    day = report.build_day("2026-11-13", GAP_ROOT)
    return {"day": day, "html": report.render_email(day, LINK), "text": report.render_text(day, report_url=LINK)}


def test_unrecorded_session_relabels_today(gap13):
    day, h, t = gap13["day"], visible(gap13["html"]), gap13["text"]
    assert day["money_summary"]["prev_close_date"] == "2026-11-11"
    assert day["today_gap"] == {"since": "2026-11-11", "missing": ["2026-11-12"]}
    for x in (h, t):
        assert "since Wed 11 Nov" in x and "2 sessions · 12 Nov not recorded" in x
        assert "Today's loss" not in x and "Today's gain" not in x
    assert "since Wed 11 Nov" in day["subject"] and "Thu 12" not in day["subject"]
    assert day["subject"].startswith("Metals Daybook · ") and "Fri 13 Nov" in day["subject"]


def test_normal_day_keeps_today_wording(real):
    assert "Today's loss" in visible(real["html"]) and "not recorded" not in visible(real["html"]) and "since" not in real["day"]["subject"]
    assert "today_gap" not in real["day"]


def test_gap_in_the_morning_cell(gap13):
    mday = report.build_morning("2026-11-16", GAP_ROOT)
    assert mday["today_gap"]["since"] == "2026-11-11"
    v = visible(report.render_email(mday, LINK))
    assert "since Wed 11 Nov" in v and "not recorded" in v and "on Fri 13 Nov" not in v


# ------------------------------------------------------------------ one rounding rule for "pts ahead"
def test_pts_gap_rounds_once_half_away_from_zero():
    assert report.pts_gap(6.0, 0.05) == 6.0          # exactly 5.95 -> 6.0, not the double-rounded 5.9 / 6.0 split
    assert report.pts_gap(0.05, 6.0) == -6.0
    assert report.pts_gap(1.04, 0.0) == 1.0 and report.pts_gap(1.26, 0.0) == 1.3
    # the old double rounding disagrees: round(6.04,1) - round(0.09,1) = 5.9 vs the exact 5.95
    assert round(round(6.04, 1) - round(0.09, 1), 1) == 6.0 - 0.1 and report.pts_gap(6.04, 0.09) == 6.0


def test_vs_gold_and_owner_top_say_the_same_number(real, root):
    chip = real["day"]["vs_gold"]["diff"]
    line = real["day"]["owner_top"]["vs_hold"]
    m = re.search(r"\(([\d.]+) pts (ahead|behind)\)", line)
    assert m and (float(m.group(1)) * (1 if m.group(2) == "ahead" else -1)) == chip
    assert f"{abs(chip):.1f} pts" in visible(real["html"])


# ------------------------------------------------------------------ cosmetics of the approved image
def test_navy_card_keeps_its_rounded_corners(real):
    h = real["html"]
    assert "border-radius:14px;overflow:hidden" in h
    assert "border-radius:14px 14px 0 0" in h and "border-radius:0 0 14px 14px" in h      # first and last inner cells carry the radius too


def test_timeline_dots_are_round_8px_and_verb_is_bold(real):
    h = real["html"]
    assert '<table role="presentation" width="8" cellpadding="0" cellspacing="0" style="width:8px;">' in h
    assert re.search(r'<b style="color:#a85a00;">blocked</b> GOLDBEES', h)


# ------------------------------------------------------------------ morning variant
def test_monday_morning_uses_the_weekday_not_yesterday(morning):
    v = visible(morning["html"])
    assert "Fri" in morning["day"]["date"] or morning["day"]["date"] == "2026-11-06"
    assert "yesterday" not in v and "yesterday" not in morning["text"]
    assert "−1.63% Fri" in v and "−1.63% Fri" in morning["text"]


def test_tuesday_morning_still_says_yesterday(root, shipped_config):
    day = report.build_morning("2026-11-09", root)
    day["morning_date"] = "2026-11-07"        # data day 6 Nov, shown on 7 Nov: the day before
    assert "yesterday" in visible(report.render_email(day, LINK))


def test_stale_guidance_biases_show_as_stale(root, tmp_path, shipped_config):
    r = tmp_path / "stale"
    shutil.copytree(root, r)
    g = {"updated": "2026-11-05T14:00:00+05:30", "bias": {"GOLD": "block", "SILVER": "block", "COPPER": "favour"}}
    (r / "state" / "guidance.json").write_text(json.dumps(g), encoding="utf-8")
    day = report.build_morning("2026-11-09", r)
    v = visible(report.render_email(day, LINK))
    assert "Gold: neutral (stale)" in v and "Copper: neutral (stale)" in v and "block" not in v.split("PLAN TODAY")[1].split("Every trade")[0]
    g["updated"] = "2026-11-06T14:00:00+05:30"
    (r / "state" / "guidance.json").write_text(json.dumps(g), encoding="utf-8")
    v = visible(report.render_email(report.build_morning("2026-11-09", r), LINK))
    assert "Gold: block" in v and "Copper: favour" in v and "stale" not in v


# ------------------------------------------------------------------ HTML vs text, field by field
def _labelled_html(m):
    """Each labelled value of the broker card, read back from the HTML (label cell, then the value in the same cell)."""
    out = {}
    for label in ("Total value", "Invested", "Cash", "Booked"):
        mm = re.search(rf">{label}</div><div[^>]*>(.*?)</div>", m["html"])
        out[label] = re.sub(r"<[^>]+>", "", htmllib.unescape(mm.group(1))).strip() if mm else None
    for key, label in (("Overall", "Overall (?:gain|loss)"), ("Today", r"(?:Today's (?:gain|loss)|(?:Gain|Loss) (?:on|since) [^<]+)")):
        mm = re.search(rf">{label}</div><div[^>]*>(.*?)</div>", m["html"])
        if mm:
            out[key] = re.sub(r"<[^>]+>", "", htmllib.unescape(mm.group(1))).strip()
    return out


def _labelled_text(t):
    out = {}
    out["Total value"] = re.search(r"^Total value (\S+)", t, re.M).group(1)
    mo = re.search(r"^Overall (?:gain|loss) (.+?)   \|   (?:Today's (?:gain|loss)|Gain on|Loss on|Gain since|Loss since)[^\n]*? ([↑↓→] [+−]?₹[\d,]+ \([^)]*\))$", t, re.M)
    mi = re.search(r"^Invested (\S+)   Cash (\S+)   Booked ([^\n]+)$", t, re.M)
    out["Invested"], out["Cash"], out["Booked"] = mi.groups()
    out["Overall"], out["Today"] = mo.groups() if mo else (None, None)
    return out


def test_each_labelled_field_matches_its_own_value(real, morning, examples):
    for name, c in all_cases(real, morning, examples):
        h, t = _labelled_html(c), _labelled_text(c["text"])
        h = {k: re.sub(r"\s+", "", v or "") for k, v in h.items()}
        t = {k: re.sub(r"\s+", "", v or "") for k, v in t.items()}
        for k in ("Total value", "Invested", "Cash", "Booked", "Overall", "Today"):
            assert h[k] and h[k] == t[k], (name, k, h[k], t[k])
        # and the values are the right ones, not merely equal to each other
        ms = c["day"]["money_summary"]
        assert t["Total value"] == report.inr(ms["total_now"]).replace(" ", "")
        assert t["Cash"] == report.inr(ms["cash"]) and t["Invested"] == report.inr(ms["funds"]["total"]["invested"])
        assert t["Cash"] != t["Invested"] or ms["cash"] == ms["funds"]["total"]["invested"]


def test_swapped_cash_and_invested_in_text_is_caught(real):
    t = real["text"].replace("Invested ₹49,406   Cash ₹49,574", "Invested ₹49,406   Cash ₹49,406")
    assert t != real["text"] and _labelled_text(t)["Cash"] != _labelled_html(real)["Cash"]


# ------------------------------------------------------------------ fixture examples are internally consistent
def test_examples_are_internally_consistent(examples):
    for name, c in examples.items():
        d, ms = c["day"], c["day"]["money_summary"]
        bd, p = ms["breakdown"], d["portfolio"]
        assert abs(ms["total_now"] - (sum(m["value"] for m in ms["metals"]) + ms["cash"])) <= 1.0, name
        assert abs(ms["overall_profit"] - (bd["open"] + bd["booked"] + bd["interest"])) <= 0.01, name
        assert abs(p["close"] - ms["total_now"]) < 0.01 and abs(p["day_pnl"] - ms["today_profit"]) <= 0.02, name
        assert abs(d["safety"]["peak"] - max(100000, ms["total_now"], p["open"])) < 0.01, name
        held = {r["symbol"] for r in ms["funds"]["rows"] if r["kind"] == "fund" and r.get("held")}
        for t in d["targets"]:
            assert (t["state"] == "ON") == (t["symbol"] in held), (name, t["symbol"])
            if t["symbol"] in held:
                assert abs(t["close"] - next(r["price"] for r in ms["funds"]["rows"] if r["symbol"] == t["symbol"])) < 0.01, name
        todays = [x for x in d["decisions"] if x["action"] in ("BUY", "SELL")]
        assert d["counts"]["buys"] == sum(x["action"] == "BUY" for x in todays) and d["counts"]["sells"] == sum(x["action"] == "SELL" for x in todays), name
        if name == "all_cash":
            assert not held and not todays and ms["funds"]["total"]["invested"] == 0
            assert not any(t["state"] == "ON" for t in d["targets"])
        if d.get("vs_gold"):
            assert abs(d["vs_gold"]["you"] - (ms["total_now"] / 100000 - 1) * 100) < 0.01, name
        assert c["day"].get("next_event", "") >= "" and "14 Oct" not in d.get("next_event", ""), name


# ------------------------------------------------------------------ holding rows and the Cash row, each against its own value
def _rows_html(c):
    v = visible(c["html"])
    out = {"holdings": {}, "cash": None}
    for m in re.finditer(r"([A-Z]{6,10}) ([\d,]+) × (₹[\d,.]+)(?: · stop ₹[\d,.]+)? ([↑↓→] [+−]?₹[\d,]+ \([^)]*\)) LTP (₹[\d,.]+)", v):
        out["holdings"][m.group(1)] = {"qty": m.group(2), "atp": m.group(3), "pnl": m.group(4), "ltp": m.group(5)}
    for m in re.finditer(r"([\d,]+) shares × (₹[\d,.]+) = (₹[\d,]+) · (₹[\d,]+) after selling cost", v):
        for h in out["holdings"].values():
            if h["qty"] == m.group(1) and "worth" not in h:
                h["worth"] = m.groups()
                break
    m = re.search(r"Cash (idle[^₹]*?) (₹[\d,]+) (?:WHY|PLAN|TODAY|YOU|SAFETY)", v)
    out["cash"] = (m.group(2), m.group(1)) if m else None
    return out


def _rows_text(c):
    t = c["text"]
    out = {"holdings": {}, "cash": None}
    for m in re.finditer(r"^([A-Z]{6,10}) ([\d,]+) × (₹[\d,.]+)(?: · stop ₹[\d,.]+)?: ([↑↓→] [+−]?₹[\d,]+ \([^)]*\)) · LTP (₹[\d,.]+).*\n  ([\d,]+) shares × (₹[\d,.]+) = (₹[\d,]+) · (₹[\d,]+) after selling cost$", t, re.M):
        out["holdings"][m.group(1)] = {"qty": m.group(2), "atp": m.group(3), "pnl": m.group(4), "ltp": m.group(5), "worth": m.groups()[5:]}
    m = re.search(r"^Cash (₹[\d,]+) \((idle[^)]*)\)$", t, re.M)
    out["cash"] = (m.group(1), m.group(2)) if m else None
    return out


def test_holding_rows_and_cash_row_match_their_own_values(real, morning, examples):
    for name, c in all_cases(real, morning, examples):
        ms = c["day"]["money_summary"]
        held = [r for r in ms["funds"]["rows"] if r["kind"] == "fund" and r.get("held")]
        h, t = _rows_html(c), _rows_text(c)
        assert set(h["holdings"]) == set(t["holdings"]) == {r["symbol"] for r in held}, name
        for r in held:
            for src in (h, t):
                row = src["holdings"][r["symbol"]]
                assert row["qty"] == f"{int(r['units']):,}" and row["atp"] == report.inr(r["avg_price"], 2) and row["ltp"] == report.inr(r["price"], 2), (name, r["symbol"])
                assert row["pnl"].replace(" ", "").startswith(("↑" if round(r["profit"]) > 0 else "↓" if round(r["profit"]) < 0 else "→") + report.sg(r["profit"])), (name, row["pnl"])
                assert tuple(row["worth"])[-3:] == (report.inr(r["price"], 2), report.inr(r.get("gross_value", r["value"])), report.inr(r["value"])), (name, row["worth"])
                assert row["worth"][-4] == f"{int(r['units']):,}"
            assert h["holdings"][r["symbol"]]["pnl"] == t["holdings"][r["symbol"]]["pnl"]
        cash = next(r for r in ms["funds"]["rows"] if r["kind"] == "cash")
        assert h["cash"] and t["cash"], name
        assert h["cash"][0] == t["cash"][0] == report.inr(cash["value"]) == report.inr(ms["cash"]), name
        assert h["cash"][1].startswith("idle") and t["cash"][1].startswith("idle"), name


def test_text_cash_row_printing_the_invested_amount_fails(real):
    ms = real["day"]["money_summary"]
    inv, cash = report.inr(ms["funds"]["total"]["invested"]), report.inr(ms["cash"])
    assert inv != cash
    bad = {**real, "text": real["text"].replace(f"Cash {cash} (idle", f"Cash {inv} (idle")}
    assert bad["text"] != real["text"]
    assert _rows_text(bad)["cash"][0] == inv != _rows_html(real)["cash"][0]


def test_wrong_holding_value_in_text_fails(real):
    t = real["text"].replace("₹50,754 after selling cost", "₹50,779 after selling cost")
    assert t != real["text"]
    assert _rows_text({"text": t})["holdings"]["GOLDBEES"]["worth"][-1] != _rows_html(real)["holdings"]["GOLDBEES"]["worth"][-1]


# ------------------------------------------------------------------ exact .05 gap: one figure everywhere (13 Nov style rounding)
def test_pts_gap_exact_half_is_the_same_figure_on_every_surface(real, root, monkeypatch):
    monkeypatch.setattr(report, "_first_gold_price", lambda *a, **k: 100.0)
    monkeypatch.setattr(report, "_latest_gold_price", lambda *a, **k: 100.09)      # gold +0.09%
    day = copy.deepcopy(real["day"])
    day["portfolio"]["close"] = 106040.0                                          # account +6.04% -> exact gap 5.95
    day["vs_gold"] = report._vs_gold(DAY, root, day)
    day["owner_top"] = report.owner_top(DAY, root, day)
    assert day["vs_gold"]["diff"] == 6.0
    assert round(round(6.04, 1) - round(0.09, 1), 1) == 5.9           # the old double rounding said 5.9 here
    h, t = visible(report.render_email(day, LINK)), report.render_text(day, report_url=LINK)
    assert "6.0 pts ahead" in h and "6.0 pts ahead" in t and "5.9 pts" not in h + t
    assert "(6.0 pts ahead)" in day["owner_top"]["vs_hold"]
    # and a behind case, half away from zero
    day["portfolio"]["close"] = 93960.0
    monkeypatch.setattr(report, "_latest_gold_price", lambda *a, **k: 99.91)
    day["vs_gold"], day["owner_top"] = report._vs_gold(DAY, root, day), report.owner_top(DAY, root, day)
    assert "6.0 pts behind" in visible(report.render_email(day, LINK)) and "(6.0 pts behind)" in day["owner_top"]["vs_hold"]


# ------------------------------------------------------------------ morning fallback is labelled
def test_morning_fallback_body_says_it_is_the_last_close(morning, monkeypatch):
    monkeypatch.setattr(report, "compact_model", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    day = morning["day"]
    h, t = visible(report.render_email(day, LINK)), report.render_text(day, report_url=LINK)
    for x in (h, t):
        assert "Pre-open plan for Mon 9 Nov" in x and "last close, Fri 6 Nov" in x and "CPI Mon 9 Nov 19:00" in x and "Silver: favour" in x
    assert day["subject"] == "Metals Daybook · Mon 9 Nov · pre-open plan"
