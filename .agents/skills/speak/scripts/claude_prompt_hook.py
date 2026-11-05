"""Claude Code UserPromptSubmit hook: runs /speak itself, so the command never reaches the model.

The prompt is blocked and the speak_ctl output is shown to the user as the block reason: no model
turn, no tokens. Any other prompt passes through untouched. If this hook is missing or fails, the
/speak command file still works as a fallback (one cheap Haiku turn).
"""
import os
import io
import re
import sys
import json
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SPEAK = re.compile(r"\s*/speak(?:\s+(.*))?\s*$", re.S)


def main():
    try:
        data = json.load(sys.stdin.buffer)  # raw bytes: Windows would decode stdin as cp1252
    except Exception:
        return
    match = SPEAK.fullmatch(data.get("prompt") or "")
    if not match:
        return
    # speak_ctl reads the session, project and agent from its environment, as under the command file
    os.environ["CLAUDECODE"] = "1"
    if data.get("session_id"):
        os.environ["CLAUDE_CODE_SESSION_ID"] = data["session_id"]
    if data.get("cwd") and os.path.isdir(data["cwd"]):
        os.chdir(data["cwd"])
    import speak_ctl
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            speak_ctl.main((match.group(1) or "").split())
    except Exception as e:
        out.write(f"/speak failed: {e}")
    print(json.dumps({"decision": "block", "reason": out.getvalue().strip() or "Done."}))


if __name__ == "__main__":
    main()
