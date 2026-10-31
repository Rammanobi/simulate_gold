"""logs/alldays.json (simgold/alldays.py), the engine hook, the page template and the page builder."""
import json
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from simgold import alldays
from simgold.market import NSE_HOLIDAYS, is_trading_day

ROOT = Path(__file__).resolve().parent.parent
D1, D2, D3 = "2026-10-21", "2026-10-22", "2026-10-23"       # Wed Thu Fri, all trading days


def _doc(day, close, holdings=(), liquid=0.0, idle=None, decisions=()):
    alloc = {"GOLDBEES": 0.0, "SILVERBEES": 0.0, "HINDCOPPER": 0.0, "LIQUIDBEES": liquid}
    rows = []
    for sym, units, price in holdings:
        alloc[sym] = round(units * price, 2)
        rows.append({"symbol": sym, "units": units, "price": price})
    d = {"date": day, "close": close, "updated": f"{day}T15:30:00+05:30", "alloc": alloc, "holdings": rows,
         "decisions": list(decisions), "ticks": []}
    if idle:
        d["idle"] = idle
    return d


def _ticks(root, day, rows):
    lines = ["time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER"]
    lines += [f"{t},{v},75000,0,0,{g},183,400" for t, v, g in rows]
    (root / "logs" / f"ticks-{day}.csv").write_text("\n".join(lines) + "\n")


def _trade(ts, side, qty, price, costs, pnl=0.0, asset="GOLDBEES"):
    return {"ts": ts, "asset": asset, "side": side, "qty": qty, "price": price, "value": round(qty * price, 2),
            "costs": costs, "pnl": pnl, "reason": "because"}


@pytest.fixture
def root(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()
    trades = [_trade(f"{D1}T15:10:20+05:30", "BUY", 400, 118.0, 23.6),
              _trade(f"{D3}T09:16:00+05:30", "SELL", 300, 115.0, 17.0, pnl=-950.5),
              _trade(f"{D3}T10:30:00+05:30", "SELL", 100, 115.5, 5.0, pnl=-300.0)]
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps({"cash": 1, "positions": {}, "trades": trades}))
    _ticks(tmp_path, D1, [("09:15:00", 100000, 119), ("09:16", 100001, 119.5), ("15:29", 100048.28, 118.13)])
    docs = {D1: _doc(D1, 100048.28, [("GOLDBEES", 400, 118.13)], liquid=52800.0),
            D2: _doc(D2, 99739.84, [("GOLDBEES", 400, 117.35)], liquid=52700.0),
            D3: _doc(D3, 98980.67, idle="cash", liquid=98980.67)}
    for d, doc in docs.items():
        (tmp_path / "logs" / f"dashboard-{d}.json").write_text(json.dumps(doc))
    return tmp_path


def test_every_day_reconciles_to_the_close_exactly(root):
    out = alldays.build(root)
    assert [d["date"] for d in out["days"]] == [D1, D2, D3]
    for d in out["days"]:
        assert round(sum(d["alloc"].values()), 2) == d["close_value"]
        assert d["invested_value"] + d["cash_value"] == pytest.approx(d["close_value"], abs=0.005)
        for h in d["holdings"]:                              # holdings are valued at the close price
            assert h["value"] == round(h["qty"] * h["price"], 2)
            assert d["alloc"][h["symbol"]] == h["value"]
    d1 = out["days"][0]
    assert d1["alloc"]["GOLDBEES"] == round(400 * 118.13, 2) and d1["alloc"]["Cash"] == round(100048.28 - 400 * 118.13, 2)


def test_day_profit_is_close_minus_previous_close(root):
    days = alldays.build(root)["days"]
    assert days[0]["day_profit"] == pytest.approx(48.28) and days[0]["open_value"] == 100000.0   # first day: starting capital
    assert days[1]["day_profit"] == pytest.approx(99739.84 - 100048.28)
    assert days[2]["day_profit"] == pytest.approx(98980.67 - 99739.84)
    assert days[2]["overall_profit"] == pytest.approx(-1019.33)
    assert days[2]["day_profit_pct"] == pytest.approx(-759.17 / 99739.84 * 100, abs=0.001)


def test_liquidbees_era_counts_as_cash_and_is_noted(root):
    days = alldays.build(root)["days"]
    assert days[0]["idle_instrument"] == "liquidbees" and days[2]["idle_instrument"] == "cash"
    assert "LIQUIDBEES" not in days[0]["alloc"] and days[0]["cash_value"] > 50000


