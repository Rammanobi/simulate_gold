"""PreToolUse hook for every Gmail MCP tool. Exit 0 = allow, exit 2 = block (reason on stderr).

Policy (fail closed):
  * Read tools (get_*, list_*, search_*) are allowed.
  * send_message is allowed ONLY when
      - the REPORT_EMAIL environment variable is set and non-empty,
      - its keys are a subset of {to, subject, body, htmlBody},
      - `to` holds exactly one address, equal to REPORT_EMAIL (exact match, ASCII case-insensitive; non-ASCII
        look-alikes such as the Kelvin sign are refused, and REPORT_EMAIL itself must be ONE plain address),
      - the subject starts with "Metals Daybook" and has no control characters (no CR/LF header injection),
      - for the DAILY REPORT (subject "Metals Daybook · <📈|📉|📊|🏖️> Ddd D Mon · ..."): `htmlBody` is the generated
        reports/daily/<date>.email.html, verbatim (whitespace runs are ignored), and `body`, when given, is that
        day's reports/daily/<date>.txt. On 14 Oct 2026 the routine wrote its own short HTML instead of the approved
        design; this makes that impossible.
    No cc, bcc, attachments, draftId or reply ids (those keys are not in the allowed set).
  * Every other Gmail tool (drafts, forward, reply, trash, labels, spam, unknown ones) is blocked.
  * Any error while checking blocks the call.
The address itself is never stored in this repository; it comes from the environment.
"""
import json
import os
import re
import sys

ALLOWED_KEYS = {"to", "subject", "body", "htmlBody"}
SUBJECT_PREFIX = "Metals Daybook"
READ_PREFIXES = ("get_", "list_", "search_")
WRITE_WORDS = ("send", "forward", "reply", "trash", "delete", "spam", "modify", "update", "create", "import", "insert",
               "batch", "move")
PLAIN_ADDRESS = re.compile(r"^[A-Za-z0-9._%+\-]+\x40[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")   # \x40: no at-sign in this file
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
DAILY_SUBJECT = re.compile("^Metals Daybook \u00b7 (\U0001F4C8|\U0001F4C9|\U0001F4CA|\U0001F3D6\uFE0F?) [A-Z][a-z]{2} \\d{1,2} [A-Z][a-z]{2} \u00b7 ")
WS = re.compile(r"\s+")


def _root():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _norm(text):
    return WS.sub(" ", text).strip()


def check_daily_report(tool_input, root):
    """The daily report must be the generated files, not a rewrite. Returns None or a reason."""
    html = tool_input.get("htmlBody")
    if not isinstance(html, str) or not html.strip():
        return "the daily report needs htmlBody = the contents of html_path from tools/send_report.py"
    folder = os.path.join(root, "reports", "daily")
    want = _norm(html)
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith(".email.html"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            if _norm(fh.read()) != want:
                continue
        body = tool_input.get("body")
        if body is None:
            return None
        txt = os.path.join(folder, name[:-len(".email.html")] + ".txt")
        if os.path.exists(txt):
            with open(txt, encoding="utf-8") as fh:
                if _norm(fh.read()) == _norm(body):
                    return None
        return "body must be the contents of text_path from tools/send_report.py, unchanged"
    return ("htmlBody must be the contents of html_path from tools/send_report.py, unchanged "
            "(a rewritten or shortened report is not allowed)")


def _ascii_lower(s):
    return s.lower() if s.isascii() else None


def action_of(tool_name):
    """mcp__Gmail__send_message -> send_message (also tolerates a gmail_ prefix on the action)."""
    name = str(tool_name).split("__")[-1].strip().lower()
    return name[len("gmail_"):] if name.startswith("gmail_") else name


def check_send(tool_input, report_email, root=None):
    if not report_email or not report_email.strip():
        return "REPORT_EMAIL is not set, so sending is blocked"
    if not PLAIN_ADDRESS.match(report_email.strip()):
        return "REPORT_EMAIL must be one plain address (no lists, names or brackets), so sending is blocked"
    if not isinstance(tool_input, dict):
        return "tool_input is not an object"
    extra = sorted(set(tool_input) - ALLOWED_KEYS)
    if extra:
        return "not allowed in a report email: " + ", ".join(extra)
    to = tool_input.get("to")
    if isinstance(to, str):
        to = [to]
    if not isinstance(to, list) or len(to) != 1 or not isinstance(to[0], str):
        return "exactly one recipient in `to` is required"
    if CONTROL.search(to[0]) or _ascii_lower(to[0].strip()) != report_email.strip().lower():
        return "the recipient is not the configured report address"
    subject = tool_input.get("subject")
    if not isinstance(subject, str) or not subject.startswith(SUBJECT_PREFIX):
        return 'the subject must start with "%s"' % SUBJECT_PREFIX
    if CONTROL.search(subject):
        return "the subject must not contain line breaks or control characters"
    for k in ("body", "htmlBody"):
        if k in tool_input and not isinstance(tool_input[k], str):
            return "%s must be text" % k
    if DAILY_SUBJECT.match(subject):
        return check_daily_report(tool_input, root or _root())
    return None


def decide(event, report_email, root=None):
    """Return None to allow, or a reason string to block."""
    if not isinstance(event, dict):
        return "hook input is not an object"
    action = action_of(event.get("tool_name", ""))
    if action == "send_message":
        return check_send(event.get("tool_input"), report_email, root)
    if action.startswith(READ_PREFIXES) and not any(w in action for w in WRITE_WORDS):
        return None
    return "Gmail tool %r is not allowed (only send_message to the report address and read tools)" % action


def main():
    try:
        reason = decide(json.load(sys.stdin), os.environ.get("REPORT_EMAIL", ""))
    except Exception as e:      # anything unexpected blocks
        sys.stderr.write("gmail_guard: blocked, could not check the call (%s: %s)\n" % (type(e).__name__, e))
        return 2
    if reason:
        sys.stderr.write("gmail_guard: blocked: %s\n" % reason)
        return 2
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 2
    sys.exit(code)
