"""Keyword matcher for the news "fast lane".

Reads config/news_triggers.yaml and decides, for one headline, which
categories it touches, which instruments it affects, and whether it should
wake the fast lane.

Matching rules (see the YAML header and RULEBOOK 5.2):
  - Case-insensitive.
  - Whole words or phrases only, using word boundaries, so "fed" does not
    match "federal" and "war" does not match "toward" or "award".
  - The ignore list is checked first. Any ignore hit returns the no-match result.
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


@dataclass(frozen=True)
class _Matcher:
    ignore: re.Pattern | None
    categories: tuple[_Category, ...]


@functools.lru_cache(maxsize=None)
def _compile(path: str) -> _Matcher:
    rules = load_rules(path)

    ignore_terms = [str(t) for t in (rules.get("ignore") or [])]
    ignore = (
        re.compile("|".join(_phrase_regex(t) for t in ignore_terms), re.IGNORECASE)
        if ignore_terms
        else None
    )

    categories = []
    for name, cat in rules["categories"].items():
        pattern = re.compile(
            "|".join(_phrase_regex(k) for k in cat["keywords"]), re.IGNORECASE
        )
        categories.append(
            _Category(
                name=str(name),
                urgency=int(cat["urgency"]),
                affects=tuple(str(s) for s in cat["affects"]),
                pattern=pattern,
            )
        )
    return _Matcher(ignore=ignore, categories=tuple(categories))


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
          "wake": bool,              # urgency >= 4 or 2+ categories matched
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

    urgency = max(c.urgency for c in hits)
    symbols = sorted({_clean_symbol(s) for c in hits for s in c.affects})
    wake = urgency >= WAKE_URGENCY or len(hits) >= WAKE_CATEGORY_COUNT
    return {
        "urgency": urgency,
        "categories": [c.name for c in hits],
        "symbols": symbols,
        "wake": wake,
    }
