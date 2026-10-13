"""Headline collection (Google News RSS) and per-asset sentiment.

Scoring path:
  * ANTHROPIC_API_KEY set  → Claude Haiku scores each *new* headline (cheap, batched).
  * otherwise              → transparent keyword lexicon.
Scores decay with a half-life so stale news fades out on its own.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

import feedparser

from . import config
from .llm import score_headlines

log = logging.getLogger(__name__)
ASSET_KEYS = list(config.ASSETS)  # GOLD, SILVER, COPPER

_PRECIOUS_UP = ["rate cut", "dovish", "safe haven", "safe-haven", "geopolitical", "war", "missile",
                "central bank buy", "central banks buy", "inflation rises", "inflation jumps",
                "dollar weakens", "dollar falls", "dollar slips", "yields fall", "rupee falls",
                "rupee weakens", "rupee slips", "record high", "all-time high", "surge", "rally",
                "jumps", "soars", "rise", "rises", "gains", "weaker dollar", "falling yields", "rupee weakness", "etf inflow", "festive demand", "dhanteras", "recession fears"]
_PRECIOUS_DN = ["rate hike", "hikes rate", "hawkish", "dollar strengthens", "dollar rises", "dollar jumps",
                "yields rise", "yields jump", "selloff", "sell-off", "slump", "plunge", "tumbles",
                "profit booking", "profit-taking", "import duty hike", "ceasefire", "rupee gains",
                "rupee rises", "rupee strengthens", "stronger dollar", "etf outflow", "falls", "slips", "drops"]
_COPPER_UP = ["china stimulus", "supply disruption", "mine strike", "mine closure", "deficit",
              "demand rises", "record high", "rally", "surge", "ev demand", "infrastructure push",
              "rate cut", "dollar weakens"]
_COPPER_DN = ["surplus", "inventories rise", "stockpiles rise", "china slowdown", "weak demand",
              "property slump", "tariff", "selloff", "slump", "plunge", "falls", "drops", "dollar strengthens"]

ASSET_WORDS = {
    "GOLD": ["gold", "bullion", "goldbees", "yellow metal"],
    "SILVER": ["silver", "silverbees"],
    "COPPER": ["copper", "hindustan copper", "hindcopper", "lme", "base metal"],
}
MACRO_WORDS = ["fed", "federal reserve", "rbi", "dollar", "rupee", "inflation", "yield", "fomc", "cpi"]


def _hits(text: str, words: list[str]) -> int:
    return sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"\b", text))


def lexicon_score(title: str) -> dict:
    t = title.lower()
    out = {}
    macro = _hits(t, MACRO_WORDS) > 0
    for asset, words in ASSET_WORDS.items():
        direct = _hits(t, words) > 0
        if not direct and not macro:
            out[asset] = 0.0
            continue
        up, dn = (_COPPER_UP, _COPPER_DN) if asset == "COPPER" else (_PRECIOUS_UP, _PRECIOUS_DN)
        raw = _hits(t, up) - _hits(t, dn)
        out[asset] = max(-1.0, min(1.0, raw / 2)) * (1.0 if direct else 0.4)
    out["impact"] = 0.6 if any(out[a] for a in ASSET_KEYS) else 0.0
    out["why"] = "keyword lexicon"
    return out


def fetch_headlines(queries: list[str], per_query: int = 15) -> list[dict]:
    items, seen = [], set()
    for q in queries:
        url = f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            feed = feedparser.parse(url)
        except Exception as e:  # network hiccup: skip this query
            log.warning("rss failed for %r: %s", q, e)
            continue
        for e in feed.entries[:per_query]:
            title = e.get("title", "").strip()
            hid = hashlib.sha1(title.lower().encode()).hexdigest()[:12]
            if not title or hid in seen:
                continue
            seen.add(hid)
            ts = time.mktime(e.published_parsed) if e.get("published_parsed") else time.time()
            items.append({"id": hid, "title": title, "ts": ts, "source": e.get("source", {}).get("title", "")})
    return items


class NewsBook:
    """Persistent cache of scored headlines → decayed per-asset sentiment."""

    def __init__(self, path: Path):
        self.path = path
        self.items: dict[str, dict] = json.loads(path.read_text()) if path.exists() else {}
        self.last_refresh = 0.0
        self.last_new: list[dict] = []   # headlines first seen in the latest refresh (fast-lane matching)

    def refresh(self, force: bool = False, llm_score: bool = True) -> int:
        self.last_new = []
        if not force and time.time() - self.last_refresh < config.NEWS_REFRESH_MIN * 60:
            return 0
        self.last_refresh = time.time()
        queries = [a["news_query"] for a in config.ASSETS.values()] + config.GENERAL_NEWS_QUERIES
        fresh = [h for h in fetch_headlines(queries) if h["id"] not in self.items]
        if not fresh:
            return 0
        self.last_new = fresh
        scores = score_headlines(fresh) if (llm_score and os.getenv("ANTHROPIC_API_KEY")) else None
        for h in fresh:
            s = (scores or {}).get(h["id"]) or lexicon_score(h["title"])
            self.items[h["id"]] = {**h, **s}
        cutoff = time.time() - 4 * 86400
        self.items = {k: v for k, v in self.items.items() if v["ts"] >= cutoff}
        self.path.write_text(json.dumps(self.items, indent=1))
        return len(fresh)

    def sentiment(self, now: float | None = None) -> dict[str, float]:
        now = now or time.time()
        hl = config.NEWS_HALF_LIFE_H * 3600
        out = {}
        for a in ASSET_KEYS:
            num = 0.0
            for h in self.items.values():
                w = h.get("impact", 0) * math.exp(-math.log(2) * max(0, now - h["ts"]) / hl)
                num += w * h.get(a, 0)
            out[a] = math.tanh(num / 3)  # ~3 strong fresh headlines saturate the score
        return out

    def top(self, asset: str, n: int = 5) -> list[dict]:
        rel = [h for h in self.items.values() if abs(h.get(asset, 0)) > 0]
        rel.sort(key=lambda h: (h["ts"], abs(h[asset]) * h.get("impact", 0)), reverse=True)
        return [{"title": h["title"], "score": round(h[asset], 2), "why": h.get("why", "")} for h in rel[:n]]
