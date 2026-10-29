"""Tests for the end-of-day report generator (fixtures live in a tmp dir)."""
import html
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from simgold import report

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "daily_report_template.html"

GREEN_DAY = "2026-10-12"   # Monday
RED_DAY = "2026-10-13"     # Tuesday

REASONS = {
    "buy": "Dollar fell & gold rose; price is above its 20-day average.",
    "skip": "Signal not strong enough (0.55, needs 0.65), and only ₹101 of today's risk budget was left.",
    "sell": "Hit first target (+0.9%); sold half to lock profit.",
    "sell2": "Rally faded as the dollar bounced <exit early>.",
}


def row(time, action, symbol, qty, price, amount, pnl, reason, trigger, stop=None, risk=None, value_after=100000, floor=99500):
    return {"ts": f"2026-10-12 {time}:00", "time": time, "action": action, "symbol": symbol, "qty": qty,
            "price": price, "amount": amount, "pnl": pnl, "reason": reason, "trigger": trigger,
            "stop": stop, "risk": risk, "value_after": value_after, "floor": floor}


def make_root(tmp_path, date, decisions, ticks, portfolio, equity, extras=None):
    for sub in ("logs", "state"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if decisions is not None:
        (tmp_path / "logs" / f"decisions-{date}.jsonl").write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in decisions) + "\n", encoding="utf-8")
    if ticks is not None:
        lines = ["time,value,floor,cash,liquidbees"] + [",".join(str(x) for x in t) for t in ticks]
        (tmp_path / "logs" / f"ticks-{date}.csv").write_text("\n".join(lines) + "\n")
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps(portfolio))
    (tmp_path / "state" / "equity.csv").write_text("\n".join(f"{d},{v}" for d, v in equity) + "\n")
    for name, content in (extras or {}).items():
        (tmp_path / "state" / name).write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture
def green_root(tmp_path):
    decisions = [
        row("09:52", "BUY", "GOLDBEES", 190, 104.2, 19798, None, REASONS["buy"], "Pre-market plan", 102.1, 399),
        row("11:05", "SKIP", "SILVERBEES", 0, 141.9, 0, None, REASONS["skip"], "Minute check"),
        row("12:40", "SELL", "GOLDBEES", 95, 105.1, 9984.5, 85.5, REASONS["sell"], "Target rule"),
        row("13:18", "BUY", "SILVERBEES", 40, 142.5, 5700, None, "Fast lane: jobless claims jumped.", "News trigger", 139.65, 114),
        row("14:46", "SELL", "SILVERBEES", 40, 141.4, 5656, -44, REASONS["sell2"], "Opus review"),
        row("15:20", "HOLD", "GOLDBEES", 95, 105.4, 0, None, "Overnight check passed.", "Overnight check"),
    ]
    ticks = [("09:15", 100000, 99500, 100000, 0), ("10:15", 100058, 99500, 90000, 0),
             ("12:45", 100131, 99500, 90100, 0), ("15:30", 100147, 99500, 90134, 0)]
    pf = {"cash": 90134, "positions": {"GOLDBEES": {"qty": 95, "avg": 104.2, "stop": 104.2}},
          "floor": 99500, "best_profit": 147, "month": "2026-10", "month_start_equity": 100000,
          "realised_pnl": 41.5, "total_costs": 26, "liquidbees": 0,
          "day_costs": 26, "liquidbees_income_today": 17, "day_costs_date": GREEN_DAY}
    return make_root(tmp_path, GREEN_DAY, decisions, ticks, pf, [("2026-10-09", 100000.0)])


@pytest.fixture
def red_root(tmp_path):
    decisions = [
        row("10:05", "BUY", "SILVERBEES", 100, 140.0, 14000, None, "Silver momentum turned up.", "Minute check", 137.0, 300),
        row("13:30", "SELL", "SILVERBEES", 100, 137.0, 13700, -300, "Stop was hit as silver fell with the dollar.", "Stop"),
        row("14:00", "BLOCK", "GOLDBEES", 0, 105.0, 0, None, "Buy blocked: cushion above the floor is too small.", "Guard"),
    ]
    ticks = [("09:15", 100200, 99500, 100200, 0), ("13:30", 99900, 99500, 99900, 0), ("15:30", 99890, 99500, 99890, 0)]
    pf = {"cash": 99890, "positions": {}, "floor": 99500, "best_profit": 200, "month": "2026-10",
          "month_start_equity": 100000, "liquidbees": 0, "day_costs": 20, "liquidbees_income_today": 10,
          "day_costs_date": RED_DAY}
    return make_root(tmp_path, RED_DAY, decisions, ticks, pf, [("2026-10-12", 100200.0)])


