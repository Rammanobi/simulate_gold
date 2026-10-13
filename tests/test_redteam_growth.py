"""Growth over many trading days (tests/redteam_growth.py): context <= 25 KB, brief and news inside their caps,
archive idempotent and never losing a summary row. SIMGOLD_GROWTH_DAYS=400 reproduces docs/REDTEAM.md."""
from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import date
from pathlib import Path

import redteam_growth as G

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import archive as ar  # noqa: E402

DAYS = int(os.environ.get("SIMGOLD_GROWTH_DAYS", "130"))


def test_growth_stays_inside_every_cap(tmp_path):
    out = G.run(tmp_path / "repo", DAYS, check_every=3)
    w = out["worst"]
    assert w["context"] <= G.R["context_max_bytes"]
    assert w["brief_bytes"] <= G.R["brief_max_bytes"] and w["brief_lines"] <= G.R["brief_max_lines"]
    assert w["news_items"] <= 400
    assert out["summary_dates_unique"] and out["summary_covers_archived_days"] and out["summary_rows"] == out["archived_days"]
    assert out["gz_readable"] and out["archive_runs"] >= DAYS // 25
    assert out["fastlane_requests_lines"] < 200                  # trimmed by the monthly archive
    assert not list((tmp_path / "repo" / "memory" / "daily").glob("2026-01-*.md"))     # old notes folded away


def test_archive_never_overwrites_a_different_existing_gz(tmp_path):
    """Before the fix a log that reappeared for an already-archived date replaced the old .gz (data loss)."""
    (tmp_path / "logs").mkdir()
    f = tmp_path / "logs" / "decisions-2026-01-05.jsonl"
    f.write_text('{"action": "BUY", "reason": "first"}\n')
    ar.archive_logs(tmp_path, date(2026, 3, 1))
    f.write_text('{"action": "SELL", "reason": "late copy"}\n')
    ar.archive_logs(tmp_path, date(2026, 3, 1))
    got = sorted(gzip.decompress(p.read_bytes()).decode() for p in (tmp_path / "archive" / "2026-01").glob("*.gz"))
    assert len(got) == 2 and any("first" in g for g in got) and any("late copy" in g for g in got)
    assert len(ar.read_summary(tmp_path / "data" / "daily_summary.csv")) == 1


def test_archive_rerun_after_a_crash_does_not_duplicate(tmp_path):
    (tmp_path / "logs").mkdir()
    f = tmp_path / "logs" / "ticks-2026-01-05.csv"
    f.write_text("time,value,floor,cash,liquidbees\n10:00,100000,99500,0,100000\n")
    body = f.read_bytes()
    ar.archive_logs(tmp_path, date(2026, 3, 1))
    f.write_bytes(body)                                           # as if the unlink never happened
    ar.archive_logs(tmp_path, date(2026, 3, 1))
    assert [p.name for p in (tmp_path / "archive" / "2026-01").glob("*.gz")] == ["ticks-2026-01-05.csv.gz"]


def test_fastlane_request_log_is_trimmed_without_loss(tmp_path):
    (tmp_path / "state").mkdir()
    src = tmp_path / "state" / "fastlane_requests.jsonl"
    lines = [json.dumps({"ts": f"2026-01-{d:02d}T10:00:00+05:30", "n": d}) for d in range(1, 29)]
    lines += [json.dumps({"ts": "2026-02-27T10:00:00+05:30", "n": 99}), "not json"]
    src.write_text("\n".join(lines) + "\n")
    ar.archive_fastlane_requests(tmp_path, date(2026, 3, 10))
    assert ar.archive_fastlane_requests(tmp_path, date(2026, 3, 10)) == []
    kept = src.read_text().splitlines()
    assert kept == [lines[-2], "not json"]
    arch = gzip.decompress((tmp_path / "archive" / "2026-01" / "fastlane_requests-2026-01.jsonl.gz").read_bytes()).decode()
    assert arch.splitlines() == lines[:28]
