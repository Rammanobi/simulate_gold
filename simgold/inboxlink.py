"""Links into the owner's Daybook Inbox (a claude.ai Artifact). Pure helpers, no I/O beyond one small file read."""
from __future__ import annotations

import re
from pathlib import Path

INBOX_LABEL = "Open in Daybook Inbox: accept or reject"
_BAD = re.compile(r"[^A-Za-z0-9._~-]+")


def safe_id(item_id: str) -> str:
    """Inbox item ids may only contain letters, digits, '.', '_', '~' and '-'. Anything else becomes '-'."""
    s = _BAD.sub("-", str(item_id or "")).strip("-")
    return s or "item"


def daily_item_id(date: str) -> str:
    return safe_id(f"daily-{date}")


def load_inbox_url(root: Path | str) -> str | None:
    """The URL stored in <root>/config/inbox_url.txt, or None when the file is missing, empty or not https."""
    try:
        txt = (Path(root) / "config" / "inbox_url.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return txt if txt.startswith("https://") else None


def inbox_link(inbox_url: str | None, item_id: str) -> str | None:
    """`<inbox_url>#<item id>` (any existing fragment on the URL is dropped), or None without a URL."""
    if not inbox_url:
        return None
    return f"{inbox_url.split('#', 1)[0]}#{safe_id(item_id)}"
