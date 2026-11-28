"""Build, verify and export the frozen bake-off inputs: reports/bakeoff/inputs/<task>/NN.json + manifest.json (sha256 each).

  python tools/bakeoff/inputs.py build   [--root .] [--out reports/bakeoff/inputs] [--tasks a,b]
  python tools/bakeoff/inputs.py verify  [--root .] [--inputs DIR]       (hashes + privacy scan)
  python tools/bakeoff/inputs.py export-blind --out DIR                  (input files only, no manifest: for Claude-side agents)

Deterministic: the same repo files give the same bytes (seeded sampling, no timestamps). Each input file holds the exact chat
messages every model sees, plus the payload the validators use. The injection plants (10 of 30 per task) are recorded in the
manifest only; an input file does not say whether it carries one. The privacy scanner must be clean before any file is kept.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from pathlib import Path

sys.path[:0] = [p for p in (str(Path(__file__).resolve().parents[2] / "tools"), str(Path(__file__).resolve().parents[2])) if p not in sys.path]

from bakeoff import inject, privacy, tasks     # noqa: E402
from bakeoff.common import ROOT, bakeoff_dir, check_out, jdump, jwrite    # noqa: E402

SCHEMA = 1


class BuildError(Exception):
    pass


def input_doc(task: tasks.Task, nn: str, key: str, payload: dict) -> dict:
    return {"schema": SCHEMA, "id": f"{task.name}/{nn}", "task": task.name, "key": key,
            "messages": task.messages(payload), "payload": payload}


def build(root: Path | str = ROOT, out: Path | str | None = None, task_names=None, seed: str = tasks.SEED) -> dict:
    root = Path(root)
    out = Path(out) if out else bakeoff_dir(root) / "inputs"
    sc = privacy.Scanner(root)
    man = {"schema": SCHEMA, "seed": seed, "count_per_task": tasks.N_INPUTS, "plants_per_task": inject.N_PLANT,
           "styles": list(inject.STYLES), "tasks": {}}
    docs: dict[str, str] = {}
    for t in tasks.select(task_names):
        cands, excluded = [], []
        for c in t.candidates(root):
            bad = sc.scan_sources([s["path"] for s in c.sources], c.key) + sc.scan_messages(t.messages(c.payload), c.key)
            (excluded if bad else cands).append({"key": c.key, "reason": [b.split(": ", 1)[-1] for b in bad][:3]} if bad else c)
        picked = t.pick(cands, seed)
        idx = inject.plant_indices(f"{seed}:{t.name}")
        plants = inject.assign(f"{seed}:{t.name}", idx)
        entries = []
        for i, c in enumerate(picked):
            nn = f"{i + 1:02d}"
            payload, plant = c.payload, None
            if i in plants:
                plant = dict(plants[i])
                payload, plant["location"] = t.plant(c.payload, plant["text"], random.Random(f"{seed}:{t.name}:pos:{nn}"))
            doc = input_doc(t, nn, c.key, payload)
            # the clean input must pass the scanner; the plant is synthetic and is exempted by its exact text
            bad = sc.scan_messages(doc["messages"], doc["id"], exempt=[plant["text"]] if plant else []) + sc.scan_sources(
                [s["path"] for s in c.sources], doc["id"])
            if bad:
                raise BuildError("privacy scan failed: " + "; ".join(bad[:5]))
            text = jdump(doc)
            rel = f"{t.name}/{nn}.json"
            docs[rel] = text
            entries.append({"id": doc["id"], "file": rel, "sha256": tasks.sha256_text(text), "key": c.key, "sources": c.sources,
                            "plant": plant})
        man["tasks"][t.name] = {"code": t.code, "system_sha256": tasks.sha256_text(t.system), "inputs": entries,
                                "injected_ids": [e["id"] for e in entries if e["plant"]], "excluded": excluded}
    if out.exists() and task_names is None:
        shutil.rmtree(out)
    for rel, text in docs.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    if task_names is not None and (out / "manifest.json").exists():     # partial rebuild keeps the other tasks
        old = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        old["tasks"].update(man["tasks"])
        man["tasks"] = old["tasks"]
    jwrite(out / "manifest.json", man)
    return man


def load_manifest(inputs_dir: Path | str) -> dict:
    return json.loads((Path(inputs_dir) / "manifest.json").read_text(encoding="utf-8"))


def verify(inputs_dir: Path | str, root: Path | str = ROOT) -> list[str]:
    """Problems found (empty = the frozen inputs match the manifest and the privacy scan is clean)."""
    d = Path(inputs_dir)
    probs: list[str] = []
    man = load_manifest(d)
    seen = set()
    for name, t in man["tasks"].items():
        if len(t["inputs"]) != man.get("count_per_task", 30):
            probs.append(f"{name}: {len(t['inputs'])} inputs")
        if len(t["injected_ids"]) != man.get("plants_per_task", 10):
            probs.append(f"{name}: {len(t['injected_ids'])} plants")
        for e in t["inputs"]:
            f = d / e["file"]
            seen.add(e["file"])
            if not f.exists():
                probs.append(f"{e['file']}: missing")
            elif tasks.sha256_bytes(f.read_bytes()) != e["sha256"]:
                probs.append(f"{e['file']}: hash differs from manifest")
    for f in d.rglob("*.json"):
        rel = f.relative_to(d).as_posix()
        if rel != "manifest.json" and rel not in seen:
            probs.append(f"{rel}: not in manifest")
    probs += privacy.scan_inputs(d, root)
    return probs


def load_input(inputs_dir: Path | str, task: str, nn: str) -> dict:
    return json.loads((Path(inputs_dir) / task / f"{nn}.json").read_text(encoding="utf-8"))


def plants_by_id(man: dict) -> dict[str, dict]:
    return {i["id"]: i["plant"] for t in man["tasks"].values() for i in t["inputs"] if i.get("plant")}


def export_blind(inputs_dir: Path | str, out: Path | str) -> int:
    n = 0
    for f in sorted(Path(inputs_dir).rglob("*.json")):
        if f.name == "manifest.json":
            continue
        dest = Path(out) / f.relative_to(inputs_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build", "verify", "export-blind"])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--out", default=None)
    ap.add_argument("--inputs", default=None)
    ap.add_argument("--tasks", default=None)
    a = ap.parse_args(argv)
    root = Path(a.root)
    inputs = Path(a.inputs) if a.inputs else bakeoff_dir(root) / "inputs"
    if a.cmd == "build":
        out = check_out(a.out or inputs, root)
        man = build(root, out, a.tasks.split(",") if a.tasks else None)
        for k, t in man["tasks"].items():
            print(f"{k}: {len(t['inputs'])} inputs, {len(t['injected_ids'])} with a plant, {len(t['excluded'])} candidates excluded by the privacy scan")
        return 0
    if a.cmd == "verify":
        probs = verify(inputs, root)
        print("\n".join(probs) if probs else "inputs verified: hashes match, privacy scan clean")
        return 1 if probs else 0
    if not a.out:
        print("export-blind needs --out DIR")
        return 2
    print(f"{export_blind(inputs, a.out)} input files copied (no manifest)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
