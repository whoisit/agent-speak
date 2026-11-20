"""Antigravity PreInvocation hook: runs /speak itself, so the command never reaches the model.

The turn is stopped before the model call and the speak_ctl output is shown to the user: no model
turn, no tokens. Any other prompt passes through untouched. If this hook is missing or fails, the
speak skill still works as a fallback.
"""
import os
import io
import re
import sys
import json
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SPEAK = re.compile(r"\s*/speak(?:\s+(.*))?\s*$", re.S | re.I)
COMMANDS = {"on", "off", "default", "toggle", "status", "voices", "voice", "engine", "speed", "glow", "highlight", "idle", "model", "test", "--session", "--project", "--agent", "--all"}


def is_fresh_user_input(path):
    """True if the transcript's latest step is the user prompt itself (not a tool loop or wakeup)."""
    if not path or not os.path.exists(path):
        return True
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            for chunk in (32768, size):  # the whole file only if the last line is longer than the tail
                f.seek(max(0, size - chunk))
                lines = f.read().splitlines()
                if len(lines) > 1 or chunk >= size:
                    break
        return bool(lines and json.loads(lines[-1]).get("type") == "USER_INPUT")
    except Exception:
        return False  # unreadable: not a fresh prompt (a subagent's step must not pass as one)


def main():
    try:
        data = json.load(sys.stdin.buffer)  # raw bytes: Windows would decode stdin as cp1252
    except Exception:
        print("{}")
        return
    user_input = data.get("lastUserInput") or ""
    if not is_fresh_user_input(data.get("transcriptPath")):
        print("{}")
        return
    # speak_ctl reads the session and project from its environment. agy started from a Claude Code
    # terminal inherits CLAUDECODE, which would make speak_ctl act for Claude instead.
    os.environ.pop("CLAUDECODE", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    from speak import antigravity_session, remember_antigravity_session, unattended
    sid = antigravity_session(data)
    if not unattended("antigravity"):  # headless runs write no state; speak_ctl refuses them itself
        remember_antigravity_session(sid)  # for speak_ctl runs by this conversation's model
    if sid:
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = sid
    workspaces = data.get("workspacePaths") or []
    if workspaces and os.path.isdir(workspaces[0]):
        os.chdir(workspaces[0])

    match = SPEAK.fullmatch(user_input)
    args = (match.group(1) or "").split() if match else None
    if args is None or (args and args[0].lower() not in COMMANDS):
        print("{}")
        return
    import speak_ctl
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            speak_ctl.main(args)
    except Exception as e:
        out.write(f"/speak failed: {e}")
    reply = out.getvalue().strip() or "Done."
    print(json.dumps({
        "injectSteps": [{"errorMessage": {"userMessage": reply}}],
        "terminationBehavior": "terminate",
    }))


if __name__ == "__main__":
    main()
