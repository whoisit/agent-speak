"""Install the /speak slash command and the auto-speak Stop hook into Claude Code (user scope).

Run once from the repo's venv:  venv\\Scripts\\python .agents\\skills\\speak\\scripts\\install_claude.py
Re-running is safe (also after moving the repo); settings.json is backed up to settings.json.bak-speak first.
"""
import os
import sys
import json
import shutil

SCRIPTS = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
REPO = SCRIPTS.rsplit("/", 4)[0]  # <repo>/.agents/skills/speak/scripts
VENV_PY = f"{REPO}/venv/Scripts/python.exe"
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable.replace("\\", "/")
CLAUDE_DIR = os.path.expanduser("~/.claude")


def quoted(path):
    return f'"{path}"' if " " in path else path

COMMAND = f"""---
description: Spoken replies on/off (Google Chirp 3 HD, Kokoro, Edge, Piper)
argument-hint: [on|off|status|voices|voice <name|default> [--session|--project|--agent]|engine google|kokoro|edge|piper|speed 1.1|glow on|off|idle <minutes>|model small|full|test]
allowed-tools: Bash({PY}:*)
---
!`{quoted(PY)} {quoted(SCRIPTS + '/speak_ctl.py')} $ARGUMENTS`

If the output above is a voice list, show it to the user as a short markdown list, keeping the * marks. Otherwise reply with one short plain sentence restating that status (e.g. "Speech is on, using the Charon voice."). Nothing else.
"""
HOOK_CMD = f"{quoted(PY)} {quoted(SCRIPTS + '/claude_stop_hook.py')}"

os.makedirs(os.path.join(CLAUDE_DIR, "commands"), exist_ok=True)
with open(os.path.join(CLAUDE_DIR, "commands", "speak.md"), "w", encoding="utf-8") as f:
    f.write(COMMAND)
print("Wrote ~/.claude/commands/speak.md")

settings_path = os.path.join(CLAUDE_DIR, "settings.json")
settings = {}
if os.path.exists(settings_path):
    shutil.copyfile(settings_path, settings_path + ".bak-speak")
    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)

stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
ours = [h for g in stop for h in g.get("hooks", []) if "claude_stop_hook.py" in h.get("command", "")]
if any(h["command"] == HOOK_CMD for h in ours):
    print("Stop hook already installed")
else:
    if ours:  # an older install, e.g. from before the repo moved: point it here
        ours[0]["command"] = HOOK_CMD
    else:
        stop.append({"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 10}]})
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    print("Installed the Stop hook in ~/.claude/settings.json (backup: settings.json.bak-speak)")

print("Done. Restart Claude Code, then try /speak on and /speak test.")