def test_inr_formatting():
    assert report.inr(100147) == "₹1,00,147"
    assert report.inr(1234567) == "₹12,34,567"
    assert report.inr(104.2, 2) == "₹104.20"
    assert report.inr(999) == "₹999"
    assert report.sg(-44) == "−₹44"
    assert report.sg(85.5) == "+₹86"
    assert report.sg(0.2) == "₹0"


def test_totals_reconcile(green_root):
    day = report.build_day(GREEN_DAY, green_root)
    b, p = day["pnl_breakdown"], day["portfolio"]
    assert p["day_pnl"] == pytest.approx(147)
    assert sum(b.values()) == pytest.approx(p["day_pnl"], abs=1.0)
    assert b["realized_trades"] == pytest.approx(41.5)
    # pnl on SELL rows is already net of costs: costs are shown as an info line, never subtracted again
    assert "costs" not in b and p["costs_paid"] == 26
    assert b["unrealized_open"] == pytest.approx(147 - 41.5 - 17, abs=1) and b["liquidbees_income"] == 17
    assert p["distance_to_floor"] == pytest.approx(647)
    assert day["counts"] == {"buys": 2, "sells": 2, "skips": 1, "blocks": 0, "holds": 1, "fast_lane_runs": 1}


def test_totals_reconcile_when_figures_are_estimated(tmp_path):
    """No logged costs or income: estimates are used and the breakdown still adds up."""
    decisions = [row("10:00", "BUY", "GOLDBEES", 100, 100.0, 10000, None, "Test buy.", "Plan", 98, 200),
                 row("12:00", "SELL", "GOLDBEES", 100, 101.0, 10100, 100, "Test sell.", "Target")]
    ticks = [("09:15", 100000, 99500, 100000, 50000), ("15:30", 100060, 99500, 100060, 50000)]
    root = make_root(tmp_path, GREEN_DAY, decisions, ticks, {"positions": {}, "floor": 99500}, [("2026-10-09", 100000)])
    day = report.build_day(GREEN_DAY, root)
    assert day["costs_estimated"] and day["portfolio"]["costs_paid"] > 0
    assert sum(day["pnl_breakdown"].values()) == pytest.approx(day["portfolio"]["day_pnl"], abs=1.0)


def test_html_contains_every_reason(green_root, tmp_path):
    day = report.build_day(GREEN_DAY, green_root)
    paths = report.write_reports(day, tmp_path / "out", GREEN_DAY, TEMPLATE)
    email = paths["email"].read_text(encoding="utf-8")
    page = paths["page"].read_text(encoding="utf-8")
    text = paths["txt"].read_text(encoding="utf-8")
    for d in day["decisions"]:
        assert html.escape(d["reason"], quote=False) in email
        assert d["reason"] in text
    data = json.loads(re.search(r"const DATA = (\{.*\});", page).group(1).replace("<\\/", "</"))
    assert [d["reason"] for d in data["decisions"]] == [d["reason"] for d in day["decisions"]]
    assert "SAMPLE" not in email and "_note" not in data
    assert "<script>" not in json.dumps(data) and "__DATA__" not in page


def test_subject_green_and_red(green_root, red_root):
    g = report.build_day(GREEN_DAY, green_root)
    assert report.subject_line(g) == "Metals Daybook · 📈 Mon 12 Oct · +₹147 (green)"
    r = report.build_day(RED_DAY, red_root)
    assert r["portfolio"]["day_pnl"] == pytest.approx(-310)
    assert report.subject_line(r) == "Metals Daybook · 📉 Tue 13 Oct · −₹310 (red)"


def test_red_day_wording_and_reasons(red_root, tmp_path):
    day = report.build_day(RED_DAY, red_root)
    paths = report.write_reports(day, tmp_path / "out", RED_DAY, TEMPLATE)
    email = paths["email"].read_text(encoding="utf-8")
    assert "Today's loss: −₹310.00" in email and "#c0392b" in email and "since the start" not in email
    assert "You're in the red" not in email
    assert "Where today's ₹310 loss came from" in email
    for d in day["decisions"]:
        assert html.escape(d["reason"], quote=False) in email
    assert sum(day["pnl_breakdown"].values()) == pytest.approx(day["portfolio"]["day_pnl"], abs=1.0)


