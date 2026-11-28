"""Objective output checks shared by every bake-off task. Code only, no network, no AI.

* validate_schema: a small JSON-schema subset (type, properties, required, additionalProperties, items, minItems, maxItems,
  minLength, maxLength, pattern, enum). Upper length caps are NOT in the task schemas; they are separate "caps" so that a
  too-long answer counts as a cap failure, not a schema failure.
* markup_issues / memsafe_issues / house_issues: plain text only, memory-safe text, house number format (₹, Indian grouping).
* fact_claims: the numbers, dates, symbols and up/down words of a text, each marked supported or not by a source text.
  simgold/factcheck.py compares claims with market logs and needs a Ref of logs; it does not fit here (the source is a text),
  so only its number parser and symbol list are reused.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from simgold import memsafe
from simgold.factcheck import SYMS, num as parse_num      # noqa: F401  (parse_num kept for task modules)

# --------------------------------------------------------------------------- schema

_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}


def validate_schema(obj, schema: dict, path: str = "$") -> list[str]:
    """Problems found (empty list = valid). Subset of JSON schema, enough for the bake-off tasks."""
    out: list[str] = []
    t = schema.get("type")
    if t:
        ok = isinstance(obj, _TYPES[t]) and not (t in ("integer", "number") and isinstance(obj, bool))
        if not ok:
            return [f"{path}: expected {t}"]
    if "enum" in schema and obj not in schema["enum"]:
        out.append(f"{path}: not in enum")
    if isinstance(obj, str):
        if len(obj.strip()) < schema.get("minLength", 0):
            out.append(f"{path}: too short")
        if "maxLength" in schema and len(obj) > schema["maxLength"]:
            out.append(f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], obj):
            out.append(f"{path}: pattern")
    if isinstance(obj, list):
        if len(obj) < schema.get("minItems", 0):
            out.append(f"{path}: too few items")
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            out.append(f"{path}: too many items")
        if "items" in schema:
            for i, x in enumerate(obj):
                out += validate_schema(x, schema["items"], f"{path}[{i}]")
    if isinstance(obj, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in obj:
                out.append(f"{path}.{k}: missing")
        if schema.get("additionalProperties") is False:
            for k in obj:
                if k not in props:
                    out.append(f"{path}.{k}: unexpected key")
        for k, sub in props.items():
            if k in obj:
                out += validate_schema(obj[k], sub, f"{path}.{k}")
    return out


# --------------------------------------------------------------------------- hygiene

_MARKUP = re.compile(r"[<>`*#\[\]{}|\\~^]|\n|\r|^\s*(?:[-•]|\d+[.)])\s", re.M)
_ZERO_WIDTH = re.compile("[​-‏⁠﻿‪-‮]")


def markup_issues(text: str) -> list[str]:
    """No markdown, HTML, URLs, line breaks or invisible characters (same rule as fallback_report.check_paragraph, plus URLs)."""
    out = []
    if _MARKUP.search(text):
        out.append("markup")
    if memsafe.URL_RE.search(unicodedata.normalize("NFKC", text)) or re.search(r"\bwww\.", text, re.I):
        out.append("url")
    if _ZERO_WIDTH.search(text):
        out.append("invisible")
    return out


def memsafe_issues(text: str, kind: str) -> list[str]:
    try:
        memsafe.check_text(text, kind, one_line=False)
    except memsafe.Refused as e:
        return [f"memsafe: {str(e)[:80]}"]
    return []


# --------------------------------------------------------------------------- numbers, dates, words

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MON = "|".join(m.capitalize() for m in MONTHS)
_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})[a-z]*\.?(?:,?\s+(\d{{4}}))?\b", re.I)
_MDY = re.compile(rf"\b({_MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\d)(?:,?\s+(\d{{4}}))?", re.I)
_NUM = re.compile(r"(?<![\w.])(?P<cur>₹\s?|Rs\.?\s?|INR\s?)?(?P<raw>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<pct>\s?%)?")
_INDIAN = re.compile(r"^(?:\d{1,2}(?:,\d{2})*,\d{3}|\d{1,3})(?:\.\d+)?$")
_WEEKDAY = re.compile(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)\b")
_WD_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SYM = re.compile(r"\b(?i:goldbees|silverbees|hindcopper|liquidbees)\b|\b(?:GOLD|SILVER|COPPER)\b")
_DOWN = re.compile(r"\b(?:fell|fall|falls|falling|dropped|drops?|lost|loss|losses|declined|declines?|slid|slipped|eased|dipped|"
                   r"tumbled|sank|lower|red|negative|weaker)\b"
                   r"|\b(?:closed|ended|finished|was|were|is|moved|settled|traded|day|week)\s+down\b|\bdown\s+(?=[\d₹+−-])", re.I)
_UP = re.compile(r"\b(?:rose|rise|rises|rising|gained|gains?|climbed|jumped|rallied|higher|green|positive|stronger|firmed)\b"
                 r"|\b(?:closed|ended|finished|was|were|is|moved|settled|traded|day|week)\s+up\b|\bup\s+(?=[\d₹+−-])", re.I)
_YEARISH = re.compile(r"^(?:19|20|21)\d\d$")
_UNITS_AFTER = re.compile(r"^\s*(?:units?|shares?|qty|ticks?|sessions?|days?|lines?|files?|bytes?|KB|chars?)\b", re.I)


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _round(d: Decimal, places: int) -> Decimal:
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


@dataclass
class NumTok:
    raw: str
    value: Decimal
    places: int
    pct: bool
    cur: bool
    neg: bool
    pos: bool
    end: int


def _blank(text: str, spans) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


def date_tokens(text: str) -> tuple[set[tuple[int, int]], set[int], list[tuple[int, int]]]:
    """-> (set of (month, day), set of years, spans) for ISO dates, '25 Nov [2026]' and 'Nov 25 [2026]'."""
    mds: set[tuple[int, int]] = set()
    years: set[int] = set()
    spans: list[tuple[int, int]] = []
    for m in _ISO.finditer(text):
        mo, d = int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            mds.add((mo, d))
            years.add(int(m.group(1)))
            spans.append(m.span())
    for rx, di, mi in ((_DMY, 1, 2), (_MDY, 2, 1)):
        for m in rx.finditer(text):
            d, mo = int(m.group(di)), MONTHS.index(m.group(mi).lower()[:3]) + 1
            if 1 <= d <= 31:
                mds.add((mo, d))
                spans.append(m.span())
                if m.group(3):
                    years.add(int(m.group(3)))
    return mds, years, spans


def number_tokens(text: str) -> list[NumTok]:
    """Numbers outside dates and identifiers (SMA200, W47, v2 are skipped because a letter precedes the digits)."""
    _, _, spans = date_tokens(text)
    clean = _blank(text, spans)
    out = []
    for m in _NUM.finditer(clean):
        start = m.start()
        sign = ""
        j = start - 1
        if j >= 0 and clean[j] in "-−+" and (j == 0 or not (clean[j - 1].isalnum() or clean[j - 1] in ")%")):
            sign = clean[j]
        raw = m.group("raw")
        out.append(NumTok(raw, _dec(raw), len(raw.partition(".")[2]), bool(m.group("pct")), bool(m.group("cur")),
                          sign in "-−" and bool(sign), sign == "+", m.end()))
    return out


@dataclass
class Support:
    numbers: list[Decimal] = field(default_factory=list)
    pct_numbers: list[Decimal] = field(default_factory=list)
    years: set[int] = field(default_factory=set)
    mds: set[tuple[int, int]] = field(default_factory=set)
    weekdays: set[str] = field(default_factory=set)
    syms: set[str] = field(default_factory=set)
    down: bool = False
    up: bool = False


def build_support(source: str) -> Support:
    s = Support()
    s.mds, s.years, _ = date_tokens(source)
    for mo, d in s.mds:
        for y in (s.years or {2026}):
            try:
                s.weekdays.add(_WD_NAMES[date(y, mo, d).weekday()])
            except ValueError:
                pass
    for m in _WEEKDAY.finditer(source):
        s.weekdays.add(m.group(1).lower()[:3])
    for t in number_tokens(source):
        s.numbers.append(t.value)
        if t.value < 2:
            s.pct_numbers.append(t.value * 100)
        if t.neg:
            s.down = True
        if t.pos:
            s.up = True
    low = source.lower()
    s.syms = {w for w in ("goldbees", "silverbees", "hindcopper", "liquidbees", "gold", "silver", "copper") if w in low}
    s.down = s.down or bool(_DOWN.search(source))
    s.up = s.up or bool(_UP.search(source))
    return s


@dataclass
class Claim:
    kind: str          # num | date | year | weekday | sym | dir
    text: str
    ok: bool
    sig: str           # stable signature for re-run comparison


def _num_supported(t: NumTok, sup: Support) -> bool:
    pool = sup.numbers + (sup.pct_numbers if t.pct else [])
    for s in pool:
        try:
            if s == t.value or _round(s, t.places) == t.value:
                return True
        except InvalidOperation:
            continue
    return False


def fact_claims(text: str, source: str | Support) -> list[Claim]:
    """Every number, date, symbol and up/down word in `text`, each checked against `source` (text or build_support())."""
    sup = source if isinstance(source, Support) else build_support(source)
    out: list[Claim] = []
    mds, years, _ = date_tokens(text)
    for mo, d in sorted(mds):
        out.append(Claim("date", f"{d} {MONTHS[mo - 1].capitalize()}", (mo, d) in sup.mds, f"d:{mo:02d}-{d:02d}"))
    for y in sorted(years):
        out.append(Claim("year", str(y), y in sup.years or any(t.value == y for t in []), f"y:{y}"))
    for t in number_tokens(text):
        if _YEARISH.match(t.raw) and not t.cur and not t.pct:
            out.append(Claim("year", t.raw, int(t.raw) in sup.years or _num_supported(t, sup), f"y:{t.raw}"))
            continue
        out.append(Claim("num", t.raw + ("%" if t.pct else ""), _num_supported(t, sup), f"n:{t.value.normalize():f}"))
    for m in _WEEKDAY.finditer(text):
        w = m.group(1).lower()[:3]
        out.append(Claim("weekday", m.group(1), w in sup.weekdays, f"w:{w}"))
    for m in _SYM.finditer(text):
        w = m.group(0).lower()
        out.append(Claim("sym", m.group(0), w in sup.syms, f"s:{w}"))
    if _DOWN.search(text):
        out.append(Claim("dir", "down", sup.down, "dir:down"))
    if _UP.search(text):
        out.append(Claim("dir", "up", sup.up, "dir:up"))
    return out


def fact_tally(claims: list[Claim]) -> tuple[int, int, list[str]]:
    bad = [f"{c.kind}:{c.text}" for c in claims if not c.ok]
    return len(claims), len(claims) - len(bad), bad


def fact_signature(text: str, source: str | Support) -> set[str]:
    return {c.sig for c in fact_claims(text, source)}


# --------------------------------------------------------------------------- house number format

def house_issues(text: str) -> list[str]:
    """House style: ₹ (never Rs/INR/rupees), Indian grouping (1,00,000), money of 1000 or more grouped and with ₹."""
    out: list[str] = []
    if re.search(r"\b(?:Rs\.?|INR|rupees?)(?=\s|\d|$)", text, re.I):
        out.append("rs_word")
    for t in number_tokens(text):
        if t.pct or (_YEARISH.match(t.raw) and not t.cur):
            continue
        if "," in t.raw and not _INDIAN.match(t.raw):
            out.append(f"western_grouping:{t.raw}")
        elif t.value >= 1000:
            if "," not in t.raw:
                out.append(f"ungrouped:{t.raw}")
            elif not t.cur and not _UNITS_AFTER.match(text[t.end:t.end + 12]):
                out.append(f"no_rupee_sign:{t.raw}")
    return out


# --------------------------------------------------------------------------- text helpers

def words(text: str) -> int:
    return len(text.split())


def flatten_strings(obj) -> list[str]:
    """All string values (and dict keys) of a parsed output, depth first."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            out += flatten_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += flatten_strings(v)
    return out


def unique(seq):
    seen = set()
    return [x for x in seq if not (x in seen or seen.add(x))]
