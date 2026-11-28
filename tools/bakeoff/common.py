"""Paths and small helpers shared by the bake-off CLIs. Everything the bake-off writes lives under reports/bakeoff/."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def bootstrap() -> None:
    for p in (ROOT / "tools", ROOT):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def bakeoff_dir(root: Path | str = ROOT) -> Path:
    return Path(root) / "reports" / "bakeoff"


def check_out(path: Path | str, root: Path | str = ROOT) -> Path:
    """The path itself, if it is inside <root>/reports/bakeoff; otherwise raise (the bake-off writes nowhere else)."""
    p = Path(path).resolve()
    base = bakeoff_dir(root).resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"refusing to write outside {base}: {p.name}")
    return p


def jdump(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def jwrite(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(jdump(obj), encoding="utf-8")
    tmp.replace(path)