def test_no_trades_uses_skip_reasons(tmp_path):
    decisions = [row("11:05", "SKIP", "SILVERBEES", 0, 141.9, 0, None, "Signal not strong enough.", "Minute check"),
                 row("12:00", "BLOCK", "GOLDBEES", 0, 105.0, 0, None, "Guard says the cushion is too small.", "Guard")]
    ticks = [("09:15", 100000, 99500, 100000, 100000), ("15:30", 100017, 99500, 100017, 100000)]
    root = make_root(tmp_path, GREEN_DAY, decisions, ticks, {"positions": {}, "floor": 99500, "liquidbees_income_today": 17, "day_costs": 0}, [("2026-10-09", 100000)])
    day = report.build_day(GREEN_DAY, root)
    paths = report.write_reports(day, tmp_path / "out", GREEN_DAY, TEMPLATE)
    email = paths["email"].read_text(encoding="utf-8")
    assert "No trades today. The system waited because signal not strong enough; guard says the cushion is too small." in email
    assert "Signal not strong enough." in email and "Guard says the cushion is too small." in email
    assert day["portfolio"]["day_pnl"] == 17
    assert "No positions held overnight." in email


def test_no_trades_and_no_logs_at_all(tmp_path):
    root = make_root(tmp_path, GREEN_DAY, None, None, {"positions": {}, "floor": 99500}, [("2026-10-09", 100000)])
    day = report.build_day(GREEN_DAY, root)
    assert day["decisions"] == [] and len(day["intraday"]) >= 2
    paths = report.write_reports(day, tmp_path / "out", GREEN_DAY, TEMPLATE)
    email = paths["email"].read_text(encoding="utf-8")
    assert "No trades today." in email and "No decision log was found" in email


def test_missing_ticks_file(green_root, tmp_path):
    (green_root / "logs" / f"ticks-{GREEN_DAY}.csv").unlink()
    day = report.build_day(GREEN_DAY, green_root)
    assert any("ticks" in w for w in day["warnings"])
    assert len(day["intraday"]) >= 2
    email = report.render_email(day)
    assert "ticks file was missing" in email
    for d in day["decisions"]:
        assert html.escape(d["reason"], quote=False) in email


def test_market_holiday_short_email(tmp_path):
    root = make_root(tmp_path, "2026-10-02", None, None, {"positions": {}, "floor": 99500}, [("2026-10-01", 100300)])
    day = report.build_day("2026-10-02", root)
    assert day["closed"] and "Gandhi Jayanti" in day["one_line_summary"]
    paths = report.write_reports(day, tmp_path / "out", "2026-10-02", TEMPLATE)
    email = paths["email"].read_text(encoding="utf-8")
    assert "Market closed" in email and "What I did" not in email and len(email) < 30000   # incl. the money-at-a-glance block and the 'Where your money is' section
    assert report.subject_line(day).endswith("Market closed")
    # weekends are closed too, and --holiday forces it
    assert report.build_day("2026-10-10", root)["closed"]
    assert report.build_day("2026-10-12", root, holiday="Test holiday")["closed"]


def test_market_section_and_neutral_rows(green_root, tmp_path):
    (green_root / "state" / "market_today.json").write_text(json.dumps([
        {"name": "Gold (global)", "move_pct": 0.8, "note": "Weaker dollar"},
        {"name": "Dollar index", "move_pct": -0.2, "note": "Lower dollar"}]))
    day = report.build_day(GREEN_DAY, green_root)
    email = report.render_email(day)
    assert "▲ +0.8%" in email and "▼ −0.2%" in email
    # dollar index is neutral grey, not red
    seg = email[email.index("Dollar index"):]
    assert "color:#6b7280;white-space:nowrap" in seg[:900] and "#c0392b" not in seg[:900]


