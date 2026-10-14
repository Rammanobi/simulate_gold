"""Keyword matcher for the news "fast lane".

Reads config/news_triggers.yaml and decides, for one headline, which
categories it touches, which instruments it affects, and whether it should
wake the fast lane.

Matching rules (see the YAML header and RULEBOOK 5.2):
  - Case-insensitive.
  - Whole words or phrases only, using word boundaries, so "fed" does not
    match "federal" and "war" does not match "toward" or "award".
  - The ignore list is checked first. Any ignore hit returns the no-match result.

Tuning added after 14 Oct 2026 (docs/proposals/2026-10-14-news-trigger-tuning.md), all from the YAML and all
optional, so an old rules file behaves exactly as before:
  - categories.<name>.escalate: {urgency, phrases}: the category's urgency rises to `urgency` only when one of
    these decision phrases is in the headline ("raises rates", "emergency", ...). monetary_policy sits at 3 and
    escalates to 5, so commentary about the Fed or RBI no longer wakes on its own.
  - global_rules.count_min_urgency: only categories at this urgency or above count toward the
    "2+ categories" wake rule (the dollar category at 2 no longer turns every rupee/RBI story into a wake).
  - context_phrases: previews, outlooks, explainers and personal-finance pieces ("ahead of", "outlook",
    "how to", "EMI") never wake unless a category reaches urgency 5.
  - symbols are taken from the categories that carry the wake (urgency >= count_min_urgency), so a dollar
    mention no longer pauses HINDCOPPER on a gold/Fed story.
  - global_rules.cooldown_minutes is passed on in the result; the engine re-blocks a symbol for the same
    category (at the same urgency) at most once per cooldown.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = "config/news_triggers.yaml"

WAKE_URGENCY = 4          # any single category at this urgency or above wakes the lane
WAKE_CATEGORY_COUNT = 2   # or this many different categories matching the same headline


def _resolve(path: str | Path) -> Path:
    """Use the path as given if it exists, else resolve it against the repo root."""
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = REPO_ROOT / p
    return p


@functools.lru_cache(maxsize=None)
def load_rules(path: str = DEFAULT_RULES_PATH) -> dict[str, Any]:
    """Load and validate the trigger YAML. Cached per path."""
    with _resolve(path).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError(f"{path}: 'categories' must be a non-empty mapping")
    for name, cat in categories.items():
        if not isinstance(cat.get("keywords"), list) or not cat["keywords"]:
            raise ValueError(f"{path}: category {name!r} needs a non-empty keywords list")
        if not isinstance(cat.get("urgency"), int):
            raise ValueError(f"{path}: category {name!r} needs an integer urgency")
        if not isinstance(cat.get("affects"), list):
            raise ValueError(f"{path}: category {name!r} needs an affects list")

    ignore = data.get("ignore") or []
    if not isinstance(ignore, list):
        raise ValueError(f"{path}: 'ignore' must be a list")
    return data


def _phrase_regex(phrase: str) -> str:
    """Regex for one keyword or phrase: whole words, any run of whitespace between words."""
    body = r"\s+".join(re.escape(word) for word in str(phrase).split())
    return rf"(?<!\w){body}(?!\w)"


@dataclass(frozen=True)
class _Category:
    name: str
    urgency: int
    affects: tuple[str, ...]
    pattern: re.Pattern
    escalate_urgency: int | None = None
    escalate: re.Pattern | None = None


@dataclass(frozen=True)
class _Matcher:
    ignore: re.Pattern | None
    categories: tuple[_Category, ...]
    context: re.Pattern | None = None
    count_min_urgency: int = 0
    cooldown_minutes: int = 0


def _any_of(terms) -> re.Pattern | None:
    terms = [str(t) for t in (terms or [])]
    return re.compile("|".join(_phrase_regex(t) for t in terms), re.IGNORECASE) if terms else None


@functools.lru_cache(maxsize=None)
def _compile(path: str) -> _Matcher:
    rules = load_rules(path)

    ignore = _any_of(rules.get("ignore"))

    categories = []
    for name, cat in rules["categories"].items():
        pattern = re.compile(
            "|".join(_phrase_regex(k) for k in cat["keywords"]), re.IGNORECASE
        )
        esc = cat.get("escalate") or {}
        categories.append(
            _Category(
                name=str(name),
                urgency=int(cat["urgency"]),
                affects=tuple(str(s) for s in cat["affects"]),
                pattern=pattern,
                escalate_urgency=int(esc["urgency"]) if esc.get("urgency") is not None else None,
                escalate=_any_of(esc.get("phrases")),
            )
        )
    g = rules.get("global_rules") or {}
    return _Matcher(ignore=ignore, categories=tuple(categories), context=_any_of(rules.get("context_phrases")),
                    count_min_urgency=int(g.get("count_min_urgency") or 0),
                    cooldown_minutes=int(g.get("cooldown_minutes") or 0))


def _clean_symbol(symbol: str) -> str:
    s = symbol.strip()
    if s.upper().endswith(".NS"):
        s = s[:-3]
    return s


def match_headline(text: str, path: str = DEFAULT_RULES_PATH) -> dict[str, Any]:
    """Match one headline against the trigger rules.

    Returns:
        {
          "urgency": int,            # max urgency over matched categories, 0 if none
          "categories": [str, ...],  # matched category names, in YAML order
          "symbols": [str, ...],     # sorted unique symbols (no .NS suffix)
          "wake": bool,              # urgency >= 4 or 2+ categories matched (see the module doc for the tuning)
          # only when something matched:
          "levels": {name: urgency}, # per matched category, after escalation
          "wake_categories": [str],  # the categories that carry the wake (and give the symbols)
          "context": bool,           # a preview/outlook/explainer headline
          "cooldown_minutes": int,   # engine re-block cooldown per (category, symbol); 0 = none
        }
    """
    no_match: dict[str, Any] = {"urgency": 0, "categories": [], "symbols": [], "wake": False}
    if not text:
        return no_match

    matcher = _compile(str(path))
    if matcher.ignore is not None and matcher.ignore.search(text):
        return no_match

    hits = [c for c in matcher.categories if c.pattern.search(text)]
    if not hits:
        return no_match

    levels = {}
    for c in hits:
        up = c.escalate_urgency if c.escalate is not None and c.escalate.search(text) else None
        levels[c.name] = max(c.urgency, up) if up is not None else c.urgency
    urgency = max(levels.values())
    counted = [c for c in hits if levels[c.name] >= matcher.count_min_urgency]
    context = bool(matcher.context is not None and matcher.context.search(text))
    wake = urgency >= WAKE_URGENCY or len(counted) >= WAKE_CATEGORY_COUNT
    if context and urgency < 5:
        wake = False
    carriers = counted or hits
    symbols = sorted({_clean_symbol(s) for c in carriers for s in c.affects})
    return {
        "urgency": urgency,
        "categories": [c.name for c in hits],
        "symbols": symbols,
        "wake": wake,
        "levels": levels,
        "wake_categories": [c.name for c in carriers],
        "context": context,
        "cooldown_minutes": matcher.cooldown_minutes,
    }
