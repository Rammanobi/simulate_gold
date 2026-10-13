"""Check that the DeepSeek fallback works: key present, models listed, one tiny call per tier.

Prints the chosen model per tier and OK/FAIL. Never prints the key. Exit 0 = both tiers work, 1 = a problem.
Usage: python tools/fallback_check.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simgold import config, envfile, fallback_llm  # noqa: E402


def main() -> int:
    envfile.load_env(ROOT / ".env")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print("FAIL: DEEPSEEK_API_KEY is not set")
        return 1
    client = fallback_llm.Client(key, fallback_llm.load_cfg(config.load_params()))
    have = client.list_models()
    print("models on this account:", ", ".join(sorted(have)) if have else "(could not list)")
    ok = True
    for tier in ("fast", "thinking"):
        try:
            reply = client.chat(tier, [{"role": "user", "content": "Reply with the single word OK."}])
            print(f"{tier}: OK via {client.chosen.get(tier)} (reply {reply.strip()[:20]!r})")
        except fallback_llm.FallbackError as e:
            ok = False
            print(f"{tier}: FAIL {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
