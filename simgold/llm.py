"""Optional Claude layer. Every function returns None when no API key is configured or a
call fails, and the rule engine then runs on its own. Claude can only *shrink* a trade
the rules propose, never enlarge it or invent one (see brain.apply_review)."""
from __future__ import annotations

import json
import logging
import os

from . import config

log = logging.getLogger(__name__)
_client = None


def _get_client():
    global _client
    if _client is None and os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        _client = anthropic.Anthropic(max_retries=3, timeout=120)
    return _client


def _call(model: str, system: str, prompt: str, schema: dict | None, effort: str, max_tokens: int = 16000):
    client = _get_client()
    if client is None:
        return None
    import anthropic
    kw = dict(model=model, max_tokens=max_tokens, system=system,
              messages=[{"role": "user", "content": prompt}],
              output_config={"effort": effort, **({"format": {"type": "json_schema", "schema": schema}} if schema else {})})
    try:
        if model != config.MODELS["news"]:
            # Server-side refusal fallback for Opus/Sonnet; plain call if the beta is rejected.
            try:
                resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
            except anthropic.BadRequestError:
                resp = client.messages.create(**kw)
        else:
            resp = client.messages.create(**kw)
    except anthropic.APIStatusError as e:
        log.warning("%s call failed (%s): %s", model, e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("%s connection error: %s", model, e)
        return None
    if resp.stop_reason == "refusal":
        log.warning("%s refused the request", model)
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text) if schema else text


_SCORE_SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "GOLD": {"type": "number"}, "SILVER": {"type": "number"}, "COPPER": {"type": "number"},
            "impact": {"type": "number"}, "why": {"type": "string"},
        },
        "required": ["id", "GOLD", "SILVER", "COPPER", "impact", "why"],
        "additionalProperties": False,
    }}},
    "required": ["scores"], "additionalProperties": False,
}


def score_headlines(headlines: list[dict]) -> dict[str, dict] | None:
    """Haiku: per-headline expected price impact on Indian gold/silver ETFs and Hindustan Copper."""
    out: dict[str, dict] = {}
    for i in range(0, len(headlines), 40):
        batch = headlines[i:i + 40]
        lines = "\n".join(f"{h['id']}: {h['title']}" for h in batch)
        res = _call(
            config.MODELS["news"],
            "You score financial headlines for an Indian metals paper-trading simulator. For each "
            "headline give the likely short-term (1-3 day) price direction for GOLD (GOLDBEES, INR gold), "
            "SILVER (SILVERBEES) and COPPER (Hindustan Copper / LME copper) from -1 (strongly bearish) to "
            "+1 (strongly bullish); 0 if irrelevant. Remember INR weakness lifts INR metal prices. "
            "impact is 0-1: how market-moving the news is (routine price recaps ≈0.2, central-bank "
            "decisions or wars ≈0.9). why: ≤12 words.",
            f"Headlines:\n{lines}", _SCORE_SCHEMA, effort="low", max_tokens=8000)
        if res is None:
            return out or None
        for s in res["scores"]:
            s = {k: (max(-1.0, min(1.0, v)) if isinstance(v, (int, float)) else v) for k, v in s.items()}
            s["impact"] = max(0.0, s["impact"])
            out[s.pop("id")] = s
    return out


_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "asset": {"type": "string", "enum": list(config.ASSETS)},
                "scale": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["asset", "scale", "reason"], "additionalProperties": False,
        }},
        "market_view": {"type": "string"},
    },
    "required": ["decisions", "market_view"], "additionalProperties": False,
}


def review_orders(context: dict) -> dict | None:
    """Opus: risk-committee review of the rule engine's proposed orders."""
    return _call(
        config.MODELS["decide"],
        "You are the risk committee of a PAPER-TRADING simulator (no real money) that trades Indian "
        "gold/silver ETFs and Hindustan Copper. A rule engine has proposed orders. For each proposed "
        "asset return scale in [0,1]: 1 = execute as proposed, 0.5 = half size, 0 = skip. You cannot "
        "increase size or add assets. Sells that reduce risk (stops, drawdown cuts) should normally be "
        "scale 1. Look for: news that contradicts the technicals, event risk in the next 24h (FOMC, CPI, "
        "RBI), chasing an overstretched move, or costs eating the edge on small trades. Keep each "
        "reason to one sentence. market_view: 2-3 sentences.",
        json.dumps(context, default=str), _REVIEW_SCHEMA, effort="medium")


def write_journal(day_summary: dict) -> str | None:
    """Sonnet: plain-English end-of-day journal entry."""
    return _call(
        config.MODELS["journal"],
        "Write a concise markdown trading-journal entry for a paper-trading simulator: what the "
        "portfolio did today, why each trade happened (cite the signal/news drivers given), what "
        "risk controls fired, and what to watch tomorrow. Under 250 words. No investment advice.",
        json.dumps(day_summary, default=str), None, effort="low", max_tokens=4000)
