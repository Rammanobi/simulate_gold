"""simgold.logmerge: the union merge driver for the append-only daily logs."""
from __future__ import annotations

import json

from simgold import logmerge

H8 = "time,value,floor,cash,liquidbees,GOLDBEES,SILVERBEES,HINDCOPPER"
H5 = "time,value,floor,cash,liquidbees"


def _run(tmp_path, base, ours, theirs, name):
    paths = []
    for n, text in (("O", base), ("A", ours), ("B", theirs)):
        p = tmp_path / n
        p.write_text(text)
        paths.append(str(p))
    assert logmerge.main(["merge-file", *paths, name]) == 0
    return (tmp_path / "A").read_text()


def test_ticks_union_sorted_one_header(tmp_path):
    out = _run(tmp_path, "", f"{H8}\n12:21,1,2,3,4,5,6,7\n", f"{H8}\n10:17,1,2,3,4,5,6,7\n12:20,1,2,3,4,5,6,7\n",
               "logs/ticks-2026-10-14.csv")
    assert out.splitlines() == [H8, "10:17,1,2,3,4,5,6,7", "12:20,1,2,3,4,5,6,7", "12:21,1,2,3,4,5,6,7"]


def test_ticks_same_minute_kept_once_and_old_rows_padded(tmp_path):
    out = _run(tmp_path, f"{H5}\n", f"{H5}\n10:00,1,2,3,4\n", f"{H8}\n10:00,9,9,9,9,9,9,9\n10:01,1,2,3,4,5,6,7\n",
               "logs/ticks-2026-10-14.csv")
    assert out.splitlines() == [H8, "10:00,1,2,3,4,,,", "10:01,1,2,3,4,5,6,7"]


def test_jsonl_union_sorted_by_ts_without_duplicates(tmp_path):
    a = [json.dumps({"ts": "2026-10-14T12:21:00+05:30", "n": 3}), json.dumps({"ts": "2026-10-14T10:20:00+05:30", "n": 1})]
    b = [json.dumps({"ts": "2026-10-14T10:20:00+05:30", "n": 1}), json.dumps({"ts": "2026-10-14T11:00:00+05:30", "n": 2})]
    out = _run(tmp_path, "", "\n".join(a) + "\n", "\n".join(b) + "\n", "logs/decisions-2026-10-14.jsonl")
    assert [json.loads(x)["n"] for x in out.splitlines()] == [1, 2, 3]


def test_jsonl_without_ts_keeps_order_ours_first(tmp_path):
    out = _run(tmp_path, "", '{"a":1}\nnot json\n', '{"a":1}\n{"b":2}\n', "logs/health-2026-10-14.jsonl")
    assert out.splitlines() == ['{"a":1}', "not json", '{"b":2}']


def test_missing_files_are_empty_sides(tmp_path):
    (tmp_path / "A").write_text(f"{H8}\n09:15,1,2,3,4,5,6,7\n")
    assert logmerge.main(["merge-file", str(tmp_path / "nope"), str(tmp_path / "A"), str(tmp_path / "nope2"),
                          "logs/ticks-x.csv"]) == 0
    assert (tmp_path / "A").read_text().splitlines() == [H8, "09:15,1,2,3,4,5,6,7"]
