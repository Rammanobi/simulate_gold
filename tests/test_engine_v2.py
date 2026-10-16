"""Strategy v2 through the real minute tick (simgold.engine.run_tick): rising series ends invested, falling series ends in
LIQUIDBEES, decisions carry the formula numbers, blocks delay buys but never exits, the account stop, v1 rollback."""
from __future__ import annotations

import copy
import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from conftest import v2_params
from simgold import config, trend
from simgold.broker import Portfolio, Position
from simgold.engine import Env, Market, run_tick
from simgold.market import IST

SYMS = ["GOLDBEES", "SILVERBEES", "HINDCOPPER"]
START = {"GOLDBEES": 100.0, "SILVERBEES": 200.0, "HINDCOPPER": 400.0}
DAYS = pd.bdate_range("2025-01-01", periods=420)


def series(kind: str, sym: str, n: int = 420, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed + len(sym))
    if kind == "up":
        r = 0.0012 + rng.normal(0, 0.004, n)
    elif kind == "down":
        r = -0.0012 + rng.normal(0, 0.004, n)
    elif kind == "up_then_crash":
        r = np.where(np.arange(n) < 330, 0.0015, -0.012) + rng.normal(0, 0.004, n)
    else:
        raise ValueError(kind)
    return START[sym] * np.cumprod(1 + r)


def make_env(tmp_path, params=None) -> Env:
    return Env(state_dir=tmp_path / "state", logs_dir=tmp_path / "logs", params=params or v2_params(), environ={},
               match_headline=None)


def fresh_pf(cash=100000.0) -> Portfolio:
    pf = Portfolio()
    pf.cash, pf.liquidbees = 0.0, cash
    return pf


def tick(pf, env, closes: dict, i: int, hh=15, mm=10, hist_len=None):
    """One engine tick on day index i: prices are closes[sym][i]; the daily history is the bars before day i."""
    day = DAYS[i].to_pydatetime()
    now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)
    prices = {s: float(closes[s][i]) for s in SYMS}
    hist = {}
    for s in SYMS:
        c = closes[s][:i]
        hist[s] = trend.clean_bars(c.tolist(), (c * 1.004).tolist(), (c * 0.996).tolist(),
                                   [d.date().isoformat() for d in DAYS[:i]])
    mkt = Market(now=now, prices=prices, price_ts={s: now for s in SYMS}, scores={}, prev_close={s: float(closes[s][i - 1]) for s in SYMS},
                 hist=hist)
    return run_tick(pf, mkt, env)


def run_days(tmp_path, closes, first=260, last=419, params=None, pf=None):
    env = make_env(tmp_path, params)
    pf = pf or fresh_pf()
    for i in range(first, last + 1):
        tick(pf, env, closes, i)
    return pf, env


def weights(pf):
    v = pf.cash + pf.liquidbees + sum(p.qty * p.last_price for p in pf.positions.values())
    return {s: pf.positions[s].qty * pf.positions[s].last_price / v if s in pf.positions else 0.0 for s in SYMS}, v


def read_decisions(env, day_index):
    f = env.logs_dir / f"decisions-{DAYS[day_index].date().isoformat()}.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def all_decisions(env):
    out = []
    for f in sorted(env.logs_dir.glob("decisions-*.jsonl")):
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    return out


