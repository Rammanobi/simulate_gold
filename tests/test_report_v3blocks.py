"""Optional report blocks: India price gap chips, 'Default vs AI vs executed', 'paper twins since start' (simgold/reportblocks.py).

Rules under test: every block is hidden when its data is absent, stale, switched off or corrupt; absent data leaves the compact email
BYTE-IDENTICAL (golden hashes of the 6 Nov fixture, produced by the code before these blocks existed); a failure inside a block hides the
block and never the email; the email stays under 60 KB and keeps the Gmail rules (tables, inline styles, bgcolor + both css backgrounds)."""
import copy
import hashlib
import html as htmllib
import json
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from simgold import config, dashboard, reportblocks as RB, report

FIX = Path(__file__).resolve().parent / "fixtures" / "2026-11-06"
REPO = Path(__file__).resolve().parent.parent
DAY = "2026-11-06"
LINK = "https://claude.ai/artifact/1YVZ1m6rDxCwccPzQtCz73#2026-11-06"
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
# sha256 of the 6 Nov fixture email + text as rendered by origin/main BEFORE the optional blocks existed (same setup as test_email_compact.py)
GOLDEN = {
    "evening_html": "b11ce20336aa893c422ac4b9ac503d36da89b0d085d5bee3750d4809c4132d57",
    "evening_text": "575571d6a43c3bd7554ae24c11938104d0bfa78d4a5f7180cd1381c33e1836d8",
    "morning_html": "6ab429772514b6284e04fce3f813d55021503018308cf8cdc7cc6f1bb5f9f054",
    "morning_text": "d2425104d0cc84a1db31817d58df4c86a20ff733645a63040c499773c05ca297",
}


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture(scope="module")
def cfg():
    p = config.load_params()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(p["strategy"], "mode", "v2_trend")
        mp.setitem(p["capital"], "idle", "cash")
        mp.setitem(p["report"], "portfolio_style", "COMPACT")
        yield p


def make_root(base: Path) -> Path:
    shutil.copytree(FIX, base, dirs_exist_ok=True)
    (base / "logs" / "ticks-2026-10-13.csv").write_text("t,value,cash,liquidbees,floor,GOLDBEES\n09:16,100000,100000,0,75000,121.0\n", encoding="utf-8")
    (base / "state" / "last_prices.json").write_text(json.dumps({
        "GOLDBEES": {"price": 116.2, "ts": "2026-11-06T15:29:00+05:30", "prev_close": 118.12},
        "SILVERBEES": {"price": 174.95, "ts": "2026-11-06T15:29:00+05:30", "prev_close": 177.86},
        "HINDCOPPER": {"price": 424.0, "ts": "2026-11-06T15:29:00+05:30", "prev_close": 427.33}}), encoding="utf-8")
    (base / "state" / "events.json").write_text(json.dumps([{"date": "2026-11-06", "type": "JOBS", "time": "19:00"}, {"date": "2026-11-09", "type": "CPI", "time": "19:00"}]), encoding="utf-8")
    (base / "state" / "guidance.json").write_text(json.dumps({"updated": "2026-11-06T14:00:00+05:30", "bias": {"GOLD": "neutral", "SILVER": "favour", "COPPER": "block"}}), encoding="utf-8")
    (base / "state" / "budget.json").write_text(json.dumps({"mode": "THRIFT"}), encoding="utf-8")
    return base


@pytest.fixture()
def root(tmp_path, cfg):
    return make_root(tmp_path / "r")


@pytest.fixture()
def on(cfg):
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(cfg, "pricegap", {"enabled": True})
        yield cfg


def render(root, date=DAY, morning=False):
    day = report.build_morning(date, root) if morning else report.build_day(date, root)
    return day, report.render_email(day, LINK), report.render_text(day, report_url=LINK)


