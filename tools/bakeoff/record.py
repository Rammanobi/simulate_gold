#!/usr/bin/env python3
"""Claude-side helper: wrap one answer in the common results schema and write it under reports/bakeoff/<run-id>/<side>/.

  python tools/bakeoff/record.py --run-id R --side sonnet --model claude-sonnet-5-5 --input report_paragraph/07 --run 1 --answer-file ans.txt

ans.txt holds the model's raw reply (JSON). Nothing else is written; paths outside reports/bakeoff/ are refused.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path[:0] = [p for p in (str(Path(__file__).resolve().parents[2] / "tools"), str(Path(__file__).resolve().parents[2])) if p not in sys.path]

from bakeoff import tasks      # noqa: E402
from bakeoff.common import ROOT, bakeoff_dir, check_out, jwrite    # noqa: E402


def record(root: Path, run_id: str, side: str, model: str, input_id: str, run: int, raw: str, latency_ms=None, tokens_in=None, tokens_out=None) -> Path:
    task, nn = input_id.split("/")
    t = tasks.get(task)
    out, err = tasks.parse_output(raw)
    dest = check_out(bakeoff_dir(root) / run_id / side / t.name / f"{nn}.{run}.json", root)
    jwrite(dest, {"schema": 1, "run_id": run_id, "side": side, "model": model, "task": t.name, "input_id": f"{t.name}/{nn}", "run": run,
                  "output": out, "raw_text": raw[:6000], "error": err, "latency_ms": latency_ms, "finish_reason": None,
                  "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}, "cost_usd": None})
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--side", required=True, choices=["sonnet", "opus"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--run", type=int, default=1)
    ap.add_argument("--answer-file", required=True)
    ap.add_argument("--root", default=str(ROOT))
    a = ap.parse_args(argv)
    p = record(Path(a.root), a.run_id, a.side, a.model, a.input, a.run, Path(a.answer_file).read_text(encoding="utf-8"))
    print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
