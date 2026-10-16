"""Monthly performance evaluation (simgold/evaluate.py, tools/evaluate.py) on synthetic fixtures."""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from simgold import evaluate as ev

ROOT = Path(__file__).resolve().parent.parent


def make_root(tmp_path, equity, closes=None, decisions=(), trades=(), ticks_extra=None, total_costs=0.0):
    """equity: [(date, value)]; closes: {date: (gold, silver)}; decisions: [(date, action, sym, pnl)]."""
    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "state" / "equity.csv").write_text("\n".join(f"{d},{v}" for d, v in equity) + "\n")
    for d, (g, s) in (closes or {}).items():
        extra = (ticks_extra or {}).get(d, (0.0, 0.0))        # (cash, liquidbees) fractions of 100000
        cash, liq = extra
        (tmp_path / "logs" / f"ticks-{d}.csv").write_text(
            "time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER\n"
            f"10:00,100000,99500,{cash},{liq},{g},{s},450\n15:29,100000,99500,{cash},{liq},{g},{s},450\n")
    byday = {}
    for i, (d, a, sym, pnl) in enumerate(decisions):
        byday.setdefault(d, []).append(json.dumps(
            {"ts": f"{d}T10:{i:02d}:00+05:30", "action": a, "symbol": sym, "qty": 10, "price": 100.0,
             "amount": 1000.0, "pnl": pnl}))
    for d, lines in byday.items():
        (tmp_path / "logs" / f"decisions-{d}.jsonl").write_text("\n".join(lines) + "\n")
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps({"trades": list(trades), "total_costs": total_costs}))
    return tmp_path


def test_drawdown_math():
    pts = [(date(2026, 10, i), v) for i, v in zip(range(1, 7), [100, 110, 99, 105, 120, 108])]
    dd = ev.drawdown(pts)
    assert dd["rupees"] == 12 and dd["peak_date"] == "2026-10-05" and dd["trough_date"] == "2026-10-06"
    first = ev.drawdown(pts[:3])
    assert first["rupees"] == 11 and first["pct"] == 10.0
    assert ev.drawdown([(date(2026, 10, 1), 5), (date(2026, 10, 2), 6)])["rupees"] == 0


def test_cagr_and_flag(tmp_path):
    assert ev.cagr(100, 110, 365) == pytest.approx(10.0)
    assert ev.cagr(100, 110, 0) is None
    root = make_root(tmp_path, [("2026-10-01", 100000), ("2026-10-02", 100100)],
                     {"2026-10-01": (100, 200), "2026-10-02": (101, 200)})
    r = ev.evaluate(root, "2026-10")
    assert r["cagr_reliable"] is False
    assert any("under 3 months" in f for f in r["flags"])


def test_cagr_reliable_after_three_months(tmp_path):
    eq = [("2026-10-01", 100000), ("2027-01-15", 101000)]
    r = ev.evaluate(make_root(tmp_path, eq), None)
    assert r["cagr_reliable"] is True and not any("under 3 months" in f for f in r["flags"])
    assert r["cagr_pct"] > 3


def test_benchmarks_and_excess(tmp_path):
    eq = [("2026-10-01", 100000), ("2026-10-02", 100500), ("2026-10-03", 101000)]
    closes = {"2026-10-01": (100, 200), "2026-10-02": (90, 220), "2026-10-03": (110, 200)}
    r = ev.evaluate(make_root(tmp_path, eq, closes), "2026-10")
    b = {x["name"]: x for x in r["benchmarks"]}
    gold = b["Buy-and-hold GOLDBEES"]
    assert gold["return_pct"] == 10.0 and gold["max_drawdown_pct"] == 10.0
    assert b["Buy-and-hold SILVERBEES"]["return_pct"] == 0.0
    assert b["50/50 gold-silver"]["return_pct"] == pytest.approx(5.0)
    assert gold["excess_pct"] == pytest.approx(1.0 - 10.0)
    assert b["All-cash LIQUIDBEES (6.5%/yr)"]["return_pct"] == pytest.approx(6.5 * 2 / 365, abs=1e-3)
    assert r["verdict"].startswith("Lagged gold buy-and-hold by 9.00%")
    assert "fraction of its drawdown" in r["verdict"] or "half its drawdown" in r["verdict"]


def test_verdict_beat_with_half_drawdown():
    gold = {"available": True, "excess_pct": 1.2, "max_drawdown_pct": 4.0, "return_pct": 1.0}
    v = ev._verdict(2.2, 2.0, gold, {"available": True, "return_pct": 0.5}, True)
    assert v == "Beat gold buy-and-hold by +1.20% with half its drawdown."


def test_no_trade_month(tmp_path):
    eq = [("2026-10-01", 100000), ("2026-10-02", 100017.26)]
    r = ev.evaluate(make_root(tmp_path, eq, {"2026-10-01": (100, 200), "2026-10-02": (100, 200)}), "2026-10")
    t = r["trades"]
    assert t["trades"] == 0 and t["win_rate_pct"] is None and t["profit_factor"] is None
    assert t["profit_factor_note"] == "no closed trades"
    assert r["costs_paid"] == 0
    assert ev.to_md(r) and ev.to_html(r)