def test_cli_sample_and_date(green_root):
    out = subprocess.run([sys.executable, str(REPO / "run_report.py"), "--date", GREEN_DAY, "--root", str(green_root),
                          "--report-url", "https://example.com/r"], capture_output=True, text=True, encoding="utf-8", check=True)
    assert out.stdout.strip() == "Metals Daybook · 📈 Mon 12 Oct · +₹147 (green)"
    daily = green_root / "reports" / "daily"
    for suffix in ("json", "email.html", "txt", "page.html"):
        assert (daily / f"{GREEN_DAY}.{suffix}").exists()
    assert "https://example.com/r" in (daily / f"{GREEN_DAY}.email.html").read_text(encoding="utf-8")
    assert "_note" not in (daily / f"{GREEN_DAY}.json").read_text(encoding="utf-8")

    out = subprocess.run([sys.executable, str(REPO / "run_report.py"), "--sample", "--root", str(green_root)],
                         capture_output=True, text=True, encoding="utf-8", check=True)
    assert "(green)" in out.stdout
    assert "SAMPLE" in (daily / "sample.email.html").read_text(encoding="utf-8")
    assert "SAMPLE" in (daily / "sample.page.html").read_text(encoding="utf-8")


def test_sample_email_matches_approved_design():
    day = json.loads((REPO / "templates" / "sample_day.json").read_text(encoding="utf-8"))
    mine = report.render_email(report.prepare(day), sample=True)
    approved = (REPO / "templates" / "email_report.sample.html").read_text(encoding="utf-8")
    squash = lambda h: re.sub(r">\s+<", "><", h).strip()
    assert squash(mine) == squash(approved)


def test_costs_not_double_counted_and_engine_fields_preferred(tmp_path):
    decisions = [row("10:00", "BUY", "GOLDBEES", 100, 100.0, 10000, None, "Test buy.", "Plan", 98, 200),
                 row("12:00", "SELL", "GOLDBEES", 100, 101.0, 10100, 80, "Test sell.", "Target"),
                 {**row("13:00", "BLOCK", "GOLDBEES", 0, 0, 0, None, "Paused.", "Price shock"), "symbol": "ALL"}]
    ticks = [("09:15", 100000, 99500, 0, 100000), ("15:30", 100100, 99500, 0, 100100)]
    pf = {"positions": {}, "floor": 99500, "day_costs": 20, "day_costs_date": GREEN_DAY, "day_open_value": 99990,
          "liquidbees_income_today": 17}
    day = report.build_day(GREEN_DAY, make_root(tmp_path, GREEN_DAY, decisions, ticks, pf, [("2026-10-09", 100000)]))
    p, b = day["portfolio"], day["pnl_breakdown"]
    assert p["open"] == 100000 and p["day_pnl"] == pytest.approx(100)      # previous close (equity.csv), not the engine's 09:15 value
    assert b["realized_trades"] == 80 and p["costs_paid"] == 20
    assert sum(b.values()) == pytest.approx(100, abs=1.0)
    assert any(d["symbol"] == "All metals" for d in day["decisions"])
    assert "already included" in report.render_email(day)


def test_fixed_today_and_system_health_sections(tmp_path):
    import json as _json
    inc = tmp_path / "incidents"; inc.mkdir()
    (inc / "2026-10-13-feed.json").write_text(_json.dumps({"title": "Feed <down>", "actions": [
        {"kind": "safe-fix", "text": "re-ran the job"}, {"kind": "info", "text": "x"}]}))
    (inc / "2026-10-13-nofix.json").write_text(_json.dumps({"title": "No fix", "actions": [{"kind": "proposal", "text": "pr"}]}))
    (inc / "2026-10-12-old.json").write_text(_json.dumps({"title": "Old", "actions": [{"kind": "safe-fix", "text": "y"}]}))
    assert report.fixed_today("2026-10-13", tmp_path) == ["Feed <down>: re-ran the job"]
    assert report.fixed_today("2026-10-14", tmp_path) == []
    logs = tmp_path / "logs"; logs.mkdir()
    green = _json.dumps({"results": [], "all_green": True})
    (logs / "health-2026-10-13.jsonl").write_text("\n".join([green] * 38) + "\n")
    assert report.system_health("2026-10-13", tmp_path) == "Watchdog: 38 checks today, all green"
    bad = _json.dumps({"results": [{"check": "ticks_stale"}, {"check": "guidance_stale"}], "all_green": False})
    (logs / "health-2026-10-13.jsonl").write_text(green + "\n" + bad + "\n" + bad + "\n")
    assert report.system_health("2026-10-13", tmp_path) == "Watchdog: 3 checks today, 2 problems, see incidents"
    assert report.system_health("2026-10-15", tmp_path) == ""
    day = {"fixed_today": ["Feed <down>: re-ran the job"], "system_health": "Watchdog: 3 checks today, 2 problems, see incidents"}
    h = report._ops_html(day)
    assert "Fixed today" in h and "Feed &lt;down&gt;" in h and "see incidents" in h
    assert report._ops_html({"fixed_today": [], "system_health": ""}) == ""          # hidden when empty
    assert "FIXED TODAY" in "\n".join(report._ops_text(day)) and report._ops_text({}) == []


