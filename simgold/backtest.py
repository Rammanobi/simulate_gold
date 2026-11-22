"""Daily-bar backtest of the same brain (news layer off — free historical headlines don't exist).
Signals use data up to day t's close; orders fill at day t+1's close (no look-ahead)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .brain import decide
from .broker import Portfolio
from .data import daily_history_robust
from .features import all_tickers, build_frames, snapshot_row


def run(start: str = "2019-01-01", warmup: int = 120) -> dict:
    hist = daily_history_robust(all_tickers(), start=start)
    frames = build_frames(hist)
    idx = sorted(set.intersection(*(set(f.dropna(subset=["close"]).index) for f in frames.values())))
    pf = Portfolio()
    curve, reports = [], []
    for i in range(warmup, len(idx) - 1):
        d, nxt = idx[i], idx[i + 1]
        snap = {a: snapshot_row(f.loc[d]) for a, f in frames.items()}
        orders, rep = decide(pf, snap, d)
        for o in orders:
            pf.execute(o["asset"], o["side"], o["qty"], float(frames[o["asset"]].at[nxt, "close"]), nxt, o["reason"])
        prices_next = {a: float(f.at[nxt, "close"]) for a, f in frames.items()}
        curve.append((nxt, pf.equity(prices_next), sum(pf.weights(prices_next).values())))
        reports.append(rep)
    eq = pd.DataFrame(curve, columns=["date", "equity", "gross"]).set_index("date")
    gold = frames["GOLD"]["close"].reindex(eq.index)
    bench = config.STARTING_CAPITAL * gold / gold.iloc[0]
    return {"equity": eq, "benchmark_gold": bench, "portfolio": pf, "stats": stats(eq["equity"]),
            "bench_stats": stats(bench), "last_report": reports[-1] if reports else None}


def stats(equity: pd.Series) -> dict:
    rets = equity.pct_change().dropna()
    monthly = equity.resample("ME").last().pct_change().dropna()
    first_m = equity.resample("ME").last().iloc[0] / equity.iloc[0] - 1
    monthly = pd.concat([pd.Series([first_m], index=[equity.resample("ME").last().index[0]]), monthly])
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    dd = equity / equity.cummax() - 1
    return {
        "final_equity": round(float(equity.iloc[-1]), 0),
        "cagr": round(float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1), 4),
        "max_drawdown": round(float(dd.min()), 4),
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(252)), 2) if rets.std() > 0 else 0.0,
        "months": int(len(monthly)),
        "avg_month": round(float(monthly.mean()), 4),
        "median_month": round(float(monthly.median()), 4),
        "pct_months_positive": round(float((monthly > 0).mean()), 3),
        "pct_months_ge_3pct": round(float((monthly >= 0.03).mean()), 3),
        "worst_month": round(float(monthly.min()), 4),
        "best_month": round(float(monthly.max()), 4),
        "monthly": monthly,
    }


# ======================================================================================================================
# Strategy v2 ("trend core"): daily replay of the very same rules the live engine uses (simgold/trend.py).
#   python run_backtest.py --strategy v2 [--data-dir DIR] [--start 2010-06-01] [--set vol_target=0.10 ...]
# The signal uses data up to the close of day t. With lag_days = 1 (default, as in the research) the order fills at the
# close of t+1; the live engine fills at its 15:10 check, so lag 1 is the conservative reading. News layer and Claude's
# bias are off (bias = neutral). Idle money earns the LIQUIDBEES yield.
# ======================================================================================================================
import copy as _copy
from pathlib import Path as _Path

from . import trend as _trend


def load_csv_frames(data_dir, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """The cleaned research CSVs (nse_daily_ohlcv.csv + nse_close_wide.csv) as {"close","high","low"} frames, one column
    per symbol, on the NSE calendar. SILVERBEES before its listing (2022-02-04) is the COMEX x USDINR proxy from the file."""
    d = _Path(data_dir)
    long = pd.read_csv(d / "nse_daily_ohlcv.csv", parse_dates=["date"])
    wide = pd.read_csv(d / "nse_close_wide.csv", parse_dates=["date"], index_col="date")
    cal = wide.index
    out = {f: long.pivot(index="date", columns="symbol", values=f).reindex(cal)[symbols].copy()
           for f in ("high", "low", "close")}
    out["close"] = out["close"].ffill()
    if "SILVER_SPLICED" in wide and "SILVERBEES" in symbols:
        s = wide["SILVER_SPLICED"]
        real = out["close"]["SILVERBEES"].notna()
        out["close"]["SILVERBEES"] = out["close"]["SILVERBEES"].fillna(s)
        r = s.pct_change().abs().fillna(0)
        for f, sg in (("high", 1), ("low", -1)):
            out[f]["SILVERBEES"] = out[f]["SILVERBEES"].where(real, s * (1 + sg * 0.6 * r))
    for f in ("high", "low"):
        out[f] = out[f].where(out[f] > 0).fillna(out["close"])
    out["high"] = np.maximum(out["high"], out["close"])
    out["low"] = np.minimum(out["low"], out["close"])
    op = long.pivot(index="date", columns="symbol", values="open").reindex(cal)[symbols]
    out["open"] = op.where(op > 0)                      # real opens only (0 = none); used by strategy.stops.intraday_exit
    return out


def yahoo_frames(p: dict, start: str) -> dict[str, pd.DataFrame]:
    tick = p["engine"]["tickers"]
    h = daily_history_robust(list(tick.values()), start=start)
    inv = {v: k for k, v in tick.items()}
    out = {k: h[k].rename(columns=inv)[list(tick)] for k in ("close", "high", "low")}
    out["high"] = np.maximum(out["high"], out["close"])
    out["low"] = np.minimum(out["low"], out["close"])
    return out


def apply_overrides(p: dict, sets: list[str] | None) -> dict:
    """A deep copy of the params with `--set a.b.c=value` overrides applied inside the `strategy:` block (value parsed as YAML)."""
    import yaml
    q = _copy.deepcopy(p)
    for item in sets or []:
        key, _, raw = item.partition("=")
        node = q["strategy"]
        parts = key.strip().split(".")
        for k in parts[:-1]:
            node = node[k]
        if parts[-1] not in node:
            raise KeyError(f"strategy.{key} does not exist")
        node[parts[-1]] = yaml.safe_load(raw)
    return q


def load_wide_frames(data_dir, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Research data as four wide CSVs (close.csv, high.csv, low.csv, open.csv; a Date column and one column per symbol), the layout of
    the strategy research folder. SILVERBEES before its listing is the proxy already spliced into the file."""
    d = _Path(data_dir)
    out = {}
    for f in ("close", "high", "low", "open"):
        df = pd.read_csv(d / f"{f}.csv", parse_dates=["Date"], index_col="Date")[symbols].astype(float)
        out[f] = df
    out["close"] = out["close"].ffill()
    for f in ("high", "low"):
        out[f] = out[f].where(out[f] > 0).fillna(out["close"])
    out["high"] = np.maximum(out["high"], out["close"])
    out["low"] = np.minimum(out["low"], out["close"])
    out["open"] = out["open"].where(out["open"] > 0)
    return out


