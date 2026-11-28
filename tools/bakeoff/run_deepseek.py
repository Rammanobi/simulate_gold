#!/usr/bin/env python3
"""DeepSeek side of the bake-off: every frozen input x N runs, written under reports/bakeoff/<run-id>/deepseek/<task>/NN.<run>.json
(deepseek-pro/ for --models pro).

  python tools/bakeoff/run_deepseek.py --run-id 2026-11-29 [--tasks report_paragraph,weekly_rollup,manifest] [--runs 3]
         [--models flash[,pro]] [--max-cost 3] [--off-peak-only] [--workers 4] [--dry-run] [--limit N]

* Uses simgold.fallback_llm.Client; model ids are resolved at run time from the /models list (deepseek-flash, deepseek-v4-pro).
  Thinking is OFF (flash always; pro unless --pro-thinking). Temperature low. JSON mode.
* Hard cost cap: at most $3 (the --max-cost value can only lower it), computed from the response `usage` fields at PEAK prices
  (so it is conservative off-peak), checked before every call; a stop leaves complete earlier runs (run-major order).
* The key is read from DEEPSEEK_API_KEY only, never printed or stored; every string written is scrubbed of it;
  `reasoning_content` is never read. The privacy scanner checks the inputs before any call and each outgoing payload.
* Writes only under reports/bakeoff/. Re-running skips results that already exist (--no-resume to redo).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path[:0] = [p for p in (str(Path(__file__).resolve().parents[2] / "tools"), str(Path(__file__).resolve().parents[2])) if p not in sys.path]

from bakeoff import inputs as inp, privacy, tasks     # noqa: E402
from bakeoff.common import ROOT, bakeoff_dir, check_out, jwrite    # noqa: E402
from simgold import fallback_llm      # noqa: E402

HARD_MAX_USD = 3.0
SCHEMA = 1
# USD per 1M tokens at PEAK prices (plan section 3): cache hit, cache miss, output. Off-peak is half; the cap ignores that.
PRICES = {"flash": (0.006, 0.30, 1.20), "pro": (0.044, 1.32, 3.96)}
TIER = {"flash": "fast", "pro": "thinking"}
SIDE = {"flash": "deepseek", "pro": "deepseek-pro"}      # result folder per model
KEY_ENV = "DEEPSEEK_API_KEY"


def is_peak(dt: datetime) -> bool:
    """Peak = 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday."""
    dt = dt.astimezone(timezone.utc)
    return dt.weekday() < 5 and (1 <= dt.hour < 4 or 6 <= dt.hour < 10)


def usage_cost(usage: dict, label: str) -> float:
    hit_p, miss_p, out_p = PRICES[label]
    pt = int(usage.get("prompt_tokens") or 0)
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    miss = int(miss) if miss is not None else max(pt - hit, 0)
    return (hit * hit_p + miss * miss_p + int(usage.get("completion_tokens") or 0) * out_p) / 1e6


def estimate_call(messages: list[dict], max_tokens: int, label: str) -> float:
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return usage_cost({"prompt_tokens": chars // 2 + 50, "completion_tokens": max_tokens}, label)


class CostMeter:
    def __init__(self, cap: float):
        self.cap = min(float(cap), HARD_MAX_USD)
        self.spent = 0.0
        self.calls = 0
        self.lock = threading.Lock()

    def add(self, c: float) -> None:
        with self.lock:
            self.spent += c
            self.calls += 1

    def allows(self, projected: float) -> bool:
        with self.lock:
            return self.spent + projected <= self.cap


class Recorder(threading.local):
    """What one chat() call saw: usage of every HTTP 200 attempt, finish reason, timing. Never the reply body."""

    def reset(self):
        self.usages, self.finish, self.last_ms, self.model, self.t0 = [], None, None, None, time.monotonic()


def make_request(transport, rec: Recorder, thinking_off: bool):
    def request(method, url, headers, body, timeout):
        if method == "POST" and body is not None:
            body = dict(body)
            if thinking_off:
                body["thinking"] = {"type": "disabled"}
        t0 = time.monotonic()
        status, d = transport(method, url, headers, body, timeout)
        if method == "POST":
            rec.last_ms = int((time.monotonic() - t0) * 1000)
            if status == 200:
                if isinstance(d.get("usage"), dict):
                    rec.usages.append(dict(d["usage"]))
                try:
                    rec.finish = d["choices"][0].get("finish_reason")
                except (KeyError, IndexError, TypeError, AttributeError):
                    pass
                rec.model = d.get("model") or (body or {}).get("model")
            # keep only usage-like data: drop the reply (and any reasoning_content) before it goes back to the Client
            if status == 200 and isinstance(d.get("choices"), list):
                d = {"choices": [{"message": {"content": (d["choices"][0].get("message") or {}).get("content")},
                                  "finish_reason": d["choices"][0].get("finish_reason")}] if d["choices"] else [],
                     "usage": d.get("usage"), "model": d.get("model")}
        return status, d
    return request


def scrub(s, key: str):
    return s.replace(key, "***") if isinstance(s, str) and key else s


def one_call(client, rec, task, doc, label, key, meter) -> dict:
    """Run one chat call. Returns the usage/latency/output part of a result record."""
    rec.reset()
    client.cfg["max_tokens"][TIER[label]] = task.max_tokens
    raw, err = None, None
    try:
        raw = client.chat(TIER[label], doc["messages"], json_mode=True)
    except (fallback_llm.FallbackError, OSError) as e:
        err = scrub(str(e), key)[:300]
    cost = sum(usage_cost(u, label) for u in rec.usages)
    meter.add(cost)
    tot = lambda k: sum(int(u.get(k) or 0) for u in rec.usages)      # noqa: E731
    raw = scrub(raw, key)                      # scrub BEFORE parsing, so the parsed output cannot carry the key either
    out, perr = tasks.parse_output(raw) if raw is not None else (None, err)
    return {"output": out, "raw_text": raw[:6000] if raw else None, "error": perr if out is None else None,
            "latency_ms": rec.last_ms, "wall_ms": int((time.monotonic() - rec.t0) * 1000), "finish_reason": rec.finish,
            "model": rec.model or client.chosen.get(TIER[label]), "cost_usd": round(cost, 6),
            "usage": {"prompt_tokens": tot("prompt_tokens"), "completion_tokens": tot("completion_tokens"),
                      "cache_hit_tokens": tot("prompt_cache_hit_tokens"), "attempts_billed": len(rec.usages)}}


def plan(inputs_dir: Path, names, limit: int | None) -> list[tuple[tasks.Task, str]]:
    man = inp.load_manifest(inputs_dir)
    out = []
    for t in tasks.select(names):
        ids = [e["id"].split("/")[1] for e in man["tasks"][t.name]["inputs"]]
        out += [(t, nn) for nn in (ids[:limit] if limit else ids)]
    return out


def run(argv=None, environ=None, transport=None, now=None, sleep=None) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--tasks", default=None)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--models", default="flash")
    ap.add_argument("--max-cost", type=float, default=HARD_MAX_USD)
    ap.add_argument("--off-peak-only", action="store_true")
    ap.add_argument("--pro-thinking", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--deadline-min", type=float, default=25.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root)
    inputs_dir = bakeoff_dir(root) / "inputs"
    base = check_out(bakeoff_dir(root) / a.run_id, root)
    run_dir = base / "deepseek"
    labels = [m.strip() for m in a.models.split(",") if m.strip()]
    if any(m not in PRICES for m in labels):
        print("--models takes flash and/or pro")
        return 2
    key = (environ.get(KEY_ENV) or "").strip()
    now = now or datetime.now(timezone.utc)

    probs = inp.verify(inputs_dir, root)             # hashes + privacy scan, before any API call
    if probs:
        print("inputs failed verification, no API call made:\n" + "\n".join(probs[:20]))
        return 1
    man = inp.load_manifest(inputs_dir)
    work = plan(inputs_dir, a.tasks, a.limit)
    meter = CostMeter(a.max_cost)
    est = sum(estimate_call(inp.load_input(inputs_dir, t.name, nn)["messages"], t.max_tokens // 4, lb) for t, nn in work for lb in labels) * a.runs
    print(f"plan: {len(work)} inputs x {a.runs} runs x {len(labels)} model(s) = {len(work) * a.runs * len(labels)} calls; "
          f"rough peak cost ${est:.2f}; cap ${meter.cap:.2f}")
    if a.dry_run:
        return 0
    if a.off_peak_only and is_peak(now):
        print("peak hours (01-04 and 06-10 UTC, Mon-Fri): nothing run; start again off-peak")
        return 0
    if not key:
        print(f"{KEY_ENV} is not set; nothing run")
        return 2

    sc = privacy.Scanner(root)
    exempt = {i["id"]: [i["plant"]["text"]] for t in man["tasks"].values() for i in t["inputs"] if i.get("plant")}
    rec = Recorder()
    clients = {}
    for lb in labels:
        cfg = fallback_llm.load_cfg(None)
        cfg.update({"temperature": 0.1, "timeout_s": 90, "retries": 1})
        cfg["models"] = {"fast": ["deepseek-flash", "deepseek-v4-flash"], "thinking": ["deepseek-v4-pro"]}
        cfg["max_tokens"] = dict(cfg["max_tokens"])
        req = make_request(transport or fallback_llm.http, rec, thinking_off=(lb == "flash" or not a.pro_thinking))
        clients[lb] = fallback_llm.Client(key, cfg, request=req, sleep=sleep or time.sleep)
        have = clients[lb].list_models()
        print(f"{lb}: resolved {clients[lb].candidates(TIER[lb])[:1] or 'NONE'} (account lists {len(have or [])} models)")

    jobs = [(r, t, nn, lb) for r in range(1, a.runs + 1) for lb in labels for t, nn in work]     # run-major: a stop keeps whole runs
    state = {"stopped": None, "done": 0, "skipped": 0, "errors": 0}
    t_end = time.monotonic() + a.deadline_min * 60
    lock = threading.Lock()

    def worker(job):
        r, t, nn, lb = job
        dest = base / SIDE[lb] / t.name / f"{nn}.{r}.json"
        if dest.exists() and not a.no_resume:
            with lock:
                state["skipped"] += 1
            return
        if state["stopped"]:
            return
        if time.monotonic() > t_end:
            state["stopped"] = state["stopped"] or "deadline"
            return
        doc = inp.load_input(inputs_dir, t.name, nn)
        hits = sc.scan_messages(doc["messages"], doc["id"], exempt.get(doc["id"], []))
        if hits:
            with lock:
                state["stopped"] = "privacy"
            return
        if not meter.allows(estimate_call(doc["messages"], t.max_tokens, lb)):
            with lock:
                state["stopped"] = "cost_cap"
            return
        res = one_call(clients[lb], rec, t, doc, lb, key, meter)
        out = {"schema": SCHEMA, "run_id": a.run_id, "side": SIDE[lb], "task": t.name, "input_id": doc["id"], "run": r,
               "label": lb, "thinking": "on" if (lb == "pro" and a.pro_thinking) else "off", **res}
        jwrite(check_out(dest, root), out)
        with lock:
            state["done"] += 1
            state["errors"] += 1 if res["error"] else 0
            if meter.spent >= meter.cap:
                state["stopped"] = state["stopped"] or "cost_cap"

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        list(ex.map(worker, jobs))
    meta = {"schema": SCHEMA, "run_id": a.run_id, "tasks": [t.name for t in tasks.select(a.tasks)], "runs": a.runs, "models": labels,
            "resolved": {lb: clients[lb].candidates(TIER[lb])[:1] for lb in labels}, "max_cost_usd": meter.cap,
            "spent_usd": round(meter.spent, 4), "calls": meter.calls, "written": state["done"], "skipped_existing": state["skipped"],
            "call_errors": state["errors"], "stopped_by": state["stopped"], "thinking": "off" if not a.pro_thinking else "pro on",
            "finished_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    jwrite(check_out(run_dir / "run_meta.json", root), meta)
    print(f"done: {state['done']} written, {state['skipped']} existing, {state['errors']} errors, spent ${meter.spent:.4f}"
          + (f", STOPPED by {state['stopped']}" if state["stopped"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
