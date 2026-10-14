"""Replay 14 Oct 2026's news-trigger headlines through the real engine under several trigger-rule options.

usage: PYTHONPATH=<repo> python docs/proposals/2026-10-14-news-trigger-replay.py <repo> <fastlane_requests.jsonl>...
Each request line = one tick at which >= 1 headline woke the lane; its triggers carry the headlines. Every headline
is fed to the engine at the tick it first fired (duplicates within a tick removed), with prices and no series, so only
news triggers can block. Counts BLOCK decisions with trigger "News trigger".
"""
import copy
import json
import sys
import tempfile
from datetime import datetime
from functools import partial
from pathlib import Path

import yaml

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))
from simgold import config, triggers  # noqa: E402
from simgold.broker import Portfolio  # noqa: E402
from simgold.engine import Env, Market, run_tick  # noqa: E402

ticks = []
for f in sys.argv[2:]:
    for line in open(f, encoding="utf-8"):
        r = json.loads(line)
        if not r["ts"].startswith("2026-10-14"):
            continue
        heads = []
        for t in r["triggers"]:
            h = t.get("headline")
            if h and h not in heads:
                heads.append(h)
        ticks.append((datetime.fromisoformat(r["ts"]), heads))
ticks.sort(key=lambda x: x[0])

new = yaml.safe_load((repo / "config" / "news_triggers.yaml").read_text(encoding="utf-8"))


def variant(decision=False, context=False, cooldown=False):
    y = copy.deepcopy(new)
    g = y["global_rules"]
    mp = y["categories"]["monetary_policy"]
    if not decision:
        mp["urgency"] = 4
        mp.pop("escalate", None)
        g.pop("count_min_urgency", None)
    if not context:
        y.pop("context_phrases", None)
    if not cooldown:
        g.pop("cooldown_minutes", None)
    return y


OPTIONS = [
    ("baseline (rules on main)", variant()),
    ("A  decision words (+ count only urgency>=3)", variant(decision=True)),
    ("B  2 h per-category cooldown", variant(cooldown=True)),
    ("C  ignore previews/outlooks/explainers", variant(context=True)),
    ("A+C", variant(decision=True, context=True)),
    ("D  A+B+C (recommended)", variant(decision=True, context=True, cooldown=True)),
]

P = config.load_params()
results = []
with tempfile.TemporaryDirectory() as td:
    for name, y in OPTIONS:
        path = Path(td) / (str(len(results)) + ".yaml")
        path.write_text(yaml.safe_dump(y, sort_keys=False), encoding="utf-8")
        triggers.load_rules.cache_clear(); triggers._compile.cache_clear()
        env = Env(Path(td) / f"s{len(results)}", Path(td) / f"l{len(results)}", P, environ={}, post=lambda *a, **k: None,
                  match_headline=partial(triggers.match_headline, path=str(path)))
        pf = Portfolio(); pf.cash, pf.liquidbees, pf.last_accrual = 0.0, 100000.0, "2026-10-14"
        blocks, fired_heads, per_sym = 0, set(), {}
        for now, heads in ticks:
            m = Market(now, {"GOLDBEES": 120.0, "SILVERBEES": 203.0, "HINDCOPPER": 468.0},
                       {s: now for s in ("GOLDBEES", "SILVERBEES", "HINDCOPPER")}, {}, headlines=[{"title": h} for h in heads])
            for r in run_tick(pf, m, env):
                if r["action"] == "BLOCK" and r["trigger"] == "News trigger":
                    blocks += 1
                    per_sym[r["symbol"]] = per_sym.get(r["symbol"], 0) + 1
            for h in heads:
                mm = triggers.match_headline(h, path=str(path))
                if mm["wake"]:
                    fired_heads.add(h)
        results.append((name, blocks, per_sym, sorted(fired_heads)))

all_heads = sorted({h for _, hs in ticks for h in hs})
print(f"ticks with a news trigger: {len(ticks)}, distinct headlines that fired on main: {len(all_heads)}")
for name, b, ps, fh in results:
    print(f"{name:45s} blocks={b:3d}  {ps}  waking headlines={len(fh)}")
print()
for name, b, ps, fh in results[1:]:
    if name.startswith("D"):
        print("still waking under D:")
        for h in fh:
            print("  ", h)
