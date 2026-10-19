"""Data-growth controls (news cap, seen-headline cap, archive), plus the touch/ALL/send_report interfaces."""
from __future__ import annotations

import csv
import gzip
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import archive as ar          # noqa: E402

from simgold import brief, config, news   # noqa: E402
from simgold.market import IST             # noqa: E402

P = config.load_params()
NOW = datetime(2026, 10, 14, 11, 0, tzinfo=IST)


def tool(name, root, *args, now=NOW):
    return subprocess.run([sys.executable, str(ROOT / "tools" / name), "--root", str(root), *(["--now", now.isoformat()] if name == "guidance.py" else []), *args],
                          capture_output=True, text=True)


# ------------------------------------------------------------------ news.json
def test_news_pruned_to_48h_and_400_items_and_one_line_each(tmp_path):
    now = time.time()
    items = {"id%03d" % i: {"id": "id%03d" % i, "title": "t%d" % i, "ts": now - i * 60} for i in range(600)}
    items["ancient"] = {"id": "ancient", "title": "old", "ts": now - 49 * 3600}
    kept = news.prune_items(items, now)
    assert len(kept) == 400 and "ancient" not in kept and "id000" in kept and "id399" in kept and "id400" not in kept
    book = news.NewsBook(tmp_path / "news.json")
    book.items = kept
    book.write()
    text = (tmp_path / "news.json").read_text(encoding="utf-8")
    assert json.loads(text) == kept and len(text.splitlines()) == 402          # braces + one line per headline
    keys = [json.loads("{" + ln.rstrip(",") + "}") for ln in text.splitlines()[1:-1]]
    assert [list(k)[0] for k in keys] == sorted(kept)                           # sorted keys: stable diffs
    book.write()
    assert (tmp_path / "news.json").read_text(encoding="utf-8") == text         # byte-identical rewrite


def test_empty_news_is_valid_json(tmp_path):
    book = news.NewsBook(tmp_path / "n.json")
    book.write()
    assert json.loads((tmp_path / "n.json").read_text()) == {}


# ------------------------------------------------------------------ seen_headlines.json
def test_seen_headlines_capped_by_count_and_age(tmp_path):
    (tmp_path / "state").mkdir()
    now = NOW.timestamp()
    old = {"old%d" % i: now - 8 * 86400 for i in range(5)}
    recent = {"r%d" % i: now - i for i in range(2100)}
    (tmp_path / "state" / "seen_headlines.json").write_text(json.dumps({"seen": {**old, **recent}}))
    (tmp_path / "state" / "news.json").write_text(json.dumps(
        {"new": {"id": "new", "title": "Gold edges higher", "ts": now - 60}}))
    brief.build_brief(tmp_path, NOW, P)
    seen = json.loads((tmp_path / "state" / "seen_headlines.json").read_text())["seen"]
    assert len(seen) == 2000 and "new" in seen and not any(k.startswith("old") for k in seen)


def test_old_ids_form_is_still_read(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "seen_headlines.json").write_text(json.dumps({"ids": ["a1"]}))
    (tmp_path / "state" / "news.json").write_text(json.dumps(
        {"a1": {"id": "a1", "title": "Gold edges higher", "ts": NOW.timestamp() - 60}}))
    assert "Gold edges higher" not in brief.build_brief(tmp_path, NOW, P)


# ------------------------------------------------------------------ archive
def make_logs(root, day, ticks=True):
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if ticks:
        (logs / f"ticks-{day}.csv").write_text("time,value,floor,cash,liquidbees\n09:15,100000,99500,0,100000\n"
                                               "12:00,100250,99500,0,100250\n15:29,100100,99500,0,100100\n")
    rows = [{"action": "BUY"}, {"action": "SELL"}, {"action": "SKIP"}, {"action": "SKIP"}, {"action": "BLOCK"}]
    (logs / f"decisions-{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (logs / f"incidents-{day}.jsonl").write_text(json.dumps({"x": 1}) + "\n")


def test_archive_summarises_then_compresses_and_is_idempotent(tmp_path):
    today = date(2026, 10, 14)
    make_logs(tmp_path, "2026-08-20")                    # 55 days old: archived
    make_logs(tmp_path, "2026-09-20")                    # 24 days old: stays
    ar.archive_logs(tmp_path, today)
    assert not (tmp_path / "logs" / "ticks-2026-08-20.csv").exists() and (tmp_path / "logs" / "ticks-2026-09-20.csv").exists()
    gz = tmp_path / "archive" / "2026-08" / "ticks-2026-08-20.csv.gz"
    assert gzip.decompress(gz.read_bytes()).decode().startswith("time,value,floor")
    assert (tmp_path / "archive" / "2026-08" / "decisions-2026-08-20.jsonl.gz").exists()
    with open(tmp_path / "data" / "daily_summary.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows == [{"date": "2026-08-20", "open": "100000.0", "close": "100100.0", "day_pnl": "100.0", "floor": "99500.0",
                     "buys": "1", "sells": "1", "skips": "2", "blocks": "1", "incidents": "1",
                     "max_cushion": "750.0", "min_cushion": "500.0"}]
    before = (tmp_path / "data" / "daily_summary.csv").read_text()
    snapshot = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert ar.archive_logs(tmp_path, today) == []                                 # second run: nothing to do
    assert (tmp_path / "data" / "daily_summary.csv").read_text() == before
    assert snapshot == sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())


