"""Claude Code Stop hook: speaks a cleaned-up version of Claude's final reply.

Does nothing unless /speak is on. Synthesis and playback run in the background
speech server so the hook returns immediately.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from speak import load_state
from textclean import clean
import speak_server
import windows


def last_reply_from_transcript(path):
    """Return the text of the last assistant message that contains text."""
    last = ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, str):
                    text = content
                else:
                    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
                if text.strip():
                    last = text
    except Exception:
        pass
    return last


def main():
    state = load_state()
    if not state["enabled"]:
        return
    try:
        data = json.load(sys.stdin.buffer)  # raw bytes: Windows would decode stdin as cp1252, mangling – into â€“
    except Exception:
        data = {}
    reply = data.get("last_assistant_message") or last_reply_from_transcript(data.get("transcript_path", ""))
    text = clean(reply)
    if not text:
        return
    # Tagged with the session so parallel sessions take turns instead of cutting each other off,
    # and can each have their own voice and glow around their own window
    cwd = data.get("cwd") or ""
    source = {"session": data.get("session_id"), "project": cwd or None, "agent": "claude",
              "label": os.path.basename(os.path.normpath(cwd)) if cwd else "Claude",
              "window": windows.session_window()}
    # Server not running (after a reboot or the tray's Quit): start it with this reply
    if not speak_server.request({"cmd": "speak", "text": text, **source}):
        speak_server.start_detached(text, source)


if __name__ == "__main__":
    main()
