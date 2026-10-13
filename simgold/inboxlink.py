"""Links into the owner's Daybook Inbox (a claude.ai Artifact). Pure helpers, no I/O beyond one small file read."""
from __future__ import annotations

import re
from pathlib import Path

INBOX_LABEL = "Open in Daybook Inbox: accept or reject"
_BAD = re.compile(r"[^A-Za-z0-9._~-]+")
MAX_ID = 80
ID_RE = re.compile(r"^(?![.])[A-Za-z0-9._~-]{1,%d}\Z" % MAX_ID)       # what a valid item id looks like (no leading dot)
INBOX_URL_RE = re.compile(r"^https://claude\.ai/[A-Za-z0-9._~/-]+\Z")
OWN_REPO = "Rammanobi/simulate_gold"
OWN_PR_RE = re.compile(r"^https://github\.com/Rammanobi/simulate_gold/pull/([1-9][0-9]{0,6})\Z")


def safe_id(item_id: str) -> str:
    """Inbox item ids may only contain letters, digits, '.', '_', '~' and '-'. Anything else becomes '-'."""
    s = _BAD.sub("-", str(item_id or ""))[:MAX_ID * 2].strip("-.")[:MAX_ID].strip("-.")   # never "." / ".." / a leading dot
    return s or "item"


def is_valid_id(item_id) -> bool:
    return isinstance(item_id, str) and bool(ID_RE.match(item_id))


def own_pr_url(url) -> str:
    """The URL if it is exactly a pull request of THIS repository (https, no query/fragment/suffix), else ''."""
    return url if isinstance(url, str) and OWN_PR_RE.match(url) else ""


def pr_number(url) -> int | None:
    m = OWN_PR_RE.match(url) if isinstance(url, str) else None
    return int(m.group(1)) if m else None


def daily_item_id(date: str) -> str:
    return safe_id(f"daily-{date}")


def load_inbox_url(root: Path | str) -> str | None:
    """The URL stored in <root>/config/inbox_url.txt, or None when the file is missing, empty or not a plain https://claude.ai/ URL."""
    try:
        txt = (Path(root) / "config" / "inbox_url.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return txt if INBOX_URL_RE.match(txt) else None     # one claude.ai URL, nothing that can break out of an href or a line


def inbox_link(inbox_url: str | None, item_id: str) -> str | None:
    """`<inbox_url>#<item id>` (any existing fragment on the URL is dropped), or None without a URL."""
    if not inbox_url or not INBOX_URL_RE.match(inbox_url.split("#", 1)[0]):
        return None
    return f"{inbox_url.split('#', 1)[0]}#{safe_id(item_id)}"