# ------------------------------------------------------------------ rising and falling series
def test_rising_series_ends_invested(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    pf, env = run_days(tmp_path, closes)
    w, v = weights(pf)
    assert w["GOLDBEES"] > 0.3 and sum(w.values()) > 0.85, w          # invested, never levered
    assert sum(w.values()) <= 1.02
    assert v > 100000
    assert pf.floor == 75000                                           # v2: the "floor" is the hard account stop
    pf.save(tmp_path / "pf.json")                                      # the v2 runtime state is plain JSON and reloads
    again = Portfolio.load(tmp_path / "pf.json")
    assert again.runtime["v2"]["brake"]["level"] == 0 and again.runtime["v2"]["done"] == DAYS[419].date().isoformat()
    buys = [d for d in all_decisions(env) if d["action"] == "BUY"]
    assert buys and all(d["trigger"] == "Trend check" and "trend ON (close" in d["reason"] for d in buys)


def test_falling_series_stays_in_liquidbees(tmp_path):
    closes = {s: series("down", s) for s in SYMS}
    pf, env = run_days(tmp_path, closes)
    assert not pf.positions and pf.cash == 0.0
    assert pf.liquidbees > 100000                                      # only LIQUIDBEES interest
    dec = all_decisions(env)
    assert not [d for d in dec if d["action"] in ("BUY", "SELL")]
    tg = [d for d in dec if d["action"] == "TARGET"]
    assert tg and all(d["state"] == "OFF" and d["target_pct"] == 0.0 and d["actual_pct"] == 0.0 for d in tg)


def test_crash_exits_everything_to_liquidbees(tmp_path):
    closes = {s: series("up_then_crash", s) for s in SYMS}
    pf, env = run_days(tmp_path, closes)
    assert not pf.positions
    sells = [d for d in all_decisions(env) if d["action"] == "SELL"]
    assert sells and any(d["trigger"] == "Trend exit" and "trend OFF" in d["reason"] and "selling all" in d["reason"] for d in sells)
    assert weights(pf)[1] > 70000                                      # the crash was mostly avoided


def test_decisions_state_the_formula_numbers(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    pf, env = run_days(tmp_path, closes, last=300)
    buy = next(d for d in all_decisions(env) if d["action"] == "BUY" and d["symbol"] == "GOLDBEES")
    r = buy["reason"]
    assert r.startswith("GOLDBEES trend ON (close ") and "> SMA200 " in r and "target " in r and "= min(1" in r
    assert "0.14/" in r and "buying " in r and "units to reach target" in r
    tg = next(d for d in all_decisions(env) if d["action"] == "TARGET" and d["symbol"] == "SILVERBEES")
    assert "sigma60" in tg["reason"] and "actual" in tg["reason"]
    assert set(("state", "sigma", "target_pct", "actual_pct")) <= set(tg)


def test_one_daily_check_only(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    env = make_env(tmp_path)
    pf = fresh_pf()
    tick(pf, env, closes, 300, 14, 0)                                  # before the check time: nothing
    assert not read_decisions(env, 300)
    tick(pf, env, closes, 300, 15, 10)
    n = len(read_decisions(env, 300))
    assert n >= 3
    tick(pf, env, closes, 300, 15, 11)                                 # already done today
    tick(pf, env, closes, 300, 15, 26)
    assert len(read_decisions(env, 300)) == n


def test_minute_checks_say_the_v2_reason(tmp_path):
    closes = {s: series("down", s) for s in SYMS}
    env = make_env(tmp_path)
    tick(fresh_pf(), env, closes, 300, 11, 0)
    rec = json.loads((env.logs_dir / f"checks-{DAYS[300].date().isoformat()}.jsonl").read_text().splitlines()[-1])
    txt = rec["checks"]["GOLDBEES"]
    assert txt.startswith("Price ₹") and "Trend OFF: close " in txt and "< SMA200 " in txt and "target 0%" in txt
    assert "Trend OFF: close " in rec["checks"]["SILVERBEES"] and "-day high" in rec["checks"]["SILVERBEES"]


# ------------------------------------------------------------------ blocks, bias, stops
def test_price_shock_block_delays_buy_but_not_exit(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    env = make_env(tmp_path)
    pf = fresh_pf()
    pf.cooldowns["block_until"] = {"GOLDBEES": datetime(2030, 1, 1, tzinfo=IST).isoformat()}
    tick(pf, env, closes, 300)
    assert "GOLDBEES" not in pf.positions and "SILVERBEES" in pf.positions
    assert any(d["action"] == "BLOCK" and d["symbol"] == "GOLDBEES" and "paused" in d["reason"] for d in all_decisions(env))
    # an existing position whose trend is OFF is still sold while buys are paused
    falling = {s: series("down", s) for s in SYMS}
    pf2 = fresh_pf(50000.0)
    pf2.positions["GOLDBEES"] = Position(qty=400, avg_cost=100.0, opened="2025-01-01", last_price=100.0)
    pf2.liquidbees = 100000.0 - 400 * 100.0
    pf2.cooldowns["block_until"] = {"GOLDBEES": datetime(2030, 1, 1, tzinfo=IST).isoformat(), "ALL": datetime(2030, 1, 1, tzinfo=IST).isoformat()}
    env2 = make_env(tmp_path / "b")
    tick(pf2, env2, falling, 300)
    assert "GOLDBEES" not in pf2.positions


def write_guidance(env, bias, now):
    env.state_dir.mkdir(parents=True, exist_ok=True)
    (env.state_dir / "guidance.json").write_text(json.dumps({"updated": now.isoformat(), "bias": bias, "note": "t"}))


def test_bias_block_means_no_new_buys(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    env = make_env(tmp_path)
    now = datetime(DAYS[300].year, DAYS[300].month, DAYS[300].day, 15, 5, tzinfo=IST)
    write_guidance(env, {"GOLD": "block", "SILVER": "neutral", "COPPER": "neutral"}, now)
    pf = fresh_pf()
    tick(pf, env, closes, 300)
    assert "GOLDBEES" not in pf.positions and "SILVERBEES" in pf.positions
    assert any(d["action"] == "SKIP" and d["symbol"] == "GOLDBEES" and "bias" in d["reason"] for d in all_decisions(env))


def test_bias_block_keeps_existing_core_while_trend_on(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    env = make_env(tmp_path)
    pf = fresh_pf()
    tick(pf, env, closes, 300)
    held = pf.positions["GOLDBEES"].qty
    now = datetime(DAYS[301].year, DAYS[301].month, DAYS[301].day, 15, 5, tzinfo=IST)
    write_guidance(env, {"GOLD": "block", "SILVER": "block", "COPPER": "block"}, now)
    tick(pf, env, closes, 301)
    assert pf.positions["GOLDBEES"].qty >= held * 0.95               # not sold because of the block


def test_account_stop_halts_buys_and_alerts(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    env = make_env(tmp_path)
    pf = fresh_pf(70000.0)                                             # under the Rs 75,000 stop
    tick(pf, env, closes, 300)
    assert not pf.positions
    dec = all_decisions(env)
    assert any(d["trigger"] == "Account stop" and d["action"] == "BLOCK" for d in dec)
    alerts_file = env.state_dir / "alerts.json"
    assert alerts_file.exists() and "account_stop" in alerts_file.read_text()


def test_hindcopper_results_exit_and_block(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    env = make_env(tmp_path)
    pf = fresh_pf()
    tick(pf, env, closes, 300)
    assert "HINDCOPPER" in pf.positions
    nxt = DAYS[301].date()
    (env.state_dir).mkdir(parents=True, exist_ok=True)
    (env.state_dir / "events.json").write_text(json.dumps([{"date": DAYS[302].date().isoformat(), "type": "HINDCOPPER_RESULTS"}]))
    tick(pf, env, closes, 301, 14, 56)                                 # day before results, after 14:55
    assert "HINDCOPPER" not in pf.positions
    assert any(d["trigger"] == "Event rule" and d["action"] == "SELL" for d in read_decisions(env, 301))
    tick(pf, env, closes, 301, 15, 10)                                 # the daily check must not buy it back
    assert "HINDCOPPER" not in pf.positions
    assert nxt


def test_brake_level_two_flattens_everything(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    env = make_env(tmp_path)
    pf = fresh_pf()
    tick(pf, env, closes, 300)
    assert pf.positions
    pf.runtime["v2"]["brake"] = {"peak": 200000.0, "level": 0, "reconfirm": []}   # value is far under its peak
    tick(pf, env, closes, 301)
    assert not pf.positions
    dec = read_decisions(env, 301)
    assert any(d["trigger"] == "Drawdown brake" and d["action"] == "SELL" for d in dec)
    assert any(d["action"] == "BLOCK" and "level 2" in d["reason"] for d in dec)


def test_brake_level_one_halves_targets(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    full, _ = run_days(tmp_path / "a", closes, 260, 300)
    wf, vf = weights(full)
    env = make_env(tmp_path / "b")
    pf = fresh_pf()
    pf.runtime["v2"] = {"brake": {"peak": 100000.0 / 0.84, "level": 1, "reconfirm": []}}   # -16 percent from the peak
    for i in range(260, 301):
        tick(pf, env, closes, i)
    wh, _ = weights(pf)
    assert sum(wh.values()) < 0.62 * sum(wf.values()) + 0.05


# ------------------------------------------------------------------ rollback
def test_v1_rollback_path_uses_the_old_rules(tmp_path):
    closes = {s: series("up", s) for s in SYMS}
    p = copy.deepcopy(v2_params())
    p["strategy"]["mode"] = "v1_scores"
    env = make_env(tmp_path, p)
    pf = fresh_pf()
    pf.floor = 99500.0
    tick(pf, env, closes, 300)
    dec = all_decisions(env)
    assert not [d for d in dec if d["action"] == "TARGET" or d["trigger"] in ("Trend check", "Trend exit")]
    assert pf.floor == 99500.0                                         # the Green Guard floor, not the Rs 75,000 stop
    assert not pf.positions                                            # no scores were supplied, so v1 buys nothing


def test_shipped_params_are_v2_aggressive():
    import yaml
    raw = yaml.safe_load(open(config.PARAMS_PATH, encoding="utf-8"))
    st = raw["strategy"]
    assert st["mode"] == "v2_trend" and st["profile"] == "aggressive"
    assert st["vol_target"] == 0.14
    assert {s: v["cap"] for s, v in st["symbols"].items()} == {"GOLDBEES": 1.0, "SILVERBEES": 0.70, "HINDCOPPER": 0.35}
    assert st["rebalance_threshold"] == 0.05 and st["account_stop"] == 75000
    assert (st["brake"]["level1_dd"], st["brake"]["level1_mult"], st["brake"]["level2_dd"]) == (0.15, 0.5, 0.25)
    assert st["entry"]["dip_rsi"] == 5 and st["sma_days"] == 200
    assert st["donchian_entry_days"] == 55 and st["donchian_exit_days"] == 20 and st["atr_mult"] == 3.0
    assert raw["instruments"]["GOLDBEES"]["cap_pct"] == 0.60          # the v1 rules are kept intact for the rollback
