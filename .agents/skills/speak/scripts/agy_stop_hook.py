"""Antigravity Stop hook: speaks a cleaned-up version of the final reply when /speak is ON.

Shares state.json and the speech server with the Claude Code hook (installed by install_antigravity.py).
Always emits {"decision": "allow"} on stdout per Antigravity's Stop hook contract.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def truncate(text, max_chars):
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max((m.end() for m in re.finditer(r"[.!?](?=\s)", cut)), default=0)
    return cut[:end] if end > max_chars // 3 else cut.rsplit(" ", 1)[0] + "."


def extract_last_reply(transcript_path):
    """Return (last_reply_text, already_spoke_in_turn) since the last USER_INPUT step."""
    if not transcript_path or not os.path.exists(transcript_path):
        return "", False

    last_text = ""
    last_truncated_idx = None
    already_spoke = False

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                step_type = entry.get("type")
                if step_type == "USER_INPUT":
                    last_text = ""
                    last_truncated_idx = None
                    already_spoke = False
                elif step_type == "PLANNER_RESPONSE":
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
                        if "content" in (entry.get("truncated_fields") or []):
                            last_truncated_idx = line_no
                        else:
                            last_truncated_idx = None
    except Exception:
        return "", False

    if last_truncated_idx is not None:
        full_path = os.path.join(os.path.dirname(transcript_path), "transcript_full.jsonl")
        if os.path.exists(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    for line_no, line in enumerate(f):
                        if line_no == last_truncated_idx:
                            full_entry = json.loads(line)
                            full_content = full_entry.get("content")
                            if isinstance(full_content, str) and full_content.strip():
                                last_text = full_content
                            break
            except Exception:
                pass

    return last_text, already_spoke


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

    from speak import load_state
    state = load_state()
    if not state.get("enabled"):
        return

    reply, already_spoke = extract_last_reply(data.get("transcriptPath", ""))
    if already_spoke or not reply:
        return

    from textclean import clean
    import speak_server
    import windows

    text = clean(reply)
    if state.get("engine") == "google":
        text = truncate(text, state.get("max_chars", 1500))
    if not text:
        return

    # Tagged with the conversation so parallel sessions take turns instead of cutting each other off
    transcript = data.get("transcriptPath") or ""
    session = data.get("conversationId") or ("agy:" + os.path.dirname(transcript) if transcript else None)
    workspaces = data.get("workspacePaths") or []
    source = {"session": session, "project": workspaces[0] if workspaces else None, "agent": "antigravity",
              "label": os.path.basename(os.path.normpath(workspaces[0])) if workspaces else "Antigravity",
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
