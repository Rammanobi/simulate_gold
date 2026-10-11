from datetime import datetime

from simgold.brain import apply_review, decide
from simgold.broker import Portfolio, trade_costs


def snap(price, tech=0.8, macro=0.5, atr=1.0, vol=0.12):
    return {"price": price, "tech": tech, "macro": macro, "atr": atr, "vol": vol, "rsi": 55, "trend": 1, "mom": 0.5}


def test_strong_signal_buys_within_caps():
    pf = Portfolio()
    orders, rep = decide(pf, {"GOLD": snap(100), "SILVER": snap(200), "COPPER": snap(400)}, datetime(2026, 10, 12, 10))
    assert {o["asset"] for o in orders} == {"GOLD", "SILVER", "COPPER"}
    for o in orders:
        pf.execute(o["asset"], o["side"], o["qty"], o["price"], datetime(2026, 10, 12, 10), o["reason"])
    w = pf.weights({"GOLD": 100, "SILVER": 200, "COPPER": 400})
    assert w["GOLD"] <= 0.5 + 1e-9 and w["COPPER"] <= 0.2 + 1e-9
    assert pf.cash >= 0.09 * pf.equity({"GOLD": 100, "SILVER": 200, "COPPER": 400})


def test_trailing_stop_exits():
    pf = Portfolio()
    pf.execute("GOLD", "BUY", 100, 100, datetime(2026, 10, 12, 10), "t")
    pf.positions["GOLD"].high_water = 110
    orders, _ = decide(pf, {"GOLD": snap(105, atr=1.0)}, datetime(2026, 10, 13, 10))
    assert orders and orders[0]["side"] == "SELL" and orders[0]["qty"] == 100 and orders[0]["protective"]


def test_negative_score_stays_flat():
    orders, _ = decide(Portfolio(), {"GOLD": snap(100, tech=-0.5, macro=-0.5)}, datetime(2026, 10, 12, 10))
    assert orders == []


def test_review_can_only_shrink():
    orders = [{"asset": "GOLD", "side": "BUY", "qty": 10, "price": 1, "reason": "x", "protective": False},
              {"asset": "SILVER", "side": "SELL", "qty": 5, "price": 1, "reason": "stop", "protective": True}]
    out = apply_review(orders, {"decisions": [{"asset": "GOLD", "scale": 5, "reason": "?"},
                                              {"asset": "SILVER", "scale": 0, "reason": "?"}]})
    assert out[0]["qty"] == 10 and out[1]["qty"] == 5


def test_costs_positive_and_equity_pays_stt():
    assert trade_costs(10000, "BUY", "equity") > trade_costs(10000, "BUY", "etf") > 0
