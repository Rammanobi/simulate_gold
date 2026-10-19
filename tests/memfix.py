"""Fixtures for the two-tier memory tests: synthetic price series and seeded memory/ltm + memory/stm files."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

METALS = ("GOLD", "SILVER", "COPPER")


def series(kind: str = "up", n: int = 400, end: str = "2026-10-15", seed: int = 1, base: float = 100.0) -> pd.Series:
    """Synthetic business-day closes ending at `end`: up / down / flat."""
    idx = pd.bdate_range(end=end, periods=n)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.004, n)
    drift = {"up": 0.0015, "down": -0.0015, "flat": 0.0}[kind]
    px = base * np.exp(np.cumsum(drift + noise))
    return pd.Series(px, index=idx)


def seed_memory(root: Path, ltm: dict | str = "UP", stm: dict | str = "UP", asof: str = "2026-10-13", extra: dict | None = None):
    """Write memory/ltm/regime.json and memory/stm/window.json with the given labels (str = same for all metals)."""
    ltm = {m: ltm for m in METALS} if isinstance(ltm, str) else ltm
    stm = {m: stm for m in METALS} if isinstance(stm, str) else stm
    reg = {"updated": asof, "asof": asof, "metals": {m: {"label": ltm[m], "raw_label": ltm[m], "votes": 6, "tests": 6,
                                                          "since": "2026-08-04", "asof": asof, "vol_regime": "NORMAL",
                                                          "stale": False, **((extra or {}).get(m) or {})}
                                                      for m in METALS}}
    win = {"asof": asof, "metals": {m: {"stm_state": stm[m]} for m in METALS}}
    for rel, obj in (("memory/ltm/regime.json", reg), ("memory/stm/window.json", win)):
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj), encoding="utf-8")