GLANCE_DAY = "2026-10-15"


def _glance_root(tmp_path, checks=True, ticks=True):
    decisions = [{**row("10:18", "BLOCK", "SILVERBEES", 0, 195.99, 0, None, "Price shock pause.", "Price shock"), "ts": "2026-10-15T10:18:52+05:30"},
                 {**row("10:18", "BLOCK", "GOLDBEES", 0, 119.55, 0, None, "News pause.", "News trigger"), "ts": "2026-10-15T10:18:52+05:30"},
                 {**row("13:31", "BLOCK", "GOLDBEES", 0, 119.7, 0, None, "News pause.", "News trigger"), "ts": "2026-10-15T13:31:32+05:30"}]
    root = make_root(tmp_path, GLANCE_DAY, decisions, None,
                     {"positions": {}, "floor": 99500, "liquidbees": 100034.52, "liquidbees_income_today": 17.26,
                      "day_open_value": 100017.26, "day_costs": 0, "day_costs_date": GLANCE_DAY}, [("2026-10-14", 100017.26)])
    if ticks:
        rows = ["time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER",
                "10:18,100034.52,99500.00,0.00,100034.52,119.55,195.99,459.00",
                "10:19,100034.52,99500.00,0.00,100034.52,,,",
                "12:00,100034.52,99500.00,0.00,100034.52,119.20,198.10,",
                "15:29,100034.52,99500.00,0.00,100034.52,119.82,197.65,452.00"]
        (root / "logs" / f"ticks-{GLANCE_DAY}.csv").write_text("\n".join(rows) + "\n")
    if checks:
        lines = [json.dumps({"ts": f"2026-10-15T{t}:52+05:30", "time": t, "value": 100034.52}) for t in ("10:18", "10:19", "12:00", "15:29")]
        (root / "logs" / f"checks-{GLANCE_DAY}.jsonl").write_text("\n".join(lines + ["not json"]) + "\n")
    return root


def test_day_at_a_glance_counts_and_prices(tmp_path):
    day = report.build_day(GLANCE_DAY, _glance_root(tmp_path))
    g = day["glance"]
    assert g["engine"] == {"first": "10:18", "last": "15:29", "count": 4, "source": "checks", "late": True}
    assert g["decisions"]["blocked"] == 3 and g["decisions"]["blocked_by"] == {"news": 2, "price shock": 1}
    assert g["decisions"]["allowed"] == 0
    gold = next(i for i in g["instruments"] if i["symbol"] == "GOLDBEES")
    assert (gold["open"], gold["close"], gold["low"], gold["high"]) == (119.55, 119.82, 119.2, 119.82)
    assert gold["change_pct"] == pytest.approx(0.23)
    assert g["money"]["split"] == [{"name": "LIQUIDBEES (cash)", "pct": 100}]
    email = report.render_email(day, report_url="https://claude.ai/artifact/abc#2026-10-15")
    raw = email[email.index("Day at a glance"):email.index("What I did")]
    sec = re.sub(r"</?span[^>]*>", "", raw)
    assert '<span style="white-space:nowrap;">' in raw
    assert "Ran 10:18–15:29 IST · 4 minute checks (started late)" in sec
    assert "Allowed 0 (buys 0, sells 0) · Blocked 3 (news 2, price shock 1) · Skipped 0 · Held 0" in sec
    assert "₹119.55 → ₹119.82 · +0.23% · low ₹119.20 – high ₹119.82" in sec
    assert "₹459.00 → ₹452.00 · −1.53%" in sec
    assert "Value ₹1,00,034.52 · day +₹17.26 · 100% in LIQUIDBEES (cash)" in sec
    assert "No trades today." in sec
    assert 'href="https://claude.ai/artifact/abc#2026-10-15"' in raw and "open the dashboard" in sec
    assert email.index("Day at a glance") < email.index("What I did")
    assert "open the dashboard" not in report.render_email(day)          # no URL, no link line