def test_trade_stats_and_costs(tmp_path):
    eq = [("2026-09-30", 100000), ("2026-10-01", 100000), ("2026-10-02", 100090)]
    decs = [("2026-10-01", "BUY", "GOLDBEES", None), ("2026-10-01", "SELL", "GOLDBEES", 100.0),
            ("2026-10-02", "SELL", "SILVERBEES", -40.0), ("2026-10-02", "SELL", "GOLDBEES", 30.0),
            ("2026-09-30", "SELL", "GOLDBEES", 999.0)]
    trades = [{"ts": "2026-10-01T10:00:00+05:30", "costs": 5.5}, {"ts": "2026-10-02T10:00:00+05:30", "costs": 4.5},
              {"ts": "2026-09-30T10:00:00+05:30", "costs": 100.0}]
    r = ev.evaluate(make_root(tmp_path, eq, decisions=decs, trades=trades), "2026-10")
    t = r["trades"]
    assert (t["buys"], t["sells"], t["wins"], t["losses"]) == (1, 3, 2, 1)
    assert t["win_rate_pct"] == pytest.approx(66.7, abs=0.05)
    assert t["avg_win"] == 65.0 and t["avg_loss"] == -40.0
    assert t["profit_factor"] == pytest.approx(130 / 40)
    assert r["costs_paid"] == 10.0
    assert r["return_rupees"] == 90.0     # baseline is the last row before the month


def test_missing_price_days_flagged(tmp_path):
    eq = [("2026-10-01", 100000), ("2026-10-02", 100000), ("2026-10-03", 100000), ("2026-10-04", 100000)]
    closes = {"2026-10-01": (100, 200), "2026-10-03": (110, 200)}     # 10-02 and 10-04 missing
    r = ev.evaluate(make_root(tmp_path, eq, closes), "2026-10")
    assert any("without recorded closes" in f for f in r["flags"])
    gold = r["benchmarks"][0]
    assert gold["return_pct"] == 10.0 and gold["stale_days"] == 1


def test_no_prices_at_all(tmp_path):
    r = ev.evaluate(make_root(tmp_path, [("2026-10-01", 100000), ("2026-10-02", 100010)]), "2026-10")
    assert r["available"] and not r["benchmarks"][0]["available"]
    assert "no gold benchmark" in r["verdict"]
    assert ev.to_md(r)


def test_benchmark_csv_overrides(tmp_path):
    eq = [("2026-10-01", 100000), ("2026-10-02", 100000)]
    root = make_root(tmp_path, eq)
    csvp = tmp_path / "b.csv"
    csvp.write_text("date,GOLDBEES,SILVERBEES\n2026-10-01,100,200\n2026-10-02,105,190\n")
    r = ev.evaluate(root, "2026-10", csvp)
    b = {x["name"]: x for x in r["benchmarks"]}
    assert b["Buy-and-hold GOLDBEES"]["return_pct"] == 5.0
    assert b["Buy-and-hold SILVERBEES"]["return_pct"] == -5.0


def test_time_in_market_and_liquid_share(tmp_path):
    eq = [("2026-10-01", 100000), ("2026-10-02", 100100)]
    closes = {"2026-10-01": (100, 200), "2026-10-02": (100, 200)}
    r = ev.evaluate(make_root(tmp_path, eq, closes, ticks_extra={
        "2026-10-01": (0, 50000), "2026-10-02": (0, 50000)}), "2026-10")
    assert r["time_in_market"]["avg_invested_pct"] == 50.0
    assert r["liquidbees_income_est"] == pytest.approx(50000 * 0.063 / 365, abs=0.01)
    assert r["liquidbees_income_share_pct"] == pytest.approx(r["liquidbees_income_est"] / 100 * 100, abs=0.2)


def test_empty_and_missing_month(tmp_path):
    assert ev.evaluate(make_root(tmp_path, []), "2026-10")["available"] is False
    p2 = tmp_path / "x"
    p2.mkdir()
    r = ev.evaluate(make_root(p2, [("2026-10-01", 100000)]), "2026-11")
    assert r["available"] is False


def test_cli_write_and_formats(tmp_path):
    eq = [("2026-10-01", 100000), ("2026-10-02", 99900)]
    root = make_root(tmp_path, eq, {"2026-10-01": (100, 200), "2026-10-02": (99, 200)})
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "tools" / "evaluate.py"), "--root", str(root), *a],
                                    capture_output=True, text=True)
    p = run("--month", "2026-10", "--format", "json", "--write")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["green"] is False
    assert (root / "reports" / "monthly" / "2026-10.json").exists()
    assert "| Verdict |" in (root / "reports" / "monthly" / "2026-10.md").read_text()
    assert "<table>" in run("--all", "--format", "html").stdout
    assert run("--all", "--write").returncode == 2
    assert run("--month", "2026-13").returncode == 2
