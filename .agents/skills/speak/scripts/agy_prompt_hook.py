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

SPEAK = re.compile(r"\s*/speak(?:\s+(.*))?\s*$", re.S)
COMMANDS = {"on", "off", "status", "voices", "voice", "engine", "speed", "glow", "highlight", "idle", "model", "test"}


def is_fresh_user_input(path):
    """True if the transcript's latest step is the user prompt itself (not a tool loop or wakeup)."""
    if not path or not os.path.exists(path):
        return True
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 32768))
            lines = f.read().splitlines()
        return bool(lines and json.loads(lines[-1]).get("type") == "USER_INPUT")
    except Exception:
        return True


def main():
    try:
        data = json.load(sys.stdin.buffer)  # raw bytes: Windows would decode stdin as cp1252
    except Exception:
        print("{}")
        return
    match = SPEAK.fullmatch(data.get("lastUserInput") or "")
    args = (match.group(1) or "").split() if match else None
    if args is None or (args and args[0].lower() not in COMMANDS) or not is_fresh_user_input(data.get("transcriptPath")):
        print("{}")
        return
    # speak_ctl reads the session and project from its environment
    if data.get("conversationId"):
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = data["conversationId"]
    workspaces = data.get("workspacePaths") or []
    if workspaces and os.path.isdir(workspaces[0]):
        os.chdir(workspaces[0])
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
