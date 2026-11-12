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
import random
import shutil
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
    return {"root": root, "day": day, "doc": doc, "alldays": all_days["days"][-1], "live": live, "pf": pf, "prices": prices}


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
