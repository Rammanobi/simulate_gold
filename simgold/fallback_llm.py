"""DeepSeek client for the fallback used when the Claude routines cannot run (OpenAI-compatible API, stdlib only).

Two tiers: "thinking" (the bias decision) and "fast" (report text). Model IDs are looked up at runtime: GET /models once,
then the first ID of the tier's preference list (params.yaml `fallback.models`) that the account has. If /models fails,
the list is tried in order on the real call, moving on after a 400/404 "model not found". Only `content` of a reply is
used: `reasoning_content` is never read, logged or stored. The API key is never printed or put in an error message.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULTS = {
    "base_url": "https://api.deepseek.com",
    "models": {"thinking": ["deepseek-v4-pro", "deepseek-reasoner"], "fast": ["deepseek-flash", "deepseek-v4-flash", "deepseek-chat"]},
    "temperature": 0.2, "timeout_s": 60, "retries": 2, "max_tokens": {"thinking": 2000, "fast": 600},
}


class FallbackError(Exception):
    pass


def load_cfg(p: dict | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    f = (p or {}).get("fallback")
    if isinstance(f, dict):
        for k, v in f.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def http(method: str, url: str, headers: dict, body: dict | None, timeout: float) -> tuple[int, dict]:
    """-> (status, parsed JSON or {}). HTTP error statuses are returned, not raised; network errors/timeouts raise OSError."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 - fixed https base_url from params.yaml
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read() or b""
    try:
        parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _err_text(d: dict) -> str:
    e = d.get("error")
    return str(e.get("message", "") if isinstance(e, dict) else e or "").lower()


class Client:
    def __init__(self, api_key: str, cfg: dict | None = None, request=http, sleep=None):
        self._key = api_key
        self.cfg = cfg or load_cfg(None)
        self._request = request
        self._sleep = sleep or (lambda s: None)
        self.available: set[str] | None = None
        self._listed = False
        self.chosen: dict[str, str] = {}

    # never leak the key: every message that leaves this class goes through here
    def _clean(self, msg: str) -> str:
        return msg.replace(self._key, "***") if self._key else msg

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def list_models(self) -> set[str] | None:
        if self._listed:
            return self.available
        self._listed = True
        try:
            status, d = self._request("GET", self.cfg["base_url"].rstrip("/") + "/models", self._headers(), None, self.cfg["timeout_s"])
            if status == 200 and isinstance(d.get("data"), list):
                self.available = {str(m.get("id")) for m in d["data"] if isinstance(m, dict) and m.get("id")}
        except OSError as e:
            log.warning("fallback: /models failed: %s", self._clean(type(e).__name__))
        return self.available

    def candidates(self, tier: str) -> list[str]:
        prefs = [str(x) for x in self.cfg["models"].get(tier, [])]
        have = self.list_models()
        if have is None:
            return prefs
        picked = [m for m in prefs if m in have]
        if picked:
            return picked
        # DeepSeek renames models; when none of the configured ids exist, pick by family name
        hints = ("pro", "reasoner", "think") if tier == "thinking" else ("flash", "chat", "lite")
        return sorted(m for m in have if any(h in m.lower() for h in hints))

    def chat(self, tier: str, messages: list[dict], json_mode: bool = False) -> str:
        """The reply's `content`. Raises FallbackError when no model works."""
        cands = self.candidates(tier)
        if not cands:
            raise FallbackError(f"no {tier} model available on this account")
        last = "no attempt"
        for model in cands:
            body = {"model": model, "messages": messages, "temperature": self.cfg["temperature"],
                    "max_tokens": int(self.cfg["max_tokens"][tier])}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            for attempt in range(int(self.cfg["retries"]) + 1):
                try:
                    status, d = self._request("POST", self.cfg["base_url"].rstrip("/") + "/chat/completions",
                                              self._headers(), body, self.cfg["timeout_s"])
                except OSError as e:               # timeout / connection problem
                    last = f"{model}: {type(e).__name__}"
                    self._sleep(2 * (attempt + 1))
                    continue
                if status == 200:
                    try:
                        content = d["choices"][0]["message"]["content"]       # never reasoning_content
                    except (KeyError, IndexError, TypeError):
                        content = None
                    if isinstance(content, str) and content.strip():
                        self.chosen[tier] = model
                        return content
                    last = f"{model}: empty reply"
                    break
                text = _err_text(d)
                if status in (400, 404) and "temperature" in text and "temperature" in body:
                    body.pop("temperature")                                  # this model rejects it: ask again without
                    continue
                if status in (400, 404) and "model" in text:
                    last = f"{model}: not available (HTTP {status})"
                    break                                                    # next model in the list
                if status >= 500 or status == 429:
                    last = f"{model}: HTTP {status}"
                    self._sleep(2 * (attempt + 1))
                    continue
                raise FallbackError(self._clean(f"{model}: HTTP {status} {text[:120]}"))
        raise FallbackError(self._clean(f"all {tier} models failed (last: {last})"))


def write_state(root: Path, rec: dict) -> None:
    p = root / "state" / "fallback.json"
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
        dates = [str(x) for x in old.get("dates", [])] if isinstance(old, dict) else []
    except (OSError, ValueError):
        dates = []
    if rec.get("ok") and rec["used_at"][:10] not in dates:
        dates.append(rec["used_at"][:10])
    rec = {**rec, "dates": dates[-10:]}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)