def visible(h):
    t = re.sub(r"<style.*?</style>", "", h, flags=re.S)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", t)).replace(chr(160), " "))


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def rules_ok(h, t):
    p = Tags()
    p.feed(h)
    for tag, a in p.tags:
        assert tag not in ("svg", "img", "script", "link", "iframe")
        st = a.get("style") or ""
        if tag in ("td", "table", "body") and ("background" in st or "bgcolor" in a):
            assert a.get("bgcolor"), st
            col = a["bgcolor"].lower()
            assert f"background-color:{col}" in st.lower() and f"linear-gradient({col},{col})" in st.lower(), st
    assert not EMOJI.search(h) and not EMOJI.search(t) and "position:" not in h and "@import" not in h
    sizes = [float(x) for x in re.findall(r"font-size:\s*([\d.]+)px", h)]
    assert min(s for s in sizes if s > 0) >= 13
    for m in re.finditer(r'<td[^>]*font-size:0;[^>]*>(.*?)</td>', h):
        assert m.group(1) == "&nbsp;"
    assert len(h.encode()) <= 60_000
    assert "<table" in h and "bgcolor=" in h


# ---- data writers
def w_pricegap(root, date=DAY, warn=True):
    g = lambda p, m, z: {"premium_pct": p, "median20_pct": m, "z": z, "n": 60, "warn": warn and p >= 8}
    (root / "state" / "pricegap.json").write_text(json.dumps({"version": 1, "date": date, "warn_pct": 8, "duty_pct": 6, "gaps": {
        "GOLDBEES": g(11.5, 8.4, 2.7), "SILVERBEES": g(6.1, 9.0, -1.2)}}), encoding="utf-8")


def alloc_record(mode="shadow_v2_trades", **over):
    a = {"default": {"GOLDBEES": 0.7, "SILVERBEES": 0.2, "HINDCOPPER": 0.1}, "proposal": {"GOLDBEES": 0.5, "SILVERBEES": 0.3, "HINDCOPPER": 0.2},
         "confidence": 0.55, "k": 0.0, "advice": "allocation_file", "final": {"GOLDBEES": 0.8, "SILVERBEES": 0.15, "HINDCOPPER": 0.05},
         "clamps": [{"rule": "clamp_retry", "sym": None, "from": None, "to": None}, {"rule": "max_tilt", "sym": "SILVERBEES", "from": 0.3, "to": 0.2},
                    {"rule": "off_trend_cap", "sym": "HINDCOPPER", "from": 0.2, "to": 0.05}, {"rule": "turnover_limit", "sym": "GOLDBEES", "from": 0.5, "to": 0.7},
                    {"rule": "small_one", "sym": "GOLDBEES", "from": 0.70, "to": 0.71}]}
    a.update(over)
    rec = {"time": "15:10", "action": "ALLOC", "symbol": "ALL", "qty": 0, "price": None, "reason": "Allocation check", "alloc": a, "alloc_mode": mode, "executed": [],
           "v2_targets": {"GOLDBEES": 0.9, "SILVERBEES": 0.0, "HINDCOPPER": 0.1}}
    return rec


