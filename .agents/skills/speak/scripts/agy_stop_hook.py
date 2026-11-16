"""Antigravity Stop hook: speaks a cleaned-up version of the final reply when /speak is ON.

Shares state.json and the speech server with the Claude Code hook (installed by install_antigravity.py).
Always emits {"decision": "allow"} on stdout per Antigravity's Stop hook contract.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def extract_last_reply(transcript_path):
    """Return (last_reply_text, already_spoke_in_turn) since the last USER_INPUT step."""
    if not transcript_path or not os.path.exists(transcript_path):
        return "", False
    # Prefer transcript_full.jsonl so long replies aren't truncated and only one file is read
    full_path = os.path.join(os.path.dirname(transcript_path), "transcript_full.jsonl")
    path = full_path if os.path.exists(full_path) else transcript_path

    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            for chunk_size in (524288, size):
                f.seek(max(0, size - chunk_size))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
                last_text = ""
                already_spoke = False
                in_user_turn = False
                for line in lines:
                    if '"USER_INPUT"' not in line and '"PLANNER_RESPONSE"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    step_type = entry.get("type")
                    if step_type == "USER_INPUT":
                        in_user_turn = True
                        last_text = ""
                        already_spoke = False
                    elif in_user_turn and step_type == "PLANNER_RESPONSE":
                        for tc in entry.get("tool_calls") or []:
                            if tc.get("name") == "run_command":
                                cmd_str = str((tc.get("args") or {}).get("CommandLine", ""))
                                if re.search(r'python(?:\.exe)?["\\\s]+[^"\n]*speak\.py\b', cmd_str, re.I) or re.search(
                                    r'speak_ctl\.py["\\\s]+test\b', cmd_str, re.I
                                ):
                                    already_spoke = True
                        content = entry.get("content")
                        if isinstance(content, str) and content.strip():
                            last_text = content
                if in_user_turn or chunk_size >= size:
                    return last_text, already_spoke
    except Exception:
        pass
    return "", False


def run():
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    if data.get("terminationReason") not in (None, "", "NO_TOOL_CALL"):
        return
    if not data.get("fullyIdle", True):
        return

    from speak import is_enabled, antigravity_session
    session = antigravity_session(data)
    workspaces = data.get("workspacePaths") or []
    project = workspaces[0] if workspaces else None
    if not is_enabled(session=session, project=project, agent="antigravity"):
        return

    reply, already_spoke = extract_last_reply(data.get("transcriptPath", ""))
    if already_spoke or not reply:
        return

    from textclean import clean
    import speak_server
    import windows

    text = clean(reply)
    if not text:
        return

    # Tagged with the conversation so parallel sessions take turns instead of cutting each other off
    source = {"session": session, "project": project, "agent": "antigravity",
              "label": os.path.basename(os.path.normpath(project)) if project else "Antigravity",
              "window": windows.session_window()}
    if not speak_server.request({"cmd": "speak", "text": text, **source}):
        speak_server.start_detached(text, source)


def main():
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        run()
    except Exception as exc:
        print(f"agy_stop_hook error: {exc}", file=sys.stderr)
    finally:
        real_stdout.write('{"decision": "allow"}\n')
        real_stdout.flush()


if __name__ == "__main__":
    main()