def test_day_at_a_glance_missing_files(tmp_path):
    root = _glance_root(tmp_path, checks=False)
    g = report.build_day(GLANCE_DAY, root)["glance"]
    assert g["engine"]["source"] == "ticks" and g["engine"]["count"] == 4
    (root / "logs" / f"ticks-{GLANCE_DAY}.csv").unlink()
    day = report.build_day(GLANCE_DAY, root)
    assert "engine" not in day["glance"] and "instruments" not in day["glance"]
    assert "decisions" in day["glance"]
    email = report.render_email(day)
    assert "Day at a glance" in email and "Ran " not in email
    # no logs at all: still no exception
    (tmp_path / "e").mkdir()
    empty = make_root(tmp_path / "e", GLANCE_DAY, None, None, {"positions": {}}, [])
    assert "engine" not in report.build_day(GLANCE_DAY, empty)["glance"]
    report.render_email(report.build_day(GLANCE_DAY, empty))
    # older day dicts without a glance key render without the section
    old = dict(day)
    old.pop("glance")
    assert "Day at a glance" not in report.render_email(old)


def test_day_at_a_glance_in_text(tmp_path):
    day = report.build_day(GLANCE_DAY, _glance_root(tmp_path))
    paths = report.write_reports(day, tmp_path / "out", GLANCE_DAY, TEMPLATE, report_url="https://claude.ai/artifact/abc#2026-10-15")
    text = paths["txt"].read_text(encoding="utf-8")
    assert "DAY AT A GLANCE" in text and "Engine: Ran 10:18–15:29 IST · 4 minute checks (started late)" in text
    assert "SILVERBEES: ₹195.99 → ₹197.65" in text
    assert "open the dashboard: https://claude.ai/artifact/abc#2026-10-15" in text
    assert json.loads(paths["json"].read_text(encoding="utf-8"))["glance"]["decisions"]["blocked"] == 3


def test_glance_shortens_long_no_trade_note():
    long_note = "No trades today. The system waited because " + "; ".join(["reason number %d is quite long indeed" % i for i in range(9)]) + "."
    short = report._short_note(long_note)
    assert len(short) < len(long_note) and short.endswith("more reasons under What I did.")
    assert "reason number 0" in short and "reason number 1" not in short
    assert report._short_note("No trades today.") == "No trades today."


def _write_checks(root, rows):
    lines = [json.dumps({"ts": f"2026-10-15T{t}:52+05:30", "time": t, "value": 100034.52, "checks": c}) for t, c in rows]
    (root / "logs" / f"checks-{GLANCE_DAY}.jsonl").write_text("\n".join(lines + ["garbage"]) + "\n", encoding="utf-8")


def test_glance_best_scores_and_why_line(tmp_path):
    root = _glance_root(tmp_path)
    _write_checks(root, [
        ("10:18", {"GOLDBEES": "Price ₹119.55. Score 0.30 is below the 0.70 needed to buy (neutral bias), so no buy.",
                   "SILVERBEES": "Price ₹195.99. Score 0.20; Claude's bias for this metal is block, so no buys.",
                   "HINDCOPPER": "Score 0.30 is below the 0.80 needed to buy (neutral bias), so no buy."}),
        ("12:00", {"GOLDBEES": "Score 0.45 is below the 0.75 needed to buy (neutral bias, guidance stale), so no buy.",
                   "SILVERBEES": "Score 0.35; Claude's bias for this metal is block, so no buys.",
                   "HINDCOPPER": "No fresh price or score this minute, so no buy check."}),
    ])
    day = report.build_day(GLANCE_DAY, root)
    sc = day["glance"]["scores"]
    assert sc["GOLDBEES"] == {"best": 0.45, "needs": 0.75, "block": False, "reached": False}
    assert sc["SILVERBEES"]["block"] and sc["SILVERBEES"]["best"] == 0.35
    lines = dict(report.glance_lines(day))
    assert lines["GOLDBEES"].endswith("· best score 0.45 (needs 0.75)")
    assert lines["SILVERBEES"].endswith("· best score 0.35 (bias: block)")
    assert lines["HINDCOPPER"].endswith("· best score 0.30 (needs 0.80)")
    assert lines["Why no trades"] == ("No metal's score reached the buy level (best: gold 0.45 of 0.75, silver blocked by "
                                      "Claude's bias, copper 0.30 of 0.80); pauses from news/price shocks did not change the outcome.")
    assert "best score 0.45 (needs 0.75)" in report.render_text(day)
    assert "pauses from news/price shocks" in html.unescape(report.render_email(day))