def test_trades_are_exact_and_pairs_link_buy_to_sells(root):
    out = alldays.build(root)
    buy = out["trades_all"][0]
    assert (buy["date"], buy["time"], buy["qty"], buy["price"], buy["costs"], buy["profit"]) == (D1, "15:10", 400, 118.0, 23.6, None)
    s1, s2 = out["trades_all"][1:]
    assert s1["profit"] == -950.5 and s2["profit"] == -300.0 and s1["costs"] == 17.0
    assert buy["pair"] == s1["pair"] == s2["pair"] == "P1" and buy["status"] == "closed"
    assert [t["side"] for t in out["days"][2]["trades"]] == ["SELL", "SELL"] and out["days"][1]["trades"] == []
    assert "Sold 300 GOLDBEES at" in out["days"][2]["headline"]


def test_pairing_fifo_partial_and_open():
    L = [{"date": "2026-10-01", "time": "10:00", "symbol": "GOLDBEES", "side": "BUY", "units": 10, "price": 1, "amount": 10},
         {"date": "2026-10-02", "time": "10:00", "symbol": "GOLDBEES", "side": "BUY", "units": 5, "price": 1, "amount": 5},
         {"date": "2026-10-03", "time": "10:00", "symbol": "SILVERBEES", "side": "BUY", "units": 5, "price": 1, "amount": 5},
         {"date": "2026-10-05", "time": "10:00", "symbol": "GOLDBEES", "side": "SELL", "units": 12, "price": 1, "amount": 12}]
    p = alldays.pair_trades(L)
    assert [t["pair"] for t in p] == ["P1", "P2", "P3", "P1"]          # the sell is tagged with the first lot it takes from
    assert [t.get("status") for t in p[:3]] == ["closed", "part", "open"]
    assert len({t["id"] for t in p}) == 4


def test_chg_pct_is_close_vs_previous_close(root):
    days = alldays.build(root)["days"]
    assert days[0]["prices"]["GOLDBEES"]["chg_pct"] is None            # no previous close
    assert days[1]["prices"]["GOLDBEES"]["chg_pct"] == round((117.35 / 118.13 - 1) * 100, 2)


def test_intraday_is_five_minute_downsample_with_last_tick(root):
    iv = alldays.build(root)["days"][0]["intraday"]
    assert [x[0] for x in iv] == ["09:15", "15:29"] or iv[0] == ["09:15", 100000.0]
    assert iv[-1] == ["15:29", 100048.28]
    rows = [(f"09:{m:02d}", 1.0 + m, {}) for m in range(15, 40)]
    ds = alldays.downsample(rows)
    assert [x[0] for x in ds] == ["09:15", "09:20", "09:25", "09:30", "09:35", "09:39"]


def test_weekends_and_holidays_are_skipped_and_empty_day_is_valid(root):
    hol = next(h for h in sorted(NSE_HOLIDAYS) if h.year == 2026 and h.weekday() < 5)
    assert not is_trading_day(hol)
    for day in (hol.isoformat(), "2026-10-24"):                       # a holiday and a Saturday
        (root / "logs" / f"dashboard-{day}.json").write_text(json.dumps(_doc(day, 98980.67, idle="cash", liquid=98980.67)))
    (root / "logs" / "dashboard-bad.json").write_text("{not json")
    out = alldays.build(root)
    assert hol.isoformat() not in [d["date"] for d in out["days"]] and "2026-10-24" not in [d["date"] for d in out["days"]]
    nxt = date(2026, 10, 26)                                          # Monday: no ticks, no trades, no decisions
    (root / "logs" / f"dashboard-{nxt}.json").write_text(json.dumps(_doc(nxt.isoformat(), 98980.67, idle="cash", liquid=98980.67)))
    d = alldays.build(root)["days"][-1]
    assert d["date"] == "2026-10-26" and d["intraday"] == [] and d["trades"] == [] and d["day_profit"] == 0.0
    assert d["prices"]["GOLDBEES"]["close"] is None and d["alloc"]["Cash"] == 98980.67
    assert "No trades" in d["headline"]


def test_no_logs_at_all_gives_an_empty_but_valid_document(tmp_path):
    out = alldays.build(tmp_path)
    assert out["days"] == [] and out["trades_all"] == [] and out["now"] is None and out["v"] == 1


