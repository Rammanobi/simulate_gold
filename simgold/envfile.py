"""A tiny .env loader (stdlib only). Existing environment variables always win; the file is never written."""
from __future__ import annotations

import os
from pathlib import Path


def parse(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k.replace("_", "").isalnum() and k:
            out[k] = v
    return out


def load_env(path=".env", environ=None) -> list[str]:
    """Set variables from `path` that are not already set (empty values are skipped). Returns the names it set."""
    environ = os.environ if environ is None else environ
    try:
        data = parse(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []
    set_ = []
    for k, v in data.items():
        if v and not environ.get(k):
            environ[k] = v
            set_.append(k)
    return set_
