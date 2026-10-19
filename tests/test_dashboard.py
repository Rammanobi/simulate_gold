"""The Daybook Dashboard day document (simgold/dashboard.py, tools/dashboard.py)."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from simgold import dashboard
from simgold.market import IST

ROOT = Path(__file__).resolve().parent.parent
D = "2026-10-14"
NOW = datetime(2026, 10, 14, 12, 40, tzinfo=IST)


def dec(time, action, symbol, reason="r", **kw):
    r = {"ts": f"{D}T{time}:00+05:30", "time": time, "action": action, "symbol": symbol, "qty": kw.get("qty", 0),
         "price": kw.get("price", 100.0), "amount": kw.get("amount", 0.0), "pnl": kw.get("pnl"), "reason": reason,
         "trigger": kw.get("trigger", "Minute check"), "stop": kw.get("stop"), "risk": kw.get("risk", 0.0),
         "value_after": kw.get("value_after", 100000.0), "floor": 99500.0}
    return json.dumps(r)


def make_root(tmp_path: Path, n_ticks=5) -> Path:
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "logs" / f"decisions-{D}.jsonl").write_text("\n".join([
        dec("09:48", "BUY", "GOLDBEES", "score held", qty=40, price=119.7, amount=4788.0, stop=118.26, risk=148.0),
        dec("09:20", "BLOCK", "SILVERBEES", "news", trigger="News trigger"),
        "not json",
    ]) + "\n")
    rows = ["time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER"]
    rows += [f"09:{15 + i:02d}:00,{100000 + i},99500,0,{95212 + i},119.7,202.8,468.7" for i in range(n_ticks)]
    (tmp_path / "logs" / f"ticks-{D}.csv").write_text("\n".join(rows) + "\n")
    (tmp_path / "state" / "portfolio.json").write_text(json.dumps(
        {"positions": {"GOLDBEES": {"qty": 40, "avg": 119.7, "stop": 118.26}}, "floor": 99500.0,
         "liquidbees": 95212.0, "cash": 0.0, "day_open_value": 100000.0, "day_costs_date": D}))
    (tmp_path / "state" / "guidance.json").write_text(json.dumps(
        {"updated": f"{D}T10:16:00+05:30", "note": "GOLD: no change"}))
    return tmp_path


def test_document_has_every_key_and_sorted_decisions(tmp_path):
    doc = dashboard.build(D, make_root(tmp_path), NOW)
    for k in ("date", "weekday", "updated", "open", "close", "pnl", "floor", "cushion", "counts", "alloc",
              "holdings", "ticks", "decisions", "notes", "summary", "tomorrow"):
        assert k in doc
    assert [d["time"] for d in doc["decisions"]] == ["09:20", "09:48"]
    assert doc["counts"] == {"BUY": 1, "SELL": 0, "SKIP": 0, "BLOCK": 1, "HOLD": 0}
    assert doc["decisions"][1]["reason"] == "score held" and doc["decisions"][1]["stop"] == 118.26
    assert doc["alloc"]["GOLDBEES"] > 0 and set(doc["alloc"]) == {"GOLDBEES", "SILVERBEES", "HINDCOPPER", "LIQUIDBEES"}
    assert doc["ticks"][0] == ["09:15", 100000.0, 119.7, 202.8, 468.7]
    assert doc["updated"] == "2026-10-14T12:40:00+05:30"


def test_ticks_are_capped_and_keep_the_last_row(tmp_path):
    doc = dashboard.build(D, make_root(tmp_path, n_ticks=400), NOW)
    assert len(doc["ticks"]) <= dashboard.MAX_TICKS
    assert doc["ticks"][-1][1] == 100399.0


def test_notes_accumulate_across_writes(tmp_path):
    root = make_root(tmp_path)
    dashboard.write(D, root, NOW)
    (root / "state" / "guidance.json").write_text(json.dumps({"updated": f"{D}T11:16:00+05:30", "note": "second"}))
    path = dashboard.write(D, root, NOW)
    notes = json.loads(path.read_text())["notes"]
    assert [(n["time"], n["who"], n["text"]) for n in notes] == [("10:16", "hourly", "GOLD: no change"),
                                                                 ("11:16", "hourly", "second")]


def test_old_guidance_is_not_a_note_for_today(tmp_path):
    root = make_root(tmp_path)
    (root / "state" / "guidance.json").write_text(json.dumps({"updated": "2026-10-13T15:16:00+05:30", "note": "old"}))
    assert dashboard.build(D, root, NOW)["notes"] == []


def test_safe_write_never_raises(tmp_path):
    assert dashboard.safe_write("not-a-date", tmp_path) is False


def test_document_stays_small_on_a_busy_day(tmp_path):
    root = make_root(tmp_path, n_ticks=375)
    lines = [dec(f"{9 + i // 60:02d}:{i % 60:02d}", "BLOCK", "GOLDBEES", "x" * 200) for i in range(200)]
    (root / "logs" / f"decisions-{D}.jsonl").write_text("\n".join(lines) + "\n")
    path = dashboard.write(D, root, NOW)
    assert path.stat().st_size < 200 * 1024          # database documents are capped at 256 KiB


def test_tool_refuses_output_inside_the_repo(tmp_path):
    root = make_root(tmp_path)
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "dashboard.py"), "--root", str(root), "--date", D,
                        "--out", str(root / "logs" / "x.json")], capture_output=True, text=True)
    assert r.returncode == 2 and not (root / "logs" / "x.json").exists()
    out = tmp_path.parent / f"dash-{tmp_path.name}.json"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "dashboard.py"), "--root", str(root), "--date", D,
                        "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text())["date"] == D and not (root / "logs" / f"dashboard-{D}.json").exists()


def test_email_button_opens_the_day_on_the_dashboard(tmp_path):
    sys.path.insert(0, str(ROOT / "tools"))
    import send_report
    (tmp_path / "config").mkdir()
    assert send_report.dashboard_link(tmp_path, D) is None
    (tmp_path / "config" / "dashboard_url.txt").write_text("https://claude.ai/artifact/abc\n")
    assert send_report.dashboard_link(tmp_path, D) == f"https://claude.ai/artifact/abc#{D}"
    (tmp_path / "config" / "dashboard_url.txt").write_text("https://evil.example/x\n")
    assert send_report.dashboard_link(tmp_path, D) is None


def test_minute_checks_are_compact_and_complete(tmp_path):
    root = make_root(tmp_path)
    lines = []
    for i in range(375):
        hh, mm = divmod(9 * 60 + 15 + i, 60)
        lines.append(json.dumps({"ts": f"{D}T{hh:02d}:{mm:02d}:00+05:30", "time": f"{hh:02d}:{mm:02d}", "value": 100000 + i,
                                 "checks": {"GOLDBEES": f"Score 0.{i % 100:02d} is below the 0.70 needed to buy. " + "x" * 150,
                                            "SILVERBEES": "Buys paused until 13:21 (News trigger). Checked; nothing to do.",
                                            "HINDCOPPER": "Checked; nothing to do."}}))
    lines.append(lines[0])                                           # a duplicate from a merge is shown once
    (root / "logs" / f"checks-{D}.jsonl").write_text("\n".join(lines) + "\n")
    path = dashboard.write(D, root, NOW)
    doc = json.loads(path.read_text())
    ch = doc["checks"]
    assert ch["syms"] == ["GOLDBEES", "SILVERBEES", "HINDCOPPER"] and len(ch["rows"]) == 375
    assert ch["rows"][0][0] == "09:15" and ch["rows"][-1][0] == "15:29"
    assert ch["texts"][ch["rows"][5][2][1]].startswith("Buys paused until 13:21")
    assert len(ch["texts"]) <= 102 and path.stat().st_size < 200 * 1024


def v2_root(tmp_path: Path, brake=None) -> Path:
    root = make_root(tmp_path)
    recs = [
        json.loads(dec("09:48", "BUY", "GOLDBEES", "target 85% = min(1, 0.14/0.165)", qty=40, price=119.7,
                       amount=4788.0, stop=118.26, trigger="Trend check")),
        {"time": "15:10", "action": "TARGET", "symbol": "GOLDBEES", "trigger": "Trend check", "stop": 118.26,
         "exit_level": 117.9, "state": "ON", "sigma": 0.1647, "target_pct": 85.0, "actual_pct": 4.8,
         "reason": "GOLDBEES trend ON (close 121.40 > SMA200 118.90); sigma60 0.165; target 85%, actual 5%"},
        {"time": "15:10", "action": "TARGET", "symbol": "SILVERBEES", "trigger": "Trend check", "state": "OFF",
         "sigma": 0.31, "target_pct": 0.0, "actual_pct": 0.0,
         "reason": "SILVERBEES trend OFF (close 120.09 < SMA200 121.40); sigma60 0.310; target 0%, actual 0%"},
    ]
    if brake:
        recs.append({"time": "15:11", "action": "BLOCK", "symbol": "ALL", "trigger": "Drawdown brake", "reason": brake})
    (root / "logs" / f"decisions-{D}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return root


def test_v1_day_has_no_strategy_block(tmp_path):
    doc = dashboard.build(D, make_root(tmp_path), NOW)
    assert "strategy" not in doc and "targets" not in doc


def test_v2_day_has_strategy_and_targets(tmp_path):
    doc = dashboard.build(D, v2_root(tmp_path), NOW)
    s = doc["strategy"]
    assert s["brake_level"] == 0 and s["account_stop"] is False and s["ai_stop"] is False
    assert 0 < s["invested_pct"] < 10 and "min_invested" not in s
    g, sv = doc["targets"]
    assert (g["symbol"], g["trend"], g["sigma60"], g["target_pct"], g["actual_pct"]) == ("GOLDBEES", "ON", 0.1647, 85.0, 4.8)
    assert g["reason"] == "close 121.40 > SMA200 118.90" and g["stop"] == 118.26 and g["exit_level"] == 117.9
    assert sv["trend"] == "OFF" and sv["reason"] == "close 120.09 < SMA200 121.40" and "stop" not in sv
    assert doc["counts"]["BUY"] == 1 and "TARGET" not in doc["counts"]


def test_v2_brake_and_account_stop_levels(tmp_path):
    doc = dashboard.build(D, v2_root(tmp_path, "Drawdown brake level 2: value 70,000 is 26% from its peak"), NOW)
    assert doc["strategy"]["brake_level"] == 2
    (tmp_path / "b").mkdir()
    doc = dashboard.build(D, v2_root(tmp_path / "b", "Drawdown brake level 1: x"), NOW)
    assert doc["strategy"]["brake_level"] == 1


def test_v2_min_invested_is_shown_when_configured(tmp_path):
    root = v2_root(tmp_path)
    (root / "config").mkdir()
    (root / "config" / "params.yaml").write_text("strategy:\n  mode: v2_trend\n  min_invested: 0.3\n  account_stop: 75000\n")
    s = dashboard.build(D, root, NOW)["strategy"]
    assert s["min_invested"] == 30.0 and s["mode"] == "v2_trend"