def test_now_block_and_size(root):
    out = alldays.build(root)
    assert out["now"]["date"] == D3 and out["now"]["close_value"] == 98980.67 and out["start_capital"] == 100000
    assert len(alldays.dumps(out)) < 20000


def test_write_is_atomic_and_safe_write_never_raises(root, monkeypatch):
    p = alldays.write(root)
    assert p == root / "logs" / "alldays.json" and json.loads(p.read_text())["days"][0]["date"] == D1
    assert not list((root / "logs").glob("*.tmp"))
    before = p.read_text()

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(alldays, "build", boom)
    assert alldays.safe_write(root) is False and p.read_text() == before    # old file kept


def test_tool_writes_and_refuses_nothing(root):
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "alldays.py"), "--root", str(root), "--write"], capture_output=True, text=True)
    assert r.returncode == 0 and (root / "logs" / "alldays.json").exists() and "3 days" in r.stdout


# ------------------------------------------------------------------ engine hook
def test_engine_sync_writes_the_file_and_survives_errors(root, monkeypatch):
    import run_live
    live = run_live.Live.__new__(run_live.Live)
    live.mon = None
    live.last_tick_day = date.fromisoformat(D3)
    live.write_brief = lambda: None
    monkeypatch.setattr(run_live, "ROOT", root)
    monkeypatch.setattr(run_live.gitsync, "enabled", lambda env: True)
    monkeypatch.setattr(run_live.gitsync, "sync", lambda *a, **k: True)
    monkeypatch.setattr(run_live.dashboard, "safe_write", lambda *a, **k: True)
    live.sync()
    assert (root / "logs" / "alldays.json").exists()
    (root / "logs" / "alldays.json").unlink()
    monkeypatch.setattr(run_live.alldays, "build", lambda r: (_ for _ in ()).throw(ValueError("boom")))
    synced = []
    monkeypatch.setattr(run_live.gitsync, "sync", lambda *a, **k: synced.append(1) or True)
    live.sync()                                                       # must not raise, and the sync still happens
    assert synced == [1] and not (root / "logs" / "alldays.json").exists()


# ------------------------------------------------------------------ page
TEMPLATE = (ROOT / "docs" / "dashboard" / "template.html").read_text(encoding="utf-8")


def test_template_has_the_required_hooks_and_no_external_scripts():
    assert TEMPLATE.count("__DATA__") == 1
    for hook in ('id="top"', 'id="map-card"', 'id="hold"', 'id="cal"', 'id="trades"', 'id="drawer"', 'id="scrim"', 'id="status"',
                 "pollLive", "pollHistory", "live/now.json", "main/logs/alldays.json", "validLive", "d.v === 1", "Market closed",
                 "Delayed", "Live \\u00B7 updated", "Every trading day", "Every trade", "Details: every decision", "decisions",
                 "max-width:700px", "prefers-color-scheme:dark", "dashboard-"):
        assert hook in TEMPLATE, hook
    assert not re.findall(r"<script[^>]+src=", TEMPLATE) and "http://" not in TEMPLATE
    assert re.findall(r"https://[^\s\"')]+", TEMPLATE) and all("githubusercontent" in u or "RAW" in u or u.startswith("https://raw.") for u in re.findall(r"https://[^\s\"')]+", TEMPLATE))


def test_template_javascript_is_valid():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = re.search(r"<script>(.*)</script>", TEMPLATE, re.S).group(1).replace("__DATA__", "null")
    p = Path(__import__("tempfile").mkdtemp()) / "page.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_render_page_embeds_the_data_and_escapes_script_ends(root):
    data = alldays.build(root)
    data["days"][0]["headline"] = "</script><!-- x"
    page = alldays.render_page(data)
    assert "__DATA__" not in page and '"close_value":100048.28' in page
    assert "</script><!--" not in page.split("const EMBEDDED")[1].split("const REPO")[0]
    with pytest.raises(ValueError):
        alldays.render_page(data, "no marker")


def test_builder_tool_refuses_a_path_inside_the_repo_and_writes_outside(root, tmp_path_factory):
    out = tmp_path_factory.mktemp("out") / "dashboard.html"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "build_dashboard_page.py"), "--root", str(root), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0 and out.exists() and "const EMBEDDED = {" in out.read_text(encoding="utf-8")
    bad = subprocess.run([sys.executable, str(ROOT / "tools" / "build_dashboard_page.py"), "--root", str(root), "--out", str(root / "x.html")], capture_output=True, text=True)
    assert bad.returncode == 2 and not (root / "x.html").exists()
