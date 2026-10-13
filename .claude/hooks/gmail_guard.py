"""PreToolUse hook for every Gmail MCP tool. Exit 0 = allow, exit 2 = block (reason on stderr).

Policy (fail closed):
  * Read tools (get_*, list_*, search_*) are allowed.
  * send_message is allowed ONLY when
      - the REPORT_EMAIL environment variable is set and non-empty,
      - its keys are a subset of {to, subject, body, htmlBody},
      - `to` holds exactly one address, equal to REPORT_EMAIL (exact match, case-insensitive),
      - the subject starts with "Metals Daybook".
    No cc, bcc, attachments, draftId or reply ids (those keys are not in the allowed set).
  * Every other Gmail tool (drafts, forward, reply, trash, labels, spam, unknown ones) is blocked.
  * Any error while checking blocks the call.
The address itself is never stored in this repository; it comes from the environment.
"""
import json
import os
import sys

ALLOWED_KEYS = {"to", "subject", "body", "htmlBody"}
SUBJECT_PREFIX = "Metals Daybook"
READ_PREFIXES = ("get_", "list_", "search_")


def action_of(tool_name):
    """mcp__Gmail__send_message -> send_message (also tolerates a gmail_ prefix on the action)."""
    name = str(tool_name).split("__")[-1].strip().lower()
    return name[len("gmail_"):] if name.startswith("gmail_") else name


def check_send(tool_input, report_email):
    if not report_email or not report_email.strip():
        return "REPORT_EMAIL is not set, so sending is blocked"
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
    if to[0].strip().lower() != report_email.strip().lower():
        return "the recipient is not the configured report address"
    subject = tool_input.get("subject")
    if not isinstance(subject, str) or not subject.startswith(SUBJECT_PREFIX):
        return 'the subject must start with "%s"' % SUBJECT_PREFIX
    for k in ("body", "htmlBody"):
        if k in tool_input and not isinstance(tool_input[k], str):
            return "%s must be text" % k
    return None


def decide(event, report_email):
    """Return None to allow, or a reason string to block."""
    if not isinstance(event, dict):
        return "hook input is not an object"
    action = action_of(event.get("tool_name", ""))
    if action == "send_message":
        return check_send(event.get("tool_input"), report_email)
    if action.startswith(READ_PREFIXES):
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
