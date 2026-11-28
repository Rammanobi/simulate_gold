"""Privacy scanner for bake-off inputs and outgoing payloads. A hit fails the build of inputs and refuses an API call.

Finds: email addresses, phone numbers, token-like strings and secret names, private artifact links (also the exact links in
config/dashboard_url.txt and config/inbox_url.txt), Inbox rows, and raw headlines (titles from state/news.json). Inputs may
only come from allow-listed source folders; news.json and seen_headlines.json are never a source. Findings never echo a
secret: only the kind, the file and a masked 4-character prefix.

  python tools/bakeoff/privacy.py [--inputs reports/bakeoff/inputs] [--root .]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "tools", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

ALLOWED_SOURCE_PREFIXES = ("reports/daily/", "memory/daily/", "memory/weekly/", "memory/monthly/", "reports/weekly/", "evaluate:")
EXCLUDED_NAMES = ("news.json", "seen_headlines.json", "dashboard_url.txt", "inbox_url.txt", ".env")

PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+\s?@\s?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<![\w.,-])\+\d{1,3}[\s-]?\d[\d\s-]{7,13}\d(?![\w])|(?<![\w.,-])(?:\+?91[\s-]?)?[6-9]\d{9}(?![\w.])"
                        r"|(?<![\w.,-])\d{3}[\s-]\d{3}[\s-]\d{4}(?![\w])"),
    "token": re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{10,}|\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{16,}"
                        r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\b(?:AKIA|ASIA)[A-Z0-9]{12,}|\bAIza[A-Za-z0-9_-]{20,}"
                        r"|\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"
                        r"|(?<![\w/.-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}(?![\w/.-])|\b[0-9a-f]{32,}\b"),
    "secret_name": re.compile(r"\b(?:REPORT_EMAIL|SMTP_[A-Z_]+|ROUTINE_[A-Z_]+|DEEPSEEK_API_KEY|ANTHROPIC_API_KEY|GH_TOKEN|GITHUB_TOKEN)\b"
                              r"|(?i:\bpassword\s*[:=]|\bapi[_ -]?key\s*[:=])"),
    "artifact_link": re.compile(r"claude\.ai/(?:code/)?artifact|/artifact/[A-Za-z0-9_-]{10,}|dashboard_url|inbox_url", re.I),
    "inbox_row": re.compile(r"\bneeds_approval\b|\"revision\"\s*:\s*\d|\binbox\s+(?:item|row)s?\b|\bArtifactData\b", re.I),
}


class PrivacyError(Exception):
    pass


def _mask(s: str) -> str:
    s = s.strip()
    return (s[:4] + "***") if len(s) > 4 else "***"


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.casefold()).strip()


def load_headlines(root: Path) -> list[str]:
    """Normalised news titles (>= 20 chars), with and without the ' - Source' suffix."""
    out: list[str] = []
    try:
        d = json.loads((Path(root) / "state" / "news.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    items = d.values() if isinstance(d, dict) else d
    for it in items:
        t = it.get("title") if isinstance(it, dict) else None
        if isinstance(t, str):
            for variant in (t, re.sub(r"\s+[-–|]\s+[^-–|]+$", "", t)):
                n = _norm(variant)
                if len(n) >= 20:
                    out.append(n)
    return out


def private_links(root: Path) -> list[str]:
    out = []
    for name in ("dashboard_url.txt", "inbox_url.txt"):
        try:
            v = (Path(root) / "config" / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if v:
            out.append(v)
            out.append(v.rstrip("/").rsplit("/", 1)[-1])
    return [x for x in out if len(x) >= 8]


class Scanner:
    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root)
        self.headlines = load_headlines(self.root)
        self.links = private_links(self.root)

    def scan_text(self, text: str, where: str = "", exempt=()) -> list[str]:
        """`exempt`: our own planted injection lines (synthetic, may look like tokens); removed before scanning."""
        for e in exempt:
            text = text.replace(e, " ")
        found = []
        for kind, rx in PATTERNS.items():
            for m in rx.finditer(text):
                found.append(f"{where}: {kind} {'***' if kind in ('email', 'phone') else _mask(m.group(0))}")
        for link in self.links:
            if link in text:
                found.append(f"{where}: private link {_mask(link)}")
        n = _norm(text)
        for h in self.headlines:
            if h in n:
                found.append(f"{where}: raw headline {_mask(h)}")
        return found

    def scan_sources(self, paths, where: str = "") -> list[str]:
        found = []
        for p in paths:
            p = str(p).replace("\\", "/")
            if any(p.endswith(x) for x in EXCLUDED_NAMES) or not p.startswith(ALLOWED_SOURCE_PREFIXES):
                found.append(f"{where}: source not allowed {p}")
        return found

    def scan_messages(self, messages: list[dict], where: str = "payload", exempt=()) -> list[str]:
        return self.scan_text("\n".join(str(m.get("content", "")) for m in messages), where, exempt)


def scan_inputs(inputs_dir: Path, root: Path | str = ROOT) -> list[str]:
    sc = Scanner(root)
    found: list[str] = []
    files = sorted(Path(inputs_dir).rglob("*.json"))
    plants = []
    mf = Path(inputs_dir) / "manifest.json"
    if mf.exists():
        plants = [i["plant"]["text"] for t in json.loads(mf.read_text(encoding="utf-8")).get("tasks", {}).values()
                  for i in t.get("inputs", []) if i.get("plant")]
    for f in files:
        rel = f.relative_to(inputs_dir).as_posix()
        if f.name == "manifest.json":
            d = json.loads(f.read_text(encoding="utf-8"))
            srcs = [s["path"] for t in d.get("tasks", {}).values() for i in t.get("inputs", []) for s in i.get("sources", [])]
            found += sc.scan_sources(srcs, rel)
            continue
        found += sc.scan_text(f.read_text(encoding="utf-8"), rel, plants)
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--inputs", default=None)
    a = ap.parse_args(argv)
    root = Path(a.root)
    d = Path(a.inputs) if a.inputs else root / "reports" / "bakeoff" / "inputs"
    found = scan_inputs(d, root)
    if found:
        print("PRIVACY SCAN FAILED:\n" + "\n".join(found[:50]))
        return 1
    print(f"privacy scan clean: {sum(1 for _ in d.rglob('*.json'))} files in {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
