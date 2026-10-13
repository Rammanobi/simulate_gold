"""Memory layer: brief, should_think, the guidance CLI, context.py. No network, no AI."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import context as ctx          # noqa: E402
import should_think as st      # noqa: E402

from simgold import brief, config                       # noqa: E402
from simgold.broker import Portfolio, Position          # noqa: E402
from simgold.market import IST                          # noqa: E402

P = config.load_params()
NOW = datetime(2026, 10, 14, 11, 0, tzinfo=IST)         # a Wednesday, market open
TODAY = NOW.date().isoformat()


def write(root, rel, obj):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


def make_root(tmp_path, positions=None, floor=99500.0, liquid=100000.0, guidance_age_min=30, prices=None,
              guidance_prices=None):
    pf = Portfolio(cash=0.0, liquidbees=liquid, floor=floor, day_costs_date=TODAY, day_open_value=100000.0)
    for sym, (q, avg, last, stop) in (positions or {}).items():
        pf.positions[sym] = Position(qty=q, avg_cost=avg, last_price=last, stop=stop, opened=TODAY)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    pf.save(tmp_path / "state" / "portfolio.json")
    if prices is not None:
        write(tmp_path, "state/last_prices.json",
              {s: {"price": v, "ts": NOW.isoformat(), "prev_close": v} for s, v in prices.items()})
    if guidance_age_min is not None:
        g = {"updated": (NOW - timedelta(minutes=guidance_age_min)).isoformat(),
             "bias": {"GOLD": "neutral", "SILVER": "neutral", "COPPER": "block"}, "note": "n"}
        if guidance_prices:
            g["prices_at_update"] = guidance_prices
        write(tmp_path, "state/guidance.json", g)
    return tmp_path


def news(root, items):
    write(root, "state/news.json", {x["id"]: x for x in items})


def h(i, title, age_min=10):
    return {"id": i, "title": title, "ts": (NOW - timedelta(minutes=age_min)).timestamp(), "source": "x"}


# ------------------------------------------------------------------ brief
def test_brief_has_the_sections_and_the_numbers(tmp_path):
    root = make_root(tmp_path, positions={"GOLDBEES": (100, 100.0, 101.0, 98.8)}, prices={"GOLDBEES": 101.0})
    write(root, "logs/decisions-%s.jsonl" % TODAY, json.dumps(
        {"time": "10:05", "action": "BUY", "symbol": "GOLDBEES", "qty": 100, "price": 100.0, "reason": "score held"}) + "\n")
    write(root, "state/events.json", [{"date": "2026-10-15", "type": "CPI"}])
    text = brief.build_brief(root, NOW, P)
    for needle in ("Brief 2026-10-14 11:00 IST", "NSE OPEN", "floor ₹99,500", "GOLDBEES qty 100", "10:05 BUY GOLDBEES",
                   "COPPER=block", "us_cpi"):
        assert needle in text
    assert (root / "memory" / "brief.md").read_text(encoding="utf-8") == text


def test_brief_stays_inside_its_caps_with_a_flood_of_input(tmp_path):
    root = make_root(tmp_path, prices={"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 400.0})
    rows = [{"time": "%02d:%02d" % (9 + i // 60, i % 60), "action": "SKIP", "symbol": "SILVERBEES", "qty": 0,
             "price": 200.0, "reason": "x" * 400} for i in range(300)]
    write(root, "logs/decisions-%s.jsonl" % TODAY, "\n".join(json.dumps(r) for r in rows) + "\n")
    news(root, [h("id%d" % i, "Fed rate cut hopes lift gold " + "y" * 300 + str(i), age_min=i + 1) for i in range(80)])
    text = brief.build_brief(root, NOW, P)
    lines = text.splitlines()
    assert len(lines) <= 120 and len(text.encode("utf-8")) <= 6000
    assert sum(1 for x in lines if x.startswith("[u")) <= 15
    assert "earlier decisions omitted" in text


def test_brief_shows_only_new_headlines(tmp_path):
    root = make_root(tmp_path)
    news(root, [h("a1", "Fed signals rate cut as CPI cools"), h("a2", "Gold edges higher")])
    first = brief.build_brief(root, NOW, P)
    assert "Fed signals rate cut" in first and "Gold edges higher" in first
    assert "[u4 " in first                                     # tagged with urgency and categories
    seen = json.loads((root / "state" / "seen_headlines.json").read_text())
    assert set(seen["seen"]) == {"a1", "a2"}
    second = brief.build_brief(root, NOW + timedelta(minutes=5), P)
    assert "Fed signals rate cut" not in second and "Gold edges higher" not in second
    news(root, [h("a1", "Fed signals rate cut as CPI cools"), h("a2", "Gold edges higher"),
                h("a3", "War fears push gold to a record", age_min=1)])
    third = brief.build_brief(root, NOW + timedelta(minutes=10), P)
    assert "War fears push gold" in third and "Fed signals" not in third


def test_brief_caps_headlines_at_15_and_orders_by_urgency(tmp_path):
    root = make_root(tmp_path)
    items = [h("n%d" % i, "Gold steady number %d" % i, age_min=i + 1) for i in range(30)]
    items.append(h("hot", "Ceasefire collapses as war escalates", age_min=500))
    news(root, items)
    text = brief.build_brief(root, NOW, P)
    tagged = [x for x in text.splitlines() if x.startswith("[u")]
    assert len(tagged) == 15 and "Ceasefire collapses" in tagged[0]


def test_dry_run_does_not_touch_the_dedupe_state(tmp_path):
    root = make_root(tmp_path)
    news(root, [h("a1", "Gold edges higher")])
    brief.build_brief(root, NOW, P, write=False)
    assert not (root / "state" / "seen_headlines.json").exists() and not (root / "memory" / "brief.md").exists()


def test_brief_price_changes_since_previous_close_and_last_brief(tmp_path):
    root = make_root(tmp_path)
    write(root, "state/last_prices.json", {"GOLDBEES": {"price": 101.0, "ts": NOW.isoformat(), "prev_close": 100.0},
                                           "DXY": {"price": 99.0, "ts": NOW.isoformat(), "prev_close": 99.5}})
    brief.build_brief(root, NOW, P)                             # stores 101.0 as the last-brief price
    write(root, "state/last_prices.json", {"GOLDBEES": {"price": 102.01, "ts": NOW.isoformat(), "prev_close": 100.0},
                                           "DXY": {"price": 99.0, "ts": NOW.isoformat(), "prev_close": 99.5}})
    text = brief.build_brief(root, NOW + timedelta(minutes=5), P)
    line = next(x for x in text.splitlines() if x.startswith("GOLDBEES 102.01"))
    assert "+2.01%" in line and "+1.00%" in line and any(x.startswith("DXY") for x in text.splitlines())


def test_brief_never_fails_on_an_empty_repo(tmp_path):
    text = brief.build_brief(tmp_path, NOW, P)
    assert "Guidance" in text and "MISSING" in text


# ------------------------------------------------------------------ should_think
def quiet_root(tmp_path, **kw):
    kw.setdefault("prices", {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 400.0})
    kw.setdefault("guidance_prices", {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 400.0})
    return make_root(tmp_path, **kw)


def test_quiet_market_skips(tmp_path):
    assert st.evaluate(quiet_root(tmp_path), NOW, params=P) == []


def test_think_when_guidance_missing_or_old(tmp_path):
    assert any("missing" in w for w in st.evaluate(quiet_root(tmp_path, guidance_age_min=None), NOW, params=P))
    old = st.evaluate(quiet_root(tmp_path / "b", guidance_age_min=200), NOW, params=P)
    assert any("old" in w for w in old)


def test_think_on_a_big_move_since_guidance(tmp_path):
    # GOLDBEES 10-minute threshold is 0.6%; 0.75x = 0.45%
    big = st.evaluate(quiet_root(tmp_path, prices={"GOLDBEES": 100.5, "SILVERBEES": 200.0, "HINDCOPPER": 400.0}), NOW, params=P)
    assert any("GOLDBEES moved +0.50%" in w for w in big)
    small = st.evaluate(quiet_root(tmp_path / "b", prices={"GOLDBEES": 100.4, "SILVERBEES": 200.0, "HINDCOPPER": 400.0}), NOW, params=P)
    assert small == []


def test_think_on_a_new_urgent_headline_only_if_after_guidance(tmp_path):
    root = quiet_root(tmp_path)                                  # guidance is 30 min old
    news(root, [h("old", "Fed signals rate cut", age_min=90), h("low", "Rupee steady", age_min=5)])
    assert st.evaluate(root, NOW, params=P) == []               # old one predates guidance; rupee is urgency 2
    news(root, [h("new", "Fed signals rate cut", age_min=5)])
    assert any("urgency >= 3" in w for w in st.evaluate(root, NOW, params=P))


def test_think_on_event_today_or_tomorrow(tmp_path):
    root = quiet_root(tmp_path)
    write(root, "state/events.json", [{"date": "2026-10-16", "type": "FOMC"}])
    assert st.evaluate(root, NOW, params=P) == []
    write(root, "state/events.json", [{"date": "2026-10-15", "type": "CPI"}])
    assert any("us_cpi tomorrow" in w for w in st.evaluate(root, NOW, params=P))
    write(root, "state/events.json", [{"date": TODAY, "type": "RBI"}])
    assert any("rbi today" in w for w in st.evaluate(root, NOW, params=P))


def test_think_when_a_position_is_close_to_its_stop(tmp_path):
    far = quiet_root(tmp_path, positions={"GOLDBEES": (10, 100.0, 100.0, 98.8)})
    assert st.evaluate(far, NOW, params=P) == []
    near = quiet_root(tmp_path / "b", positions={"GOLDBEES": (10, 100.0, 100.0, 99.5)})
    assert any("within 1% of its stop" in w for w in st.evaluate(near, NOW, params=P))


def test_think_when_cushion_is_thin(tmp_path):
    root = quiet_root(tmp_path, floor=99800.0, liquid=100000.0)     # cushion 200
    assert any("cushion" in w for w in st.evaluate(root, NOW, params=P))


def test_think_on_the_first_run_of_the_day(tmp_path):
    root = quiet_root(tmp_path, guidance_age_min=0)
    g = json.loads((root / "state" / "guidance.json").read_text())
    g["updated"] = (NOW - timedelta(days=1)).replace(hour=15).isoformat()
    write(root, "state/guidance.json", g)
    out = st.evaluate(root, NOW, params=P)
    assert any("first run of the day" in w for w in out)


def test_force_and_exit_codes(tmp_path):
    root = quiet_root(tmp_path)

    def run(*a):
        return subprocess.run([sys.executable, str(ROOT / "tools" / "should_think.py"), "--root", str(root),
                               "--now", NOW.isoformat(), *a], capture_output=True, text=True)
    skip = run()
    assert skip.returncode == 3 and skip.stdout.startswith("SKIP:")
    think = run("--force")
    assert think.returncode == 0 and think.stdout.startswith("THINK:") and "--force" in think.stdout


# ------------------------------------------------------------------ guidance CLI
def cli(root, *args, now=NOW):
    return subprocess.run([sys.executable, str(ROOT / "tools" / "guidance.py"), "--root", str(root),
                           "--now", now.isoformat(), *args], capture_output=True, text=True)


def test_set_writes_the_shape_the_engine_reads(tmp_path):
    root = make_root(tmp_path, guidance_age_min=None, prices={"GOLDBEES": 100.0})
    r = cli(root, "set", "--bias", "GOLD=favour", "SILVER=neutral", "COPPER=block", "--note", "dollar weaker")
    assert r.returncode == 0, r.stderr
    g = json.loads((root / "state" / "guidance.json").read_text())
    assert g["bias"] == {"GOLD": "favour", "SILVER": "neutral", "COPPER": "block"} and g["note"] == "dollar weaker"
    assert g["prices_at_update"] == {"GOLDBEES": 100.0}
    # the engine's own reader accepts it
    from simgold.engine import Engine, Env, Market
    eng = Engine(Portfolio(), Market(now=NOW, prices={}, price_ts={}, scores={}),
                 Env(state_dir=root / "state", logs_dir=root / "logs", params=P))
    bias, fresh, _ = eng.guidance()
    assert fresh and bias == {"GOLDBEES": "favour", "SILVERBEES": "neutral", "HINDCOPPER": "block"}


def test_set_keeps_unmentioned_keys_and_refuses_bad_input(tmp_path):
    root = make_root(tmp_path)                                  # COPPER=block already there
    assert cli(root, "set", "--bias", "GOLD=favour").returncode == 0
    assert json.loads((root / "state" / "guidance.json").read_text())["bias"]["COPPER"] == "block"
    before = (root / "state" / "guidance.json").read_text()
    for bad in (["GOLD=buy"], ["PLATINUM=favour"], ["GOLD"], ["GOLD=favor"]):
        r = cli(root, "set", "--bias", *bad)
        assert r.returncode == 2 and "REFUSED" in r.stderr, bad
    assert (root / "state" / "guidance.json").read_text() == before


def test_fastlane_writes_what_the_engine_accepts(tmp_path):
    root = make_root(tmp_path, positions={"SILVERBEES": (50, 200.0, 210.0, 200.0)})
    r = cli(root, "fastlane", "--symbol", "SILVERBEES", "--block-minutes", "60", "--sell-fraction", "0.5",
            "--tighten-stop", "205.1", "--note", "shock")
    assert r.returncode == 0, r.stderr
    raw = json.loads((root / "state" / "fastlane.json").read_text())
    assert raw["SILVERBEES"]["sell_fraction"] == 0.5 and raw["SILVERBEES"]["tighten_stop"] == 205.1
    assert raw["SILVERBEES"]["block_until"].startswith("2026-10-14T12:00:00") and raw["note"] == "shock"
    from simgold import guard
    acc, rej = guard.sanitize_fastlane(raw, NOW, {"SILVERBEES": {"qty": 50, "price": 210.0, "stop": 200.0}}, P)
    assert not rej and {a["type"] for a in acc} == {"block_until", "sell_fraction", "tighten_stop"}


def test_fastlane_refuses_what_the_engine_refuses(tmp_path):
    root = make_root(tmp_path, positions={"SILVERBEES": (50, 200.0, 210.0, 200.0)})
    bad = [
        ["--symbol", "SILVERBEES", "--bias", "favour"],                        # no favour in the fast lane
        ["--symbol", "SILVERBEES", "--tighten-stop", "190"],                   # loosening
        ["--symbol", "SILVERBEES", "--tighten-stop", "215"],                   # not below the price
        ["--symbol", "GOLDBEES", "--sell-fraction", "0.5"],                    # no position
        ["--symbol", "SILVERBEES", "--sell-fraction", "1.5"],
        ["--symbol", "SILVERBEES", "--buy", "10"],                             # no buys
        ["--symbol", "SILVERBEES", "--block-minutes", "0"],
        ["--symbol", "PLATINUM", "--block-minutes", "30"],
        ["--symbol", "SILVERBEES"],                                            # nothing to do
        ["--block-minutes", "30"],                                             # no symbol
    ]
    for args in bad:
        r = cli(root, "fastlane", *args)
        assert r.returncode == 2 and "REFUSED" in r.stderr, args
        assert not (root / "state" / "fastlane.json").exists(), args


def test_fastlane_can_cover_several_symbols_and_halt(tmp_path):
    root = make_root(tmp_path)
    r = cli(root, "fastlane", "--symbol", "GOLD", "--bias", "block", "--symbol", "COPPER", "--block-minutes", "45",
            "--halt-buys-today")
    assert r.returncode == 0, r.stderr
    raw = json.loads((root / "state" / "fastlane.json").read_text())
    assert raw["GOLDBEES"] == {"bias": "block"} and "block_until" in raw["HINDCOPPER"] and raw["halt_buys_today"] is True


def test_events_roundtrip_and_validation(tmp_path):
    root = make_root(tmp_path)
    assert cli(root, "events", "add", "--date", "2026-10-15", "--type", "cpi", "--time", "18:00").returncode == 0
    assert cli(root, "events", "add", "--date", "2026-10-15", "--type", "CPI").returncode == 0     # replaces, no duplicate
    assert cli(root, "events", "add", "--date", "2026-10-28", "--type", "FOMC").returncode == 0
    ev = json.loads((root / "state" / "events.json").read_text())
    assert ev == [{"date": "2026-10-15", "type": "CPI"}, {"date": "2026-10-28", "type": "FOMC"}]
    from simgold.guard import normalise_events
    assert [e["type"] for e in normalise_events(ev)] == ["us_cpi", "fomc"]
    for args in (["--date", "2026-10-13", "--type", "CPI"], ["--date", "2026-13-01", "--type", "CPI"],
                 ["--date", "2026-10-20", "--type", "CRASH"], ["--date", "2026-10-20", "--type", "CPI", "--time", "6pm"]):
        assert cli(root, "events", "add", *args).returncode == 2, args
    assert cli(root, "events", "remove", "--date", "2026-10-28", "--type", "FOMC").returncode == 0
    assert len(json.loads((root / "state" / "events.json").read_text())) == 1


def test_market_today_and_tomorrow_match_the_report_readers(tmp_path):
    from simgold import report
    root = make_root(tmp_path)
    r = cli(root, "market-today", "--item", "Gold (global):+0.8:Weaker dollar", "--item", "USD/INR:0.1:Rupee slightly weaker")
    assert r.returncode == 0, r.stderr
    assert cli(root, "tomorrow", "--text", "US CPI comes out after the close.").returncode == 0
    day = report.build_day(TODAY, root)
    assert [m["name"] for m in day["market"]] == ["Gold (global)", "USD/INR"] and day["market"][0]["move_pct"] == 0.8
    assert day["tomorrow"] == "US CPI comes out after the close."
    assert cli(root, "market-today", "--item", "Gold:high:x").returncode == 2
    assert cli(root, "market-today", "--item", "Gold:400:x").returncode == 2
    assert cli(root, "tomorrow", "--text", "<b>hi</b>").returncode == 2
    assert cli(root, "tomorrow", "--text", "x" * 700).returncode == 2


# ------------------------------------------------------------------ context.py
def test_context_order_and_the_last_three_daily_notes(tmp_path):
    mem = tmp_path / "memory"
    (mem / "daily").mkdir(parents=True)
    (mem / "weekly").mkdir()
    (mem / "brief.md").write_text("BRIEF\n", encoding="utf-8")
    (mem / "lessons.md").write_text("LESSONS\n", encoding="utf-8")
    for d in ("2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"):
        (mem / "daily" / (d + ".md")).write_text("DAILY " + d + "\n", encoding="utf-8")
    (mem / "weekly" / "2026-W41.md").write_text("OLD WEEK\n", encoding="utf-8")
    (mem / "weekly" / "2026-W42.md").write_text("NEW WEEK\n", encoding="utf-8")
    out = ctx.build(tmp_path)
    order = [out.index(x) for x in ("BRIEF", "LESSONS", "DAILY 2026-10-09", "DAILY 2026-10-12", "DAILY 2026-10-13", "NEW WEEK")]
    assert order == sorted(order) and "DAILY 2026-10-08" not in out and "OLD WEEK" not in out


def test_context_drops_oldest_daily_first_when_over_the_cap(tmp_path):
    mem = tmp_path / "memory"
    (mem / "daily").mkdir(parents=True)
    (mem / "weekly").mkdir()
    (mem / "brief.md").write_text("BRIEF\n" + "b" * 5000, encoding="utf-8")
    (mem / "lessons.md").write_text("LESSONS\n" + "l" * 5000, encoding="utf-8")
    for d in ("2026-10-09", "2026-10-12", "2026-10-13"):
        (mem / "daily" / (d + ".md")).write_text("DAILY " + d + "\n" + "d" * 5000, encoding="utf-8")
    (mem / "weekly" / "2026-W42.md").write_text("WEEK\n" + "w" * 3000, encoding="utf-8")
    out = ctx.build(tmp_path)
    assert len(out.encode("utf-8")) <= 25000
    assert "DAILY 2026-10-09" not in out and "DAILY 2026-10-12" in out and "DAILY 2026-10-13" in out and "WEEK" in out


def test_context_hard_cap_even_for_huge_files(tmp_path):
    mem = tmp_path / "memory"
    (mem / "daily").mkdir(parents=True)
    (mem / "brief.md").write_text("BRIEF\n" + "line\n" * 20000, encoding="utf-8")
    (mem / "lessons.md").write_text("LESSONS\n" + "é" * 40000, encoding="utf-8")
    (mem / "daily" / "2026-10-13.md").write_text("D\n" * 20000, encoding="utf-8")
    out = ctx.build(tmp_path)
    assert len(out.encode("utf-8")) <= 25000 and out.startswith("===== memory/brief.md")


def test_context_works_on_the_real_repo_layout():
    out = ctx.build(ROOT)
    assert "===== memory/lessons.md =====" in out and len(out.encode("utf-8")) <= 25000