def run_v2(frames: dict[str, pd.DataFrame], p: dict | None = None, start: str | pd.Timestamp = "2010-06-01",
           end: str | pd.Timestamp | None = None, init: dict | None = None, v3: dict | None = None) -> dict:
    """Replay strategy v2 on daily bars. frames: {"close","high","low"}, columns = symbols. Returns
    {"equity": Series, "gross": Series, "trades": DataFrame(date, sym, dv, kind), "brake": Series, "weights": DataFrame}.
    v3: None (strategy v2), or {"cfg": allocation settings, "noise_seed": int | None, "noise_conf": float, "noise_sd": float}: the v3
    allocation pipeline (simgold/allocation.py): D5-FI default with the owner caps, then (with noise_seed) a seeded random "AI" mix
    tilted in at the configured ai_trust, the clamp, the turnover ledger and the risk-sell credit. Costs and fills are unchanged.
    init: optional opening book {"cash": rupees, "hold": {sym: rupees at the first bar's close}} instead of all cash. The first bar
    is then the day the book is struck: it is valued, but takes no orders (they are already in the book) and earns no return."""
    p = p or config.load_params()
    st = p["strategy"]
    syms = list(st["symbols"])
    c = frames["close"]
    tbl = {s: _trend.frame(c[s], frames["high"][s], frames["low"][s], st["symbols"][s]["signal"], st) for s in syms}
    idx = c.index[c.index >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    rets = c.pct_change().fillna(0.0)
    cost = st["backtest"]["cost_per_side"]
    lag = int(st["backtest"]["lag_days"])
    y = p["capital"]["liquid_yield_annual"]
    cap0 = float(p["capital"]["start"])
    hold = {s: 0.0 for s in syms}
    # stop-loss package D5-S: the trend exit level of the intraday-exit symbols is enforced on the day's open / low
    # (the level is known before the open; it fills at min(open, level) less the extra stop slippage; no re-entry before
    # a close above the prior 55-day high). Absent or [] = the older daily-close behaviour, byte for byte.
    scfg = st.get("stops") or {}
    xsyms = [s for s in (scfg.get("intraday_exit") or []) if s in syms]
    extra_slip = float(scfg.get("backtest_slippage", 0.001))
    xlvl = {s: _trend.next_exit_levels(tbl[s], frames["low"][s], st).shift(1) for s in xsyms}
    xopen = frames.get("open")
    xstop = {s: False for s in xsyms}
    cash, brake, pending = cap0, None, []
    if init is not None:
        cash = float(init["cash"])
        for s_, v_ in (init.get("hold") or {}).items():
            if s_ in hold:
                hold[s_] = float(v_)
    eq, gross, wlog, blog, tlog = [], [], [], [], []
    prev = None
    from . import allocation as _alloc
    a3 = v3["cfg"] if v3 else None
    ledger3: dict[str, float] = {}
    credit3 = 0.0
    rng3 = np.random.default_rng(v3["noise_seed"]) if v3 and v3.get("noise_seed") is not None else None
    dst = dict(st, rebalance_threshold=a3["rebalance_threshold"]) if v3 else st
    iso = [x.date().isoformat() for x in c.index]
    pos3 = {x: i for i, x in enumerate(c.index)}

    def execute(d, orders, V):
        nonlocal cash
        for sym, to_w, kind in sorted(orders, key=lambda o: o[1] > 0 and o[2] != "trim"):    # sells first
            if np.isnan(c.at[d, sym]):
                continue
            dv = to_w * V - hold[sym] if kind != "exit" else -hold[sym]
            if abs(dv) < 1e-6:
                continue
            if dv > 0:
                dv = min(dv, max(0.0, cash) / (1 + cost[sym]))
                if dv <= 0:
                    continue
            hold[sym] += dv
            cash -= dv + abs(dv) * cost[sym]
            tlog.append((d, sym, dv, kind))

    for d in idx:
        if prev is not None:
            cash *= (1 + y / 365.0) ** (d - prev).days
            stopped = set()
            for s in xsyms:
                lv = xlvl[s].at[d] if d in xlvl[s].index else np.nan
                if hold[s] <= 1e-6 or np.isnan(lv) or np.isnan(c.at[prev, s]):
                    continue
                op = float(xopen.at[d, s]) if xopen is not None and not np.isnan(xopen.at[d, s]) else np.nan
                lo = frames["low"].at[d, s]
                if not np.isnan(op) and op <= lv:
                    fill = op                                           # a gap under the level: sold at the open
                elif not np.isnan(lo) and lo <= lv:
                    fill = lv                                           # touched during the day: sold at the level
                else:
                    continue
                proceeds = hold[s] * fill * (1 - extra_slip) / c.at[prev, s]
                cash += proceeds * (1 - cost[s])
                if v3:                                                  # a risk sell: its cash may be re-invested outside the turnover budget
                    credit3 += proceeds * (1 - cost[s]) / max(cash + sum(hold.values()) - hold[s], 1.0)
                tlog.append((d, s, -hold[s], "stop"))
                hold[s] = 0.0
                stopped.add(s)
                xstop[s] = True
            if stopped:
                pending = [o for o in pending if o[0] not in stopped]
            for s in syms:
                hold[s] *= 1 + rets.at[d, s]
        V = cash + sum(hold.values())
        if pending:                                   # orders decided at the previous close, filled at this close
            execute(d, pending, V)
            pending = []
            V = cash + sum(hold.values())
        rows = {s: tbl[s].loc[d] for s in syms}
        valid = {s: not np.isnan(c.at[d, s]) and not np.isnan(rows[s]["sigma"]) for s in syms}
        on = {s: bool(rows[s]["on"]) and valid[s] for s in syms}
        for s in xsyms:
            if xstop[s]:
                hh = rows[s]["hh"]
                if not on[s]:
                    xstop[s] = False                                    # the daily rule confirms the exit
                elif not np.isnan(hh) and c.at[d, s] > hh:
                    xstop[s] = False                                    # a new 55-day high: re-armed
                else:
                    on[s] = False                                       # stopped out: no re-entry yet
        brake = _trend.brake_update(brake, V, on, st, cap0)
        mult = _trend.brake_mult(brake["level"], st)
        if v3 is None:
            w, _scale = _trend.target_weights({s: (on[s], rows[s]["sigma"]) for s in syms}, st, mult, brake["reconfirm"])
        else:
            avail = [s for s in syms if valid[s]]
            infos3 = {s: (on[s], float(rows[s]["sigma"])) for s in avail}
            dflt, _det = _alloc.default_d5fi(infos3, st, a3, brake["reconfirm"])
            cur3 = {s: (hold[s] / V if V > 0 else 0.0) for s in syms}
            adv3, conf3 = None, 0.0
            if rng3 is not None and avail:                               # seeded noise "AI": the default plus random tilts, as shares of the book
                raw = np.array([max(0.0, dflt[s] + rng3.normal(0.0, v3.get("noise_sd", 0.15))) for s in syms])
                raw = raw / raw.sum() if raw.sum() > 0 else np.array([1.0, 0.0, 0.0])
                adv3, conf3 = {s: float(x) for s, x in zip(syms, raw)}, float(v3.get("noise_conf", 0.6))
            lo = max(0, pos3[d] - 90)
            ser = {s: {iso[j]: float(c.iat[j, k]) for j in range(lo, pos3[d] + 1)} for k, s in enumerate(syms)
                   if not np.isnan(c.iloc[lo:pos3[d] + 1, k].to_numpy()).any()}
            led_day = d.date()
            dl, wl = _alloc.turnover_left(ledger3, led_day, a3)
            idle3 = max(0.0, 1.0 - sum(cur3.values()) - min(credit3, max(cash, 0.0) / max(V, 1.0)))
            if idle3 > _alloc.RAMP_MIN_IDLE:                              # ramp-in: the ramp limits (default = the normal ones)
                dl, wl = _alloc.turnover_left(ledger3, led_day, dict(a3, max_turnover_day=a3["ramp_turnover_day"],
                                                                      max_turnover_week=a3["ramp_turnover_week"]))
            res3 = _alloc.clamp(default=dflt, advice=adv3, confidence=conf3, current=cur3, on=on, cfg=a3,
                                cov=_alloc.ewma_cov(ser, int(st.get("vol_span", 60))) if ser else None,
                                ineligible=brake["reconfirm"], brake_level=brake["level"], brake_mult=mult,
                                day_left=dl, week_left=wl, underwater=V < cap0, credit=min(credit3, max(cash, 0.0) / max(V, 1.0)),
                                sigmas={s: float(rows[s]["sigma"]) for s in avail},
                                advice_source="noise" if adv3 else "none")
            w = {s: (res3.final[s] if valid[s] else 0.0) for s in syms}
            ledger3[led_day.isoformat()] = ledger3.get(led_day.isoformat(), 0.0) + res3.discretionary_turnover
            for k in sorted(ledger3)[:-10]:
                ledger3.pop(k, None)
        orders = []
        for s in syms:
            if np.isnan(c.at[d, s]):
                continue
            cur = hold[s] / V if V > 0 else 0.0
            dec = _trend.decide(on=on[s], target=w[s], cur=cur, flat=hold[s] < 1e-6,
                                rsi_val=None if np.isnan(rows[s]["rsi"]) else float(rows[s]["rsi"]), bias="neutral", st=dst,
                                brake_level=brake["level"], eligible=s not in brake["reconfirm"], sigma_ok=not np.isnan(rows[s]["sigma"]))
            if dec.action in ("BUY", "SELL"):
                orders.append((s, dec.to_w, dec.kind))
        if init is not None and prev is None:
            orders = []                                   # the opening book already holds whatever was decided that day
        if lag <= 0:
            execute(d, orders, V)
            V = cash + sum(hold.values())
        else:
            pending = orders
        if v3:
            credit3 = min(credit3, max(cash, 0.0) / max(V, 1.0))
        eq.append(V)
        gross.append(sum(hold.values()) / V if V > 0 else 0.0)
        blog.append(brake["level"])
        wlog.append({s: hold[s] / V for s in syms})
        prev = d
    return {"equity": pd.Series(eq, index=idx), "gross": pd.Series(gross, index=idx),
            "brake": pd.Series(blog, index=idx), "weights": pd.DataFrame(wlog, index=idx),
            "trades": pd.DataFrame(tlog, columns=["date", "sym", "dv", "kind"])}


def period_stats(res: dict, start=None, end=None, cash_yield: float = 0.063) -> dict:
    """CAGR, max drawdown, monthly win rate, worst month, trades per month (and Sharpe over cash) for [start, end)."""
    e, g, tl = res["equity"], res["gross"], res["trades"]
    if start is not None:
        e, g = e[e.index >= pd.Timestamp(start)], g[g.index >= pd.Timestamp(start)]
    if end is not None:
        e, g = e[e.index < pd.Timestamp(end)], g[g.index < pd.Timestamp(end)]
    if len(tl):
        tl = tl[(tl["date"] >= e.index[0]) & (tl["date"] <= e.index[-1])]
    r = e.pct_change().dropna()
    yrs = max((e.index[-1] - e.index[0]).days / 365.25, 1e-9)
    mr = e.resample("ME").last().pct_change().dropna()
    cash_d = (1 + cash_yield) ** (1 / 252) - 1
    return {"from": e.index[0].date().isoformat(), "to": e.index[-1].date().isoformat(), "years": round(yrs, 2),
            "final_equity": float(e.iloc[-1]), "cagr": float((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1),
            "max_drawdown": float((e / e.cummax() - 1).min()),
            "month_win": float((mr > 0).mean()) if len(mr) else float("nan"),
            "worst_month": float(mr.min()) if len(mr) else float("nan"),
            "trades_per_month": len(tl) / max(len(mr), 1), "avg_gross": float(g.mean()),
            "sharpe": float((r.mean() - cash_d) / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0}


def v2_report(frames: dict[str, pd.DataFrame], p: dict, start: str, end: str | None = None) -> dict:
    """Run v2 and split it into in-sample and the last `oos_years` years (out-of-sample), plus buy-and-hold GOLDBEES."""
    res = run_v2(frames, p, start, end)
    last = res["equity"].index[-1]
    oos_start = last - pd.DateOffset(years=int(p["strategy"]["backtest"]["oos_years"]))
    y = p["capital"]["liquid_yield_annual"]
    bh = p["capital"]["start"] * frames["close"]["GOLDBEES"].reindex(res["equity"].index).ffill()
    bh = bh / bh.iloc[0]
    bh_res = {"equity": bh * p["capital"]["start"], "gross": pd.Series(1.0, index=bh.index),
              "trades": pd.DataFrame(columns=["date", "sym", "dv", "kind"])}
    short = res["equity"].index[0] >= oos_start          # the run itself is shorter than the OOS window: it is all "OOS"
    return {"res": res, "oos_start": oos_start,
            "in_sample": period_stats(res, None, oos_start, y) if not short else None, "oos": period_stats(res, oos_start, None, y),
            "full": period_stats(res, None, None, y),
            "bh_in_sample": period_stats(bh_res, None, oos_start, y) if not short else None, "bh_oos": period_stats(bh_res, oos_start, None, y)}


def v3_report(frames: dict[str, pd.DataFrame], p: dict, start: str, end: str | None = None, seed: int = 7, noise_sd: float = 0.15,
              noise_conf: float = 0.6, ai_trust: float = 1.0) -> dict:
    """Strategy v3 on daily bars, three variants on the same data: the v2 rules as shipped, v3 D5-FI (fully invested, owner caps,
    0.20/0.40 turnover, no AI) and v3 with a seeded noise "AI" tilted in at `ai_trust` (a stress test of the clamp: the noise has no
    edge, so this shows what a worthless AI costs). Each is split into in-sample and the last oos_years like v2_report."""
    from . import allocation as _alloc
    cfg = _alloc.settings(p)
    out = {"v2": run_v2(frames, p, start, end)}
    out["v3_default"] = run_v2(frames, p, start, end, v3={"cfg": dict(cfg, ai_trust=0.0), "noise_seed": None})
    out["v3_noise_ai"] = run_v2(frames, p, start, end, v3={"cfg": dict(cfg, ai_trust=float(ai_trust)), "noise_seed": seed,
                                                           "noise_sd": noise_sd, "noise_conf": noise_conf})
    y = p["capital"]["liquid_yield_annual"]
    last = out["v2"]["equity"].index[-1]
    oos = last - pd.DateOffset(years=int(p["strategy"]["backtest"]["oos_years"]))
    rep = {"oos_start": oos, "runs": out, "stats": {}}
    for k, r in out.items():
        short = r["equity"].index[0] >= oos
        rep["stats"][k] = {"full": period_stats(r, None, None, y), "oos": period_stats(r, oos, None, y),
                           "in_sample": None if short else period_stats(r, None, oos, y)}
    bh = p["capital"]["start"] * frames["close"]["GOLDBEES"].reindex(out["v2"]["equity"].index).ffill()
    bh = bh / bh.iloc[0]
    bh_res = {"equity": bh * p["capital"]["start"], "gross": pd.Series(1.0, index=bh.index),
              "trades": pd.DataFrame(columns=["date", "sym", "dv", "kind"])}
    rep["stats"]["buy_hold_gold"] = {"full": period_stats(bh_res, None, None, y), "oos": period_stats(bh_res, oos, None, y),
                                     "in_sample": None if out["v2"]["equity"].index[0] >= oos else period_stats(bh_res, None, oos, y)}
    return rep
