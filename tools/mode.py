#!/usr/bin/env python3
"""The routines' MODE command (read only; paper trading only).

  python tools/mode.py          prints  MODE=<effective strategy mode> PHASE=<allocation.phase or none>
  python tools/mode.py --full   adds    AI_TRUST=<..|none> FANOUT=<..|none> INVESTED_TARGET=<..|none>

The effective mode is what the engine trades under, not the raw `strategy.mode` line: a `v2_trend` config whose `allocation.phase` is
`live_default` or `live` is v3_alloc (the owner's single go-live switch). simgold.config.load_params() applies that mapping on newer
code; this tool applies the same rule itself when the loader has not (idempotent), so the output is identical either way.

  v1_scores + any phase                    -> v1_scores
  v2_trend + off | shadow | absent | other -> v2_trend
  v2_trend + live_default | live           -> v3_alloc
  v3_alloc + any phase                     -> v3_alloc (PHASE still shown)

Exit 0 and one line on stdout. The RAW `strategy.mode` must be exactly v1_scores, v2_trend or v3_alloc (no case-folding, no trimming:
`V3_ALLOC` is not v3_alloc). If the config cannot be read, or the mode is missing, null, empty or anything else, nothing goes to stdout,
a reason goes to stderr and the exit code is 2; the playbooks treat that exactly like v2_trend (legacy playbooks, v3 dormant).
The phase is matched case-insensitively and trimmed, exactly as simgold.config's loader does, so the effective mode is the same on every
input the loader accepts (tests/test_mode_tool.py checks parity cell by cell). It never writes anything.
"""
from __future__ import annotations

import argparse
import re
import sys

from _common import ROOT, utf8_stdout  # noqa: F401  (puts the repo root on sys.path)

LIVE_PHASES = ("live_default", "live")
VALID_MODES = ("v1_scores", "v2_trend", "v3_alloc")


def _clean(v) -> str:
    """One safe token for the output line: none for absent, otherwise at most 24 chars of [a-z0-9_.-] (anything else becomes ?)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return "none"
    return re.sub(r"[^a-z0-9_.\-]", "?", str(v).strip().lower())[:24] or "none"


def effective_mode(params: dict) -> str:
    """Raw strategy.mode with the go-live mapping applied. Does not change `params`. Raises ValueError unless the raw mode is exactly one
    of VALID_MODES (KeyError/TypeError when there is no strategy.mode at all)."""
    mode = params["strategy"]["mode"]
    if not isinstance(mode, str) or mode not in VALID_MODES:
        raise ValueError(f"strategy.mode {str(mode)[:24]!r} is not one of {', '.join(VALID_MODES)}")
    phase = str((params.get("allocation") or {}).get("phase", "")).strip().lower()
    if mode == "v2_trend" and phase in LIVE_PHASES:
        return "v3_alloc"
    return mode


def line(params: dict, full: bool = False) -> str:
    a = params.get("allocation") or {}
    out = f"MODE={_clean(effective_mode(params))} PHASE={_clean(a.get('phase'))}"
    if full:
        d = a.get("desks")
        out += (f" AI_TRUST={_clean(a.get('ai_trust'))} FANOUT={_clean(d.get('fanout') if isinstance(d, dict) else None)}"
                f" INVESTED_TARGET={_clean(a.get('invested_target'))}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="print the effective strategy mode and allocation phase (read only)")
    ap.add_argument("--full", action="store_true", help="also print AI_TRUST, FANOUT and INVESTED_TARGET")
    ap.add_argument("--params", default=None, help="params file (testing only; default config/params.yaml)")
    args = ap.parse_args(argv)
    try:
        from simgold import config
        params = config.load_params(args.params) if args.params else config.load_params()
        text = line(params, args.full)
    except Exception as e:  # unreadable or malformed config: the caller treats it as v2_trend (legacy playbooks)
        print(f"mode: cannot read the config ({type(e).__name__}); treat as v2_trend", file=sys.stderr)
        return 2
    utf8_stdout()
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