def test_glance_why_line_keeps_note_when_a_score_reached_the_level(tmp_path):
    root = _glance_root(tmp_path)
    _write_checks(root, [("11:00", {"GOLDBEES": "Score 0.71 reached the 0.70 needed; waiting for it to hold 3 minutes (1 of 3).",
                                    "SILVERBEES": "Score 0.20 is below the 0.80 needed to buy (neutral bias), so no buy."})])
    day = report.build_day(GLANCE_DAY, root)
    assert day["glance"]["scores"]["GOLDBEES"]["reached"]
    assert day["glance"]["why_no_trades"].startswith("No trades today.")


def test_glance_no_scores_without_checks_file(tmp_path):
    day = report.build_day(GLANCE_DAY, _glance_root(tmp_path, checks=False))
    assert "scores" not in day["glance"]
    assert "best score" not in dict(report.glance_lines(day))["GOLDBEES"]
    assert day["glance"]["why_no_trades"].startswith("No trades today.")


def _story_day(open_, close, breakdown, decisions=()):
    return {"start_capital": 100000, "decisions": list(decisions), "pnl_breakdown": breakdown,
            "portfolio": {"open": open_, "close": close, "day_pnl": round(close - open_, 2)}}


def test_pnl_story_is_that_days_profit_only():
    s = report.pnl_story(_story_day(100017.26, 100034.52, {"realized_trades": 0, "unrealized_open": 0, "liquidbees_income": 17.26}))
    assert s["headline"] == "Today's profit: +₹17.26"
    assert s["source"] == "Today's +₹17 is only LIQUIDBEES interest on idle cash — no trades were made."
    assert set(s) == {"pnl", "headline", "source"}
    trades = [{"action": "BUY"}, {"action": "SELL"}]
    s = report.pnl_story(_story_day(100000, 100147, {"realized_trades": 41.5, "unrealized_open": 88.5, "liquidbees_income": 17}, trades))
    assert s["source"] == "Today's +₹147: +₹42 from closed trades, +₹89 from open positions, +₹17 LIQUIDBEES interest."
    s = report.pnl_story(_story_day(100035, 100018, {"realized_trades": -17, "unrealized_open": 0, "liquidbees_income": 0}, trades))
    assert s["headline"] == "Today's loss: −₹17.00"
    s = report.pnl_story(_story_day(100035, 100035, {"realized_trades": 0, "unrealized_open": 0, "liquidbees_income": 0}))
    assert s["headline"] == "Today: no change (₹0)"
    assert s["source"] == "No change today: no trades were made."


def test_three_days_of_plus_17_never_add_up(tmp_path):
    for open_ in (100000.0, 100017.0, 100034.0):          # each day opens at the previous day's close
        s = report.pnl_story(_story_day(open_, open_ + 17, {"realized_trades": 0, "unrealized_open": 0, "liquidbees_income": 17}))
        assert s["headline"] == "Today's profit: +₹17.00"
        assert "₹35" not in s["headline"] + s["source"] and "₹52" not in s["headline"] + s["source"]


def test_email_and_text_show_only_the_days_profit(tmp_path):
    day = report.build_day(GLANCE_DAY, _glance_root(tmp_path))
    assert day["pnl_story"]["headline"] == "Today's profit: +₹17.26"
    assert day["portfolio"]["day_pnl"] == pytest.approx(day["portfolio"]["close"] - day["portfolio"]["open"])
    email = report.render_email(day)
    text = report.render_text(day)
    assert "Today's profit: +₹17.26" in email and "only LIQUIDBEES interest on idle cash" in email
    assert "Portfolio value" in email and "Today's profit</div>" in email
    for out in (email, text):            # the overall profit (+₹35 since start) is now shown on purpose, so only the headline is checked for it
        for bad in ("since the start", "Total profit", "Total loss", "Total this month", "Month so far", "since the previous",
                    "since the 1st", "Today's profit: +₹35", "You're in the green"):
            assert bad not in out, bad
    assert "Today's profit: +₹17.26" in text and "Month P&L" not in text