def test_archive_never_adds_a_summary_row_twice_and_keeps_other_days(tmp_path):
    today = date(2026, 10, 14)
    make_logs(tmp_path, "2026-08-20")
    ar.archive_logs(tmp_path, today)
    make_logs(tmp_path, "2026-08-20", ticks=False)                                # a straggler file for the same day appears
    make_logs(tmp_path, "2026-08-21")
    ar.archive_logs(tmp_path, today)
    with open(tmp_path / "data" / "daily_summary.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["date"] for r in rows] == ["2026-08-20", "2026-08-21"] and rows[0]["buys"] == "1"


def test_archive_dry_run_changes_nothing(tmp_path):
    make_logs(tmp_path, "2026-08-20")
    said = ar.archive_logs(tmp_path, date(2026, 10, 14), dry=True)
    assert said and (tmp_path / "logs" / "ticks-2026-08-20.csv").exists() and not (tmp_path / "archive").exists()
    assert not (tmp_path / "data").exists()


def test_archive_ignores_files_it_does_not_know(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "notes-2020-01-01.txt").write_text("x")
    (tmp_path / "logs" / "decisions-notadate.jsonl").write_text("x")
    assert ar.archive_logs(tmp_path, date(2026, 10, 14)) == []


def test_archive_folds_old_daily_memory_notes(tmp_path):
    d = tmp_path / "memory" / "daily"
    d.mkdir(parents=True)
    (d / "2026-07-01.md").write_text("# 2026-07-01\n- value 100000\n")
    (d / "2026-07-02.md").write_text("# 2026-07-02\n- value 100010\n")
    (d / "2026-10-13.md").write_text("# 2026-10-13\n")
    (d / ".gitkeep").write_text("")
    today = date(2026, 10, 14)
    ar.archive_memory(tmp_path, today)
    out = (tmp_path / "archive" / "memory" / "2026-07.md").read_text()
    assert out.index("2026-07-01") < out.index("2026-07-02") and "value 100010" in out
    assert not (d / "2026-07-01.md").exists() and (d / "2026-10-13.md").exists() and (d / ".gitkeep").exists()
    # a crash after the append but before the delete must not duplicate on the next run
    (d / "2026-07-01.md").write_text("# 2026-07-01\n- value 100000\n")
    ar.archive_memory(tmp_path, today)
    assert (tmp_path / "archive" / "memory" / "2026-07.md").read_text() == out
    assert ar.archive_memory(tmp_path, today) == []


def test_size_warnings(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a").write_bytes(b"x" * 2_000_000)
    assert ar.size_warnings(tmp_path) == []
    monkeypatch.setattr(ar, "LOGS_WARN_MB", 1)
    monkeypatch.setattr(ar, "ARCHIVE_WARN_MB", 0)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "b").write_bytes(b"x" * 10)
    w = ar.size_warnings(tmp_path)
    assert len(w) == 2 and w[0].startswith("logs/") and w[1].startswith("archive/")


# ------------------------------------------------------------------ guidance touch / ALL / help
def test_touch_without_a_file_creates_all_neutral(tmp_path):
    r = tool("guidance.py", tmp_path, "touch", "--note", "checked")
    assert r.returncode == 0, r.stderr
    g = json.loads((tmp_path / "state" / "guidance.json").read_text())
    assert g["bias"] == {"GOLD": "neutral", "SILVER": "neutral", "COPPER": "neutral"}
    assert g["updated"] == NOW.isoformat(timespec="seconds") and g["note"].endswith("checked")


def test_touch_keeps_bias_refreshes_time_and_appends_note(tmp_path):
    from memfix import seed_memory
    seed_memory(tmp_path, asof="2026-10-13")          # trend UP and short term UP: ceiling is favour
    tool("guidance.py", tmp_path, "set", "--bias", "GOLD=favour", "COPPER=block", "--note", "first",
         now=NOW - timedelta(hours=2))
    r = tool("guidance.py", tmp_path, "touch", "--note", "still fine")
    assert r.returncode == 0, r.stderr
    g = json.loads((tmp_path / "state" / "guidance.json").read_text())
    assert g["bias"] == {"GOLD": "favour", "SILVER": "neutral", "COPPER": "block"}
    assert g["updated"] == NOW.isoformat(timespec="seconds") and g["note"].startswith("first | ") and "still fine" in g["note"]
    for i in range(30):                                                           # the note never outgrows its cap
        tool("guidance.py", tmp_path, "touch", "--note", "n" * 100)
    assert len(json.loads((tmp_path / "state" / "guidance.json").read_text())["note"]) <= 300


def test_fastlane_symbol_all(tmp_path):
    r = tool("guidance.py", tmp_path, "fastlane", "--symbol", "ALL", "--halt-buys-today", "--note", "shock")
    assert r.returncode == 0, r.stderr
    raw = json.loads((tmp_path / "state" / "fastlane.json").read_text())
    assert raw["halt_buys_today"] is True and raw["note"] == "shock"
    r = tool("guidance.py", tmp_path, "fastlane", "--symbol", "ALL", "--block-minutes", "30")
    raw = json.loads((tmp_path / "state" / "fastlane.json").read_text())
    assert r.returncode == 0 and set(raw) >= {"GOLDBEES", "SILVERBEES", "HINDCOPPER"}
    assert tool("guidance.py", tmp_path, "fastlane", "--symbol", "ALL", "--sell-fraction", "0.5").returncode == 2
    assert tool("guidance.py", tmp_path, "fastlane", "--symbol", "ALL").returncode == 2


def test_events_add_is_idempotent_and_help_documents_market_today(tmp_path):
    for _ in range(3):
        assert tool("guidance.py", tmp_path, "events", "add", "--date", "2026-10-15", "--type", "CPI", "--time", "18:00").returncode == 0
    assert json.loads((tmp_path / "state" / "events.json").read_text()) == [{"date": "2026-10-15", "type": "CPI", "time": "18:00"}]
    h = subprocess.run([sys.executable, str(ROOT / "tools" / "guidance.py"), "market-today", "--help"], capture_output=True, text=True)
    assert h.returncode == 0 and "NAME:MOVE_PCT" in h.stdout and "percent" in h.stdout


# ------------------------------------------------------------------ send_report and daily_memory
def test_send_report_subject_and_paths_on_a_holiday(tmp_path):
    (tmp_path / "state").mkdir()
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "send_report.py"), "--root", str(tmp_path), "--date", "2026-10-20"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["subject"].startswith("Metals Daybook · ") and "Market closed" in out["subject"]
    for k in ("html_path", "text_path", "page_path"):
        assert Path(out[k]).is_file() and str(tmp_path) in out[k]


def test_send_report_subject_on_a_trading_day(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "ticks-2026-10-14.csv").write_text("time,value,floor,cash,liquidbees\n09:15,100000,99500,0,100000\n"
                                                           "15:29,100147,99500,0,100147\n")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "send_report.py"), "--root", str(tmp_path), "--date", "2026-10-14"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["subject"] == "Metals Daybook · 📈 Wed 14 Oct · +₹147 (green)"


def test_daily_memory_prints_only(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "logs" / "ticks-2026-10-14.csv").write_text("time,value,floor,cash,liquidbees\n09:15,100000,99500,0,100000\n"
                                                           "15:29,100147,99500,0,100147\n")
    before = sorted(p for p in tmp_path.rglob("*"))
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "daily_memory.py"), "--root", str(tmp_path), "--date", "2026-10-14"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "# 2026-10-14 (Wed)" and len(lines) <= 15 and any(x.startswith("- WHY:") for x in lines)
    assert any("day P&L +147" in x for x in lines)
    assert before == sorted(p for p in tmp_path.rglob("*")) or all("reports" in str(p) for p in set(tmp_path.rglob("*")) - set(before))
