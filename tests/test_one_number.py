"""One number per metric on every surface (summary, holdings, report, dashboard, alldays, live feed) and numeric trend levels.

Fixture: the real engine state at the close of Friday 6 Nov 2026 (tests/fixtures/2026-11-06, copied from state/ and logs/ of
commit 7c4c631): 437 GOLDBEES held; 15:10 check price 116.13, last tick (15:29) 116.20, the 15:26 BLOCK record 116.33; one SELL
in the ledger with pnl -1,166.15.

Definitions (simgold/summary.py):
  value    = units x the day's close price (the last tick) after the estimated cost of selling
  open P&L = value - (units x avg + buy costs)
  booked   = sum of the SELL trades' pnl (after costs)
  interest = the pre-22-Oct LIQUIDBEES income, shown on its own
  overall  = total - 100000 = open + booked + interest, to the paisa
"""
import json
import logging
import random
import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from simgold import alldays, config, dashboard, holdings, livepnl, report, summary, trend
from simgold.market import IST

FIX = Path(__file__).resolve().parent / "fixtures" / "2026-11-06"
DAY = "2026-11-06"


def D(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


@pytest.fixture(scope="module")
def shipped_config():
    """tests/conftest.py caches v1 + LIQUIDBEES params for the older tests; 6 Nov ran on the shipped v2 trend core with idle cash."""
    p = config.load_params()
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(p["strategy"], "mode", "v2_trend")
        mp.setitem(p["capital"], "idle", "cash")
        yield p


@pytest.fixture(scope="module")
def surfaces(tmp_path_factory, shipped_config):
    root = tmp_path_factory.mktemp("nov6")
    shutil.copytree(FIX, root, dirs_exist_ok=True)
    day = report.build_day(DAY, root)
    doc = dashboard.build(DAY, root)
    all_days = alldays.build(root)
    pf = json.loads((root / "state" / "portfolio.json").read_text())
    prices = holdings.last_prices(root, DAY)
    live = livepnl.build(pf, prices, {}, livepnl.read_equity(root / "state" / "equity.csv"), shipped_config,
                         datetime(2026, 11, 6, 15, 29, tzinfo=IST), [], market_open=False)
    return {"root": root, "day": day, "doc": doc, "alldays": all_days["days"][-1], "alldays_full": all_days, "live": live, "pf": pf,
            "prices": prices}


def _gold(rows, k):
    return next(r[k] for r in rows if r["symbol"] == "GOLDBEES")


def test_real_figures_6_nov(surfaces):
    ms = surfaces["day"]["money_summary"]
    assert surfaces["prices"]["GOLDBEES"] == 116.2                # the close: last tick, not the 15:10 check (116.13) or the 15:26 block (116.33)
    assert ms["total_now"] == 100328.32 and ms["cash"] == 49574.31
    assert ms["metals"][0]["value"] == 50754.01 and ms["metals"][0]["gross_value"] == 50779.4
    assert ms["live_profit"] == 1347.65
    assert ms["breakdown"] == {"booked": -1166.15, "open": 1347.65, "interest": 146.82, "sum": 328.32, "costs_in_booked": -46.52}
    assert ms["overall_profit"] == 328.32


def test_total_is_cash_plus_net_holdings(surfaces):
    ms = surfaces["day"]["money_summary"]
    assert D(ms["total_now"]) == D(ms["cash"]) + sum(D(m["value"]) for m in ms["metals"])
    assert abs(D(ms["cash"]) - D(surfaces["pf"]["cash"])) <= Decimal("0.01")   # the real cash, not a plug absorbing a price mismatch


def test_one_total_everywhere(surfaces):
    s = surfaces
    totals = {s["day"]["portfolio"]["close"], s["day"]["money_summary"]["total_now"], s["doc"]["close"], s["doc"]["money"]["total_now"],
              s["alldays"]["close_value"], s["live"]["total_value"], s["live"]["summary"]["total_now"],
              round(sum(s["doc"]["alloc"].values()), 2), s["day"]["holdings_view"]["total_value"]}
    assert totals == {100328.32}


def test_one_cash_everywhere(surfaces):
    s = surfaces
    cash = {s["day"]["money_summary"]["cash"], s["doc"]["money"]["cash"], s["doc"]["alloc"]["LIQUIDBEES"], s["alldays"]["cash_value"],
            s["alldays"]["alloc"]["Cash"], s["live"]["cash"], s["live"]["summary"]["cash"], s["day"]["portfolio"]["cash_in_liquidbees"],
            next(r["value"] for r in s["doc"]["money"]["funds"]["rows"] if r["symbol"] == "CASH"),
            next(r["value"] for r in s["doc"]["holdings"] if r["symbol"] == "LIQUIDBEES")}
    assert cash == {49574.31}


def test_one_gold_pnl_everywhere(surfaces):
    s = surfaces
    pnl = {_gold(s["day"]["money_summary"]["metals"], "open_gain"), _gold(s["day"]["money_summary"]["funds"]["rows"], "profit"),
           _gold(s["day"]["holdings_view"]["rows"], "gain"), _gold(s["day"]["holdings_view"]["rows"], "unrealized"),
           _gold(s["day"]["holdings_close"], "unrealized"),
           _gold(s["doc"]["money"]["metals"], "open_gain"), _gold(s["doc"]["money"]["funds"]["rows"], "profit"), _gold(s["doc"]["holdings"], "gain"),
           _gold(s["alldays"]["holdings"], "profit"),
           _gold(s["live"]["positions"], "open_pnl"), s["live"]["open"]["pnl"], s["live"]["summary"]["live_profit"],
           s["day"]["money_summary"]["live_profit"], s["doc"]["money"]["funds"]["total"]["profit"]}
    assert pnl == {1347.65}
    values = {_gold(s["day"]["holdings_close"], "value"), _gold(s["doc"]["holdings"], "value"), _gold(s["alldays"]["holdings"], "value"),
              _gold(s["live"]["positions"], "value"), _gold(s["doc"]["money"]["metals"], "value"), s["doc"]["alloc"]["GOLDBEES"],
              _gold(s["doc"]["money"]["funds"]["rows"], "value")}
    assert values == {50754.01}
    assert {_gold(s["doc"]["holdings"], "price"), _gold(s["alldays"]["holdings"], "price"), _gold(s["day"]["holdings_close"], "close"),
            _gold(s["live"]["positions"], "price"), s["alldays"]["prices"]["GOLDBEES"]["close"]} == {116.2}


def test_one_booked_and_interest_everywhere(surfaces):
    s = surfaces
    ledger_sells = D(sum(Decimal(str(t["pnl"])) for t in s["pf"]["trades"] if t["side"] == "SELL"))
    assert ledger_sells == D("-1166.15") == D(s["pf"]["realised_pnl"])
    assert {D(x["breakdown"]["booked"]) for x in (s["day"]["money_summary"], s["doc"]["money"], s["live"]["summary"])} == {ledger_sells}
    assert D(_gold(s["day"]["holdings_view"]["rows"], "realised")) == ledger_sells
    assert {x["breakdown"]["interest"] for x in (s["day"]["money_summary"], s["doc"]["money"], s["live"]["summary"])} == {146.82}


def test_overall_is_open_plus_booked_plus_interest(surfaces):
    s = surfaces
    for ms in (s["day"]["money_summary"], s["doc"]["money"], s["live"]["summary"]):
        b = ms["breakdown"]
        assert D(b["open"]) + D(b["booked"]) + D(b["interest"]) == D(ms["overall_profit"]) == D(ms["total_now"]) - 100000 == D("328.32")
    assert s["live"]["total"]["pnl"] == s["alldays"]["overall_profit"] == s["doc"]["money"]["overall_profit"] == 328.32


def test_overall_reconciles_for_random_books():
    rnd = random.Random(7)
    for _ in range(300):
        trades, cash, held = [], 100000.0, {}
        for k in range(rnd.randint(0, 6)):
            sym = rnd.choice(["GOLDBEES", "SILVERBEES", "HINDCOPPER"])
            qty, px, costs = rnd.randint(1, 400), rnd.uniform(90, 450), rnd.uniform(5, 40)
            if held.get(sym) and rnd.random() < 0.5:
                q0 = held.pop(sym)
                trades.append({"ts": f"2026-10-{10 + k}T10:00:00+05:30", "asset": sym, "side": "SELL", "qty": q0["qty"], "price": px,
                               "value": q0["qty"] * px, "costs": costs, "pnl": rnd.uniform(-900, 900)})
                cash += q0["qty"] * px - costs
            elif sym not in held:
                cash -= qty * px + costs
                held[sym] = {"qty": qty, "avg_cost": px, "buy_costs": costs, "last_price": px * rnd.uniform(0.9, 1.1)}
                trades.append({"ts": f"2026-10-{10 + k}T09:30:00+05:30", "asset": sym, "side": "BUY", "qty": qty, "price": px,
                               "value": qty * px, "costs": costs, "pnl": 0.0})
        pf = {"cash": cash, "liquidbees": 0.0, "positions": held, "trades": trades}
        ms = summary.money_summary(pf, {s: p["last_price"] for s, p in held.items()}, [("2026-10-30", 100000.0)], "2026-11-02",
                                   config.load_params())
        b = ms["breakdown"]
        assert D(b["open"]) + D(b["booked"]) + D(b["interest"]) == D(ms["overall_profit"]) == D(ms["total_now"]) - 100000
        assert D(b["open"]) == D(ms["live_profit"]) == sum(D(m["open_gain"]) for m in ms["metals"])


# ------------------------------------------------------------------------------------------------ numeric trend levels
def test_dashboard_and_report_targets_carry_numeric_levels_for_6_nov(surfaces):
    t = {x["symbol"]: x for x in surfaces["doc"]["targets"]}
    g, s, c = t["GOLDBEES"], t["SILVERBEES"], t["HINDCOPPER"]
    # gold: ON level = SMA200 123.86 x (1 + 0.005 band) = 124.48; `close` is the 15:10 check price the test was decided on
    assert (g["level_kind"], g["level_raw"], g["level"], g["level_side"], g["close"]) == ("sma200", 123.86, 124.48, "on", 116.13)
    assert g["gap_pct"] == 7.19 and g["weight_if_on"] == 71.4
    assert (s["level_kind"], s["level"], s["close"], s["gap_pct"], s["weight_if_on"]) == ("donchian55", 234.28, 174.57, 34.2, 41.6)
    assert (c["level_kind"], c["level"], c["close"], c["gap_pct"], c["weight_if_on"]) == ("donchian55", 584.53, 421.45, 38.69, 35.0)
    rep = {x["symbol"]: x for x in surfaces["day"]["targets"]}
    for k in ("level", "level_kind", "close", "gap_pct", "weight_if_on"):
        assert {sym: rep[sym][k] for sym in rep} == {sym: t[sym][k] for sym in t}


def test_target_numbers_prefer_the_engines_own_fields():
    st = config.load_params()["strategy"]
    rec = {"symbol": "GOLDBEES", "price": 1.0, "state": "OFF", "sigma": 0.2, "reason": "close 1 < SMA200 999",
           "level_kind": "sma200", "level_raw": 100.0, "level": 100.5, "level_side": "on", "close": 90.0, "gap_pct": 11.67, "weight_if_on": 70.0}
    assert trend.level_numbers_from_record(rec, st)["level"] == 100.5
    assert trend.level_numbers_from_record({"symbol": "GOLDBEES", "reason": "nothing here"}, st) == {}


def _info(kind, on, close, row, sigma=0.2):
    return trend.TrendInfo("GOLDBEES" if kind == "sma_trend" else "SILVERBEES", kind, on, on, close, sigma, None, None, row)


def test_level_numbers_sma_band_both_sides():
    st = config.load_params()["strategy"]
    b = st["sma_hysteresis"]
    off = trend.level_numbers(_info("sma_trend", False, 116.13, {"sma": 123.86}, 0.1961), st)
    assert off["level"] == round(123.86 * (1 + b), 2) and off["level_side"] == "on"
    assert off["gap_pct"] == round((round(123.86 * (1 + b), 2) / 116.13 - 1) * 100, 2) or abs(off["gap_pct"] - 7.19) < 0.01
    on = trend.level_numbers(_info("sma_trend", True, 130.0, {"sma": 123.86}, 0.1961), st)
    assert on["level"] == round(123.86 * (1 - b), 2) and on["level_side"] == "off" and on["gap_pct"] < 0
    assert off["weight_if_on"] == round(min(st["symbols"]["GOLDBEES"]["cap"], st["vol_target"] / 0.1961) * 100, 1)


def test_level_numbers_donchian_and_missing_history():
    st = config.load_params()["strategy"]
    off = trend.level_numbers(_info("donchian", False, 174.57, {"hh": 234.28}, 0.3368), st)
    assert (off["level_kind"], off["level"], off["level_side"], off["gap_pct"]) == ("donchian55", 234.28, "on", 34.2)
    on = trend.level_numbers(_info("donchian", True, 240.0, {"hh": 234.28, "stop": 228.5}), st)
    assert on["level"] == 228.5 and on["level_side"] == "off" and on["gap_pct"] < 0
    none = trend.level_numbers(_info("donchian", False, 100.0, {}, None), st)
    assert none["level"] is None and none["gap_pct"] is None and none["weight_if_on"] is None


def test_engine_target_records_carry_the_numbers(tmp_path):
    from test_engine_v2 import SYMS, all_decisions, run_days, series
    closes = {s: series("up", s) for s in SYMS}
    _, env = run_days(tmp_path, closes, last=300)
    tg = {d["symbol"]: d for d in all_decisions(env) if d["action"] == "TARGET"}
    assert set(tg) == set(SYMS)
    st = env.params["strategy"]
    for sym, d in tg.items():
        assert d["level_kind"] == ("sma200" if sym == "GOLDBEES" else "donchian55")
        assert d["level_side"] == "off" and d["state"] == "ON"          # a rising series: trend ON, so the level is where it turns OFF
        assert d["close"] == pytest.approx(d["price"], abs=0.006) and d["level"] < d["close"] and d["gap_pct"] < 0
        assert d["weight_if_on"] == round(min(st["symbols"][sym]["cap"], st["vol_target"] / d["sigma"]) * 100, 1)
        assert trend.level_numbers_from_record(d, st)["level"] == d["level"]


# ------------------------------------------------------------------------------------------------ review round 1
NODE_DRIVER = r"""
const vm = require('vm'), fs = require('fs');
const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const els = {};
const el = sel => els[sel] || (els[sel] = {innerHTML: '', textContent: '', className: '', scrollLeft: 0, scrollWidth: 0, dataset: {}, style: {},
  classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }}, setAttribute() {}, removeAttribute() {}, firstChild: null,
  querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; }});
const doc = {querySelector: el, querySelectorAll: () => [], addEventListener() {}, hidden: false, body: el('body'),
  documentElement: {getAttribute() { return null; }, setAttribute() {}, removeAttribute() {}}};
const ctx = vm.createContext({document: doc, location: {hash: '#static'}, setInterval() {}, setTimeout() {}, clearTimeout() {}, console,
  Date, Math, JSON, Object, Array, Number, String, isFinite, parseFloat, Promise, fetch: undefined, __LIVE: cfg.live});
vm.runInContext(cfg.script, ctx);
const out = vm.runInContext('LIVE = __LIVE; renderTop(); const v = view(); JSON.stringify({v: v, top: $("#top").innerHTML, hold: $("#hold").innerHTML})', ctx);
process.stdout.write(out);
"""


def _run_template(tmp_path, alldays_doc, live):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = (Path(__file__).resolve().parent.parent / "docs" / "dashboard" / "template.html").read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    script = script.replace("__DATA__", json.dumps(alldays_doc))
    (tmp_path / "cfg.json").write_text(json.dumps({"script": script, "live": live}), encoding="utf-8")
    (tmp_path / "drive.js").write_text(NODE_DRIVER, encoding="utf-8")
    r = subprocess.run([node, str(tmp_path / "drive.js"), str(tmp_path / "cfg.json")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_dashboard_page_shows_the_feeds_numbers_not_its_own(surfaces, tmp_path):
    """The page used to recompute value = qty x price (gross), gain and cash from the live file: 50,779.40 / +1,373.04 / 49,548.92."""
    s = surfaces
    out = _run_template(tmp_path, s["alldays_full"], s["live"])
    v = out["v"]
    g = next(h for h in v["holdings"] if h["symbol"] == "GOLDBEES")
    assert v["live"] is True
    assert (g["value"], g["gain"]) == (50754.01, 1347.65) == (_gold(s["live"]["positions"], "value"), _gold(s["live"]["positions"], "open_pnl"))
    assert v["alloc"]["CASH"] == s["live"]["cash"] == 49574.31
    assert v["openGain"] == s["live"]["summary"]["live_profit"] == 1347.65
    assert v["total"] == s["live"]["total_value"] == 100328.32
    assert round(sum(v["alloc"].values()), 2) == v["total"]
    assert "1,348" in out["top"] and "1,373" not in out["top"] + out["hold"]          # the old gross gain must not show up anywhere


def test_dashboard_page_falls_back_for_an_old_feed(surfaces, tmp_path):
    old = json.loads(json.dumps(surfaces["live"]))
    for p in old["positions"]:
        p.pop("value"), p.pop("open_pnl")
    old.pop("cash"), old["summary"].pop("live_profit")
    g = next(h for h in _run_template(tmp_path, surfaces["alldays_full"], old)["v"]["holdings"] if h["symbol"] == "GOLDBEES")
    assert g["value"] == round(437 * 116.2, 2) or abs(g["value"] - 50779.4) < 0.01       # an old feed has nothing better than qty x price


def test_closed_day_with_a_position_held_uses_the_carried_price_and_real_cash(tmp_path, shipped_config):
    """10 Nov 2026 was a holiday with 437 GOLDBEES held: the report priced it at its average cost and invented cash."""
    root = tmp_path / "r"
    shutil.copytree(FIX, root)
    day = report.build_day("2026-11-10", root, holiday="Test holiday")
    assert day["closed"] is True
    ms = day["money_summary"]
    assert ms["total_now"] == 100328.32 and ms["cash"] == 49574.31
    assert ms["metals"][0]["price"] == 116.2 and ms["live_profit"] == 1347.65 and ms["metals"][0]["value"] == 50754.01
    assert ms["breakdown"]["interest"] == 146.82 and ms["breakdown"]["booked"] == -1166.15 and ms["overall_profit"] == 328.32
    hv = day["holdings_view"]
    assert hv["total_value"] == 100328.32
    assert _gold(hv["rows"], "price") == 116.2 and _gold(hv["rows"], "gain") == 1347.65 and _gold(hv["rows"], "value") == 50754.01
    assert next(r["value"] for r in hv["rows"] if r["symbol"] == "LIQUIDBEES") == 49574.31


def test_5_nov_snapshot_is_paise_exact(tmp_path, shipped_config):
    """Close 101,162.58 = cash 49,574.31 + gold 51,588.27; open +2,181.91; interest 146.82 (the exact total is ...57.65, which rounds up)."""
    root = tmp_path / "r"
    shutil.copytree(FIX.parent / "2026-11-05", root)
    day = report.build_day("2026-11-05", root)
    ms = day["money_summary"]
    assert ms["total_now"] == 101162.58 and ms["cash"] == 49574.31
    assert ms["metals"][0]["value"] == 51588.27 and ms["live_profit"] == 2181.91
    assert ms["breakdown"] == {"booked": -1166.15, "open": 2181.91, "interest": 146.82, "sum": 1162.58, "costs_in_booked": -46.52}
    assert D(ms["cash"]) + sum(D(m["value"]) for m in ms["metals"]) == D(ms["total_now"]) == D(day["portfolio"]["close"])
    assert day["holdings_view"]["total_value"] == 101162.58
    assert _gold(day["holdings_view"]["rows"], "gain") == _gold(day["holdings_close"], "unrealized") == 2181.91
    doc = dashboard.build("2026-11-05", root)
    assert round(sum(doc["alloc"].values()), 2) == 101162.58 and doc["alloc"]["LIQUIDBEES"] == 49574.31 and _gold(doc["holdings"], "gain") == 2181.91


HELD_DAYS = ["2026-10-21", "2026-10-22", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]


def test_alldays_cash_and_holdings_add_up_to_the_close_on_every_held_day(tmp_path):
    root = tmp_path / "r"
    shutil.copytree(FIX.parent / "held-days", root)
    days = {d["date"]: d for d in alldays.build(root)["days"]}
    assert [d for d in HELD_DAYS if d not in days] == []
    for date in HELD_DAYS:
        d = days[date]
        real_cash = alldays.tick_cash(root / "logs" / f"ticks-{date}.csv")
        assert d["invested_value"] > 0
        assert D(d["cash_value"]) == D(real_cash), date                                  # the engine's real cash, not a plug
        assert D(d["cash_value"]) + D(d["invested_value"]) == D(d["close_value"]), date
        assert sum(D(h["value"]) for h in d["holdings"]) == D(d["invested_value"]), date
        assert all(D(h["profit"]) == D(h["value"]) - D(h["cost"]) for h in d["holdings"]), date
        assert D(sum(Decimal(str(v)) for v in d["alloc"].values())) == D(d["close_value"]), date
    assert days["2026-11-05"]["holdings"][0]["profit"] == 2181.91 and days["2026-11-05"]["cash_value"] == 49574.31


def test_settle_adds_up_exactly_and_keeps_real_cash():
    rnd = random.Random(11)
    for _ in range(500):
        nets = {s: rnd.uniform(1000, 60000) for s in rnd.sample(["GOLDBEES", "SILVERBEES", "HINDCOPPER"], rnd.randint(0, 3))}
        cash = rnd.uniform(0, 60000)
        close = round(cash + sum(nets.values()) + rnd.uniform(-0.006, 0.006), 2)
        vals, cash_p, total_p = summary.settle(nets, cash, close)
        assert total_p == round(close * 100) and cash_p + sum(vals.values()) == total_p
        if nets:
            assert cash_p == summary._p(cash) and all(abs(vals[s] - nets[s] * 100) < 2.0 for s in nets)
    vals, cash_p, total_p = summary.settle({"GOLDBEES": 1000.0}, None, 5000.0)                  # unknown cash: it is what is left
    assert (vals, cash_p, total_p) == ({"GOLDBEES": 100000}, 400000, 500000)
    vals, cash_p, total_p = summary.settle({"GOLDBEES": 1000.0}, 100.0, 90000.0)                # a state that is not that day's: plain rounding
    assert (vals, cash_p) == ({"GOLDBEES": 100000}, 8900000)


def _random_live(rnd):
    held, trades, cash = {}, [], rnd.uniform(1000, 60000)
    for sym in rnd.sample(["GOLDBEES", "SILVERBEES", "HINDCOPPER"], rnd.randint(1, 3)):
        px = rnd.uniform(90, 450)
        held[sym] = {"qty": rnd.randint(1, 400), "avg_cost": px, "buy_costs": rnd.uniform(5, 40), "opened": "2026-10-20",
                     "last_price": px * rnd.uniform(0.9, 1.1)}
    if rnd.random() < 0.5:
        trades.append({"ts": "2026-10-19T10:00:00+05:30", "asset": "SILVERBEES", "side": "SELL", "qty": 10, "price": 200.0, "value": 2000.0,
                       "costs": 5.0, "pnl": rnd.uniform(-300, 300)})
    return {"cash": cash, "liquidbees": 0.0, "positions": held, "trades": trades}


def test_live_feed_adds_up_to_the_paisa_on_random_books():
    """Fails if livepnl goes back to its own float sums: positions + cash = total, open = sum of positions, parts = overall."""
    rnd = random.Random(5)
    params = config.load_params()
    for _ in range(200):
        pf = _random_live(rnd)
        prices = {s: p["last_price"] for s, p in pf["positions"].items()}
        d = livepnl.build(pf, prices, {}, [("2026-10-30", 100000.0)], params, datetime(2026, 11, 2, 11, 0, tzinfo=IST), [], market_open=True)
        sm = d["summary"]
        assert D(d["total_value"]) == D(sm["total_now"]) == D(d["cash"]) + sum(D(p["value"]) for p in d["positions"])
        assert D(d["open"]["pnl"]) == D(sm["live_profit"]) == sum(D(p["open_pnl"]) for p in d["positions"])
        assert all(D(p["open_pnl"]) == D(p["value"]) - D(p["invested"]) for p in d["positions"])
        b = sm["breakdown"]
        assert D(b["open"]) + D(b["booked"]) + D(b["interest"]) == D(d["total"]["pnl"]) == D(d["total_value"]) - 100000


def test_a_missing_trades_archive_is_flagged_not_called_interest(caplog):
    """Interest is a residual: sells that are not in the trade list would silently show up as 'past interest'."""
    pf = {"cash": 99000.0, "liquidbees": 0.0, "positions": {}, "realised_pnl": -1166.15, "trades": [
        {"ts": "2026-10-23T09:16:00+05:30", "asset": "GOLDBEES", "side": "SELL", "qty": 1, "price": 100.0, "value": 100.0, "costs": 1.0, "pnl": -100.0}]}
    with caplog.at_level(logging.WARNING, logger="simgold.summary"):
        ms = summary.money_summary(pf, {}, [("2026-10-30", 99000.0)], "2026-11-02", config.load_params())
    assert ms["breakdown"]["ledger_gap"] == -1066.15 and "realised_pnl" in caplog.text
    pf["trades"][0]["pnl"] = -1166.15
    assert "ledger_gap" not in summary.money_summary(pf, {}, [("2026-10-30", 99000.0)], "2026-11-02", config.load_params())["breakdown"]


def test_trend_numbers_survive_odd_input():
    np = pytest.importorskip("numpy")
    st = config.load_params()["strategy"]
    i = _info("sma_trend", False, np.float32(116.13), {"sma": np.float32(123.86)}, np.float32(0.1961))
    n = trend.level_numbers(i, st)
    assert n["level"] == 124.48 and n["level_raw"] == 123.86 and n["close"] == 116.13 and n["gap_pct"] == 7.19      # np.float32 is not None
    rec = {"symbol": "GOLDBEES", "state": "OFF", "sigma": 0.2, "price": 116.13, "reason": "close 116.13 < SMA200 123.86."}
    assert trend.level_numbers_from_record(rec, st)["level_raw"] == 123.86                                       # a sentence-final dot
    for bad in ({"symbol": "GOLDBEES", "reason": "SMA200 1.2.3", "price": "x", "sigma": "y"}, {"reason": None}, {"symbol": 3, "reason": "55-day high ."}):
        assert isinstance(trend.level_numbers_from_record(bad, st), dict)