def w_alloc(root, mode="shadow_v2_trades", date=DAY, **over):
    with (root / "logs" / f"decisions-{date}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(alloc_record(mode, **over)) + "\n")


def w_twins(root, obj=None):
    (root / "state" / "twins.json").write_text(json.dumps(obj if obj is not None else {"since": "2026-10-13", "asof": "2026-11-06", "twins": {
        "default": {"value": 101234.5, "return_pct": 1.23, "max_dd_pct": -3.4}, "ai_tilt": {"value": 99800, "return_pct": -0.2, "max_dd_pct": 4.1}}}), encoding="utf-8")


# ------------------------------------------------------------------ byte-identical without data
def test_6_nov_fixture_is_byte_identical_without_new_data(root, cfg):
    day, h, t = render(root)
    assert (sha(h), sha(t)) == (GOLDEN["evening_html"], GOLDEN["evening_text"])
    assert not {"pricegap_view", "alloc_view", "twins_view"} & set(day)
    mday, mh, mt = render(root, "2026-11-09", morning=True)
    assert (sha(mh), sha(mt)) == (GOLDEN["morning_html"], GOLDEN["morning_text"])


def test_6_nov_stays_identical_when_pricegap_is_on_but_has_no_file(root, on):
    _, h, t = render(root)
    assert (sha(h), sha(t)) == (GOLDEN["evening_html"], GOLDEN["evening_text"])


def test_13_nov_is_byte_identical_without_new_data(cfg, monkeypatch):
    if not any(ln.startswith("2026-11-13") for ln in (REPO / "state" / "equity.csv").read_text(encoding="utf-8").splitlines()):
        pytest.skip("13 Nov is not in this checkout's equity.csv")
    if any((REPO / "state" / n).exists() for n in ("pricegap.json", "twins.json", "allocation.json")) or list((REPO / "logs").glob("decisions-2026-11-13.jsonl")) and "ALLOC" in (REPO / "logs" / "decisions-2026-11-13.jsonl").read_text(encoding="utf-8"):
        pytest.skip("new-data files exist in this checkout")
    day = report.build_day("2026-11-13", REPO)
    h, t = report.render_email(day, LINK), report.render_text(day, report_url=LINK)
    assert not {"pricegap_view", "alloc_view", "twins_view"} & set(day)
    # the same render with the blocks forced off must equal it (the blocks add nothing when no data exists)
    monkeypatch.setattr(RB, "attach", lambda *a, **k: None)
    day2 = report.build_day("2026-11-13", REPO)
    assert report.render_email(day2, LINK) == h and report.render_text(day2, report_url=LINK) == t


# ------------------------------------------------------------------ the blocks appear with data
def test_all_three_blocks_appear_in_html_and_text(root, on):
    w_pricegap(root); w_alloc(root); w_twins(root)
    day, h, t = render(root)
    v, vt = visible(h), t
    for x in (v, vt):
        assert "INDIA PRICE GAP" in x and "Gold +11.5%" in x and "Silver +6.1%" in x
        assert "20d median +8.4%" in x and "z +2.7" in x and "z −1.2" in x
        assert "plus 6% duty" in x and "estimate, cause not verified" in x
        assert "DEFAULT VS AI VS EXECUTED · phase shadow" in x
        assert "AI proposal: confidence 0.55" in x
        assert "PAPER TWINS · since 2026-10-13 · as of 2026-11-06" in x and "₹1,01,235" in x and "+1.2%" in x and "−3.4%" in x and "−4.1%" in x
    # default / AI / executed percentages (shadow: executed = what the v2 rule traded)
    assert re.search(r"Gold 70% 50% 90%", v) and re.search(r"Silver 20% 30% 0%", v) and re.search(r"Copper 10% 20% 10%", v)
    # the three biggest clamp changes only, biggest first; the notes without numbers rank last and the small one is cut
    assert "Clamp changed: turnover_limit Gold 50→70%; off_trend_cap Copper 20→5%; max_tilt Silver 30→20%" in v
    assert "small_one" not in v and "clamp_retry" not in v
    assert day["alloc_view"]["phase"] == "shadow" and day["pricegap_view"]["gaps"]["GOLDBEES"]["warn"]


def test_blocks_keep_the_email_rules_and_size(root, on):
    w_pricegap(root); w_alloc(root); w_twins(root)
    _, h, t = render(root)
    rules_ok(h, t)
    assert len(h.encode()) - 32757 < 14_000         # the three blocks together add about 10 KB; the 60 KB cap is checked in rules_ok
    _, mh, mt = render(root, "2026-11-09", morning=True)
    rules_ok(mh, mt)


def test_block_order_and_placement(root, on):
    w_pricegap(root); w_alloc(root); w_twins(root)
    _, h, t = render(root)
    v = visible(h)
    assert v.index("DEFAULT VS AI VS EXECUTED") < v.index("PAPER TWINS") < v.index("SAFETY") < v.index("INDIA PRICE GAP") < v.index("Open dashboard")
    assert t.index("DEFAULT VS AI VS EXECUTED") < t.index("PAPER TWINS") < t.index("SAFETY") < t.index("INDIA PRICE GAP") < t.index("Open dashboard")


@pytest.mark.parametrize("mode,phase,exe", [("shadow_v2_trades", "shadow", (90, 0, 10)), ("d5fi_ai_logged_only", "live_default", (80, 15, 5)), ("v3_live", "live", (80, 15, 5))])
def test_phase_and_executed_weights(root, on, mode, phase, exe):
    w_alloc(root, mode)
    day, h, t = render(root)
    assert day["alloc_view"]["phase"] == phase
    assert f"phase {phase}" in t and re.search(rf"Gold 70% 50% {exe[0]}%", visible(h)) and re.search(rf"Silver 20% 30% {exe[1]}%", visible(h))


def test_alloc_without_ai_proposal_and_without_clamps(root, on):
    w_alloc(root, proposal=None, confidence=0.0, clamps=[])
    day, h, t = render(root)
    assert "AI proposal: none (default kept)" in t and "Clamp changed: nothing" in t and re.search(r"Gold 70% – ", visible(h))


def test_alloc_falls_back_to_the_routine_file_when_the_engine_has_not_logged(root, on):
    (root / "state" / "allocation.json").write_text(json.dumps({"for_date": DAY, "weights": {"GOLDBEES": 0.6, "SILVERBEES": 0.3, "HINDCOPPER": 0.1}, "confidence": 0.4,
                                                              "default_at_write": {"GOLDBEES": 0.98, "SILVERBEES": 0.0, "HINDCOPPER": 0.0}}), encoding="utf-8")
    day, h, t = render(root)
    assert day["alloc_view"]["source"] == "file" and "Engine check not logged yet" in t and re.search(r"Gold 98% 60% –", visible(h))
    (root / "state" / "allocation.json").write_text(json.dumps({"for_date": "2026-11-05", "weights": {"GOLDBEES": 1.0}}), encoding="utf-8")
    assert "alloc_view" not in report.build_day(DAY, root)               # another day's proposal is not shown


def test_twins_schema_variants(root, on):
    w_twins(root, {"rows": [{"name": "default", "value": 100500, "return": 0.005, "max_dd": 0.021}, {"name": "x" * 60, "equity": 99000, "ret_pct": -1.0},
                            {"value": 5}, "junk", {"name": "ghost"}]})
    day, h, t = render(root)
    rows = day["twins_view"]["rows"]
    assert [r["name"] for r in rows] == ["default", "x" * 24]            # unnamed / empty / non-dict rows dropped, long names cut
    assert rows[0]["ret_pct"] == pytest.approx(0.5) and rows[0]["dd_pct"] == pytest.approx(-2.1) and rows[1]["dd_pct"] is None
    assert "PAPER TWINS · since start" in t and "₹1,00,500  +0.5%  −2.1%" in t


def test_twins_limited_to_six_rows(root, on):
    w_twins(root, {"twins": {f"t{i}": {"value": 100000 + i, "return_pct": 0.1} for i in range(12)}})
    assert len(report.build_day(DAY, root)["twins_view"]["rows"]) == RB.MAX_TWINS


def test_pricegap_only_when_enabled_and_dated_the_report_day(root, cfg):
    w_pricegap(root)
    day = report.build_day(DAY, root)
    assert "pricegap_view" not in day                                    # enabled: false (the shipped state) hides it even with a file
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(cfg, "pricegap", {"enabled": True})
        assert "pricegap_view" in report.build_day(DAY, root)
        w_pricegap(root, date="2026-11-05")
        assert "pricegap_view" not in report.build_day(DAY, root)       # a stale file is not shown as today's
        (root / "state" / "pricegap.json").unlink()
        assert "pricegap_view" not in report.build_day(DAY, root)


def test_pricegap_chip_tones(root, on):
    w_pricegap(root)
    _, h, _ = render(root)
    assert h.count("#fdf0cf") >= 1                      # the over-band chip is amber, the other grey
    w_pricegap(root, warn=False)
    day = report.build_day(DAY, root)
    assert all(not g["warn"] for g in day["pricegap_view"]["gaps"].values())


# ------------------------------------------------------------------ failure injection
CORRUPT = ["", "{", "[]", "null", "42", '"x"', '{"twins": 5}', '{"twins": {"a": 3}}', '{"gaps": {"GOLDBEES": {"premium_pct": "NaN"}}}',
           '{"for_date": 7, "weights": []}', '{"twins": {"a": {"value": NaN}}}', "\x00\x01\x02", '{"rows": [[1, 2]]}']


@pytest.mark.parametrize("junk", CORRUPT)
def test_corrupt_files_never_cost_the_email(root, on, junk):
    for n in ("pricegap.json", "allocation.json", "twins.json"):
        (root / "state" / n).write_text(junk, encoding="utf-8")
    with (root / "logs" / f"decisions-{DAY}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(junk.replace("\n", " ") + "\n")
    day, h, t = render(root)
    assert (sha(h), sha(t)) == (GOLDEN["evening_html"], GOLDEN["evening_text"])        # corrupt data = no block = the same email as before
    assert not {"pricegap_view", "alloc_view", "twins_view"} & set(day)


def test_binary_garbage_files(root, on):
    for n in ("pricegap.json", "allocation.json", "twins.json"):
        (root / "state" / n).write_bytes(b"\xff\xfe\x00garbage\x80")
    _, h, t = render(root)
    assert sha(h) == GOLDEN["evening_html"]


def test_weird_types_in_the_alloc_record_hide_or_degrade_the_block(root, on):
    w_alloc(root, "v3_live", default="x", proposal=[1], final=None, clamps="nope", confidence="high")
    day, h, t = render(root)
    assert "alloc_view" not in day and sha(h) == GOLDEN["evening_html"]
    w_alloc(root, clamps=[None, 3, {"rule": 5}, {"rule": "ok <script>alert(1)</script>", "sym": "<b>", "from": "a", "to": float("nan")}])
    day, h, t = render(root)
    assert "alert(1)" not in h and "<script" not in h and "ok scriptalert1/script" in visible(h)        # the markup characters are stripped, the rest is plain text
    rules_ok(h, t)


def test_injection_text_in_names_is_stripped_and_escaped(root, on):
    w_twins(root, {"twins": {"<img src=x onerror=1>": {"value": 1, "return_pct": 1}}})
    _, h, t = render(root)
    assert "<img" not in h and "img srcx onerror1" in visible(h)          # the markup characters are stripped from names; what is left is plain text
    rules_ok(h, t)


def test_exception_in_a_view_builder_hides_the_block_not_the_email(root, on, monkeypatch):
    w_pricegap(root); w_alloc(root); w_twins(root)
    for fn in ("pricegap_view", "alloc_view", "twins_view"):
        monkeypatch.setattr(RB, fn, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    day, h, t = render(root)
    assert sha(h) == GOLDEN["evening_html"] and sha(t) == GOLDEN["evening_text"]


def test_attach_itself_failing_is_swallowed(root, on, monkeypatch):
    monkeypatch.setattr(RB, "attach", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _, h, _ = render(root)
    assert sha(h) == GOLDEN["evening_html"]


@pytest.mark.parametrize("name", ["alloc_rows", "alloc_footer", "alloc_title", "pricegap_chips", "pricegap_note", "twins_cells", "twins_title"])
def test_exception_while_building_the_model_hides_that_block(root, on, monkeypatch, name, capsys):
    w_pricegap(root); w_alloc(root); w_twins(root)
    monkeypatch.setattr(RB, name, lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    day, h, t = render(root)
    v = visible(h)
    hidden = {"alloc_rows": "DEFAULT VS AI", "alloc_footer": "DEFAULT VS AI", "alloc_title": "DEFAULT VS AI", "pricegap_chips": "INDIA PRICE GAP",
              "pricegap_note": "INDIA PRICE GAP", "twins_cells": "PAPER TWINS", "twins_title": "PAPER TWINS"}[name]
    assert hidden not in v and hidden not in t
    others = {"DEFAULT VS AI", "INDIA PRICE GAP", "PAPER TWINS"} - {hidden}
    assert all(o in v and o in t for o in others)            # the other blocks and the email itself are intact
    assert "Total value" in t and "SAFETY" in v
    assert "block hidden" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["_alloc_html", "_twins_html", "_gap_html"])
def test_exception_while_rendering_a_block_hides_that_block(root, on, monkeypatch, name):
    w_pricegap(root); w_alloc(root); w_twins(root)
    monkeypatch.setattr(report, name, lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    _, h, t = render(root)
    assert "Open dashboard" in visible(h) and "SAFETY" in visible(h) and "<html" in h
    assert "Total value" in t


def test_text_block_failure_is_isolated(root, on, monkeypatch):
    w_pricegap(root); w_alloc(root); w_twins(root)
    monkeypatch.setattr(report, "_alloc_text", lambda b: (_ for _ in ()).throw(KeyError("boom")))
    _, h, t = render(root)
    assert "DEFAULT VS AI" not in t and "PAPER TWINS" in t and "INDIA PRICE GAP" in t and "Total value" in t


def test_whole_compact_failure_still_falls_back_to_ab(root, on, monkeypatch):
    """The pre-existing safety net is untouched: if the compact render dies for any reason, the AB email still goes out."""
    w_pricegap(root)
    day = report.build_day(DAY, root)
    monkeypatch.setattr(report, "compact_model", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "<html" in report.render_email(day, LINK) and "Total" in report.render_text(day, report_url=LINK)


# ------------------------------------------------------------------ dashboard day JSON and the page
def test_dashboard_document_has_the_keys_only_with_data(root, on):
    doc = dashboard.build(DAY, root)
    assert not {"pricegap", "allocview", "twins"} & set(doc)
    w_pricegap(root); w_alloc(root); w_twins(root)
    doc = dashboard.build(DAY, root)
    assert doc["pricegap"]["gaps"]["GOLDBEES"]["premium_pct"] == 11.5 and doc["allocview"]["phase"] == "shadow" and doc["twins"]["rows"][0]["name"] == "default"
    json.dumps(doc)


def test_page_template_carries_the_optional_section_and_data(root, on):
    w_pricegap(root); w_alloc(root); w_twins(root)
    day = report.prepare(report.build_day(DAY, root))
    page = report.render_page(day, REPO / "templates" / "daily_report_template.html")
    assert 'id="v3sec"' in page and "D.pricegap_view" in page and "D.alloc_view" in page and "D.twins_view" in page
    assert '"pricegap_view"' in page and '"alloc_view"' in page and '"twins_view"' in page
    bare = report.render_page(report.prepare(report.build_day(DAY, make_root_for(root))), REPO / "templates" / "daily_report_template.html")
    assert '"pricegap_view"' not in bare


def make_root_for(root):
    for n in ("pricegap.json", "twins.json"):
        (root / "state" / n).unlink(missing_ok=True)
    p = root / "logs" / f"decisions-{DAY}.jsonl"
    p.write_text("\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines() if '"ALLOC"' not in ln) + "\n", encoding="utf-8")
    return root


def test_view_builders_unit():
    assert RB.sp(8.24) == "+8.2" and RB.sp(-3.14) == "−3.1" and RB.sp(-0.04) == "0.0" and RB.sp(0) == "0.0"
    assert RB._clamp_notes([{"rule": "a", "sym": "GOLDBEES", "from": 0.1, "to": 0.5}, {"rule": "b", "sym": None}, {"rule": "c", "from": 0.5, "to": 0.4}], 2) == ["a Gold 10→50%", "c 50→40%"]
    assert RB.twins_view(Path("/nonexistent")) is None and RB.alloc_view(DAY, Path("/nonexistent")) is None
