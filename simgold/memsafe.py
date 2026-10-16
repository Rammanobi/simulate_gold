"""Validator for every memory write: facts and sources only, never instructions (paper trading).

check_text(s, kind) returns the cleaned one-line text or raises Refused with the reason. Nothing is written by
this module. Web text, headlines and notes are untrusted data; this filter keeps instruction-like text out of
memory, so a later routine cannot be steered by something an earlier one copied in.
"""
from __future__ import annotations

import re

MAX_LEN = {
    "note": 160,          # stm note
    "evidence": 200,      # thesis evidence / lesson support line
    "expect": 100,        # calendar expectation
    "thesis_line": 200,
    "lesson": 240,
    "candidate": 240,
    "bias_note": 300,     # guidance.py --note
    "source": 60,         # bare domain or short name
    "why": 200,
    "bullet": 340,        # a lessons.md bullet incl. its evidence suffix
}
DATED_KINDS = {"evidence"}

_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s+" + _MON + r"\b|\b" + _MON + r"\s+\d{1,2}\b", re.I)
SOURCE_RE = re.compile(r"\([^()]*[A-Za-z][^()]*\)|\bsource:\s*\S+|\bsrc:\s*\S+", re.I)
DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$|^[A-Za-z0-9 ._&-]{2,60}$")
URL_RE = re.compile(r"https?://|ftp://|www\.|\bdata:|\bjavascript:|[a-z0-9-]+\.(?:com|org|net|io|ai|in)/\S+", re.I)

INSTRUCTION_PATTERNS = [
    r"\bignore\s+(?:all|any|the|these|those|previous|prior|above|earlier|your|my|every)\b",
    r"\bdisregard\b", r"\bforget\s+(?:all|everything|your|previous|the)\b", r"\boverride\b",
    r"\bnew\s+instructions?\b", r"\binstead\s*,?\s+(?:you\s+)?(?:must|should|will)\b",
    r"\byou\s+(?:must|should|shall|will|need\s+to|have\s+to)\b", r"\b(?:please|kindly)\s+(?:buy|sell|set|write|email|send|run|ignore)\b",
    r"\bbuy\s+(?:all|now|everything|max|maximum)\b",
    r"\bsell\s+(?:all|everything|now)\b", r"\b(?:go|going)\s+all[\s-]in\b",
    r"\bset\s+(?:the\s+)?(?:floor|cap|stop|limit|size|bias)\b", r"\b(?:raise|lift|remove|disable|loosen|widen)\s+(?:the\s+)?(?:floor|cap|caps|stop|stops|limit|limits|guard)\b",
    r"\b(?:system|developer|assistant)\s+(?:prompt|message|instructions?)\b", r"\bjailbreak\b",
    r"\bpretend\b", r"\byou\s+are\s+now\b", r"\bfrom\s+now\s+on\b",
    r"\bbypass\b", r"\bdo\s+not\s+(?:tell|report|log|follow)\b", r"\bdon'?t\s+(?:tell|report|log|follow)\b",
    r"\bfavou?r\s+(?:silver|gold|copper|all)\s+now\b",
    r"</?\s*(?:system|assistant|user|human|instructions?|tool|script)\b", r"\[/?\s*(?:INST|SYS|system)\b", r"<\|",
    r"```", r"`[^`]*\b(?:rm|curl|wget|git|python|bash|sh|sudo|gh|cat|echo)\b[^`]*`",
    r"\$\(", r"\b(?:rm\s+-rf|curl\s|wget\s|sudo\s|chmod\s|git\s+push|git\s+commit)\b",
    r"\bpassword\b", r"\bapi[_\s-]?key\b", r"\btoken\b\s*[:=]",
    r"\bsend\s+(?:an?\s+)?(?:email|mail)\b", r"\bexfiltrat", r"\bpromote\s+(?:this|it)\s+(?:to|as)\s+lesson\b",
]
_INSTR = [re.compile(p, re.I) for p in INSTRUCTION_PATTERNS]


class Refused(Exception):
    """A memory write was rejected; the message says why. Nothing was written."""


def _printable(s: str) -> bool:
    return all(ch.isprintable() for ch in s)


def check_text(s, kind: str = "note", *, one_line: bool = True) -> str:
    """Return the cleaned text or raise Refused. `kind` picks the length cap (see MAX_LEN)."""
    if kind not in MAX_LEN:
        raise Refused(f"unknown text kind {kind!r}")
    if s is None or not isinstance(s, str):
        raise Refused(f"{kind} must be text")
    if one_line and ("\n" in s.strip() or "\r" in s.strip()):
        raise Refused(f"{kind} must be one line")
    t = " ".join(s.split())
    if not t:
        raise Refused(f"{kind} is empty")
    if not _printable(t):
        raise Refused(f"{kind} has non-printable characters")
    cap = MAX_LEN[kind]
    if len(t) > cap:
        raise Refused(f"{kind} is {len(t)} characters; keep it under {cap}")
    if kind == "source":
        if URL_RE.search(t) or not DOMAIN_RE.match(t):
            raise Refused("source must be a bare domain or short name (no URL, no path)")
    elif URL_RE.search(t):
        raise Refused(f"{kind} must not contain URLs (put a bare domain in --source)")
    for rx in _INSTR:
        m = rx.search(t)
        if m:
            raise Refused(f"{kind} looks like an instruction ({m.group(0).strip()!r}); memory holds facts and sources only")
    if kind in DATED_KINDS:
        if not DATE_RE.search(t):
            raise Refused(f"{kind} needs a date (YYYY-MM-DD or '17 Oct')")
        if not SOURCE_RE.search(t):
            raise Refused(f"{kind} needs a source, for example '(reuters, 17 Oct)'")
    return t


def check_multiline(s: str, kind: str = "note", max_lines: int = 15, max_bytes: int = 4000) -> list[str]:
    """For prose files (daily notes, lessons): every line must pass; returns the lines."""
    lines = [ln for ln in (s or "").splitlines() if ln.strip()]
    if len(lines) > max_lines:
        raise Refused(f"{len(lines)} lines; keep it to {max_lines}")
    if len((s or "").encode("utf-8")) > max_bytes:
        raise Refused("text is too long")
    for ln in lines:
        body = ln.lstrip("#-*0123456789. ").strip()
        if not body:
            continue
        if not _printable(body):
            raise Refused("non-printable characters")
        for rx in _INSTR:
            m = rx.search(body)
            if m:
                raise Refused(f"line looks like an instruction ({m.group(0).strip()!r}): {body[:60]}")
        if URL_RE.search(body):
            raise Refused(f"line contains a URL: {body[:60]}")
    return lines


def looks_like_instruction(s: str) -> bool:
    try:
        check_text(s, "bias_note" if len(s or "") > 160 else "note", one_line=False)
        return False
    except Refused:
        return True
