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
argument-hint: [on|off|status|voices|voice <name|default> [--session|--project|--agent]|engine google|kokoro|edge|piper|speed 1.1|glow on|off|glow size <px>|glow corner <px|auto>|highlight on|off|idle <minutes>|model small|full|test]
allowed-tools: Bash({PY}:*)
model: haiku
---
!`{quoted(PY)} {quoted(SCRIPTS + '/speak_ctl.py')} $ARGUMENTS`

If the output above is a voice list, show it to the user as a short markdown list, keeping the * marks. Otherwise reply with one short plain sentence restating that status (e.g. "Speech is on, using the Charon voice."). Nothing else.
"""
HOOKS = {  # event -> script; the prompt hook answers /speak itself so it never costs a model turn
    "Stop": "claude_stop_hook.py",
    "UserPromptSubmit": "claude_prompt_hook.py",
}

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

changed = False
for event, script in HOOKS.items():
    cmd = f"{quoted(PY)} {quoted(SCRIPTS + '/' + script)}"
    groups = settings.setdefault("hooks", {}).setdefault(event, [])
    ours = [h for g in groups for h in g.get("hooks", []) if script in h.get("command", "")]
    if any(h["command"] == cmd for h in ours):
        print(f"{event} hook already installed")
        continue
    if ours:  # an older install, e.g. from before the repo moved: point it here
        ours[0]["command"] = cmd
    else:
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
    changed = True
    print(f"Installed the {event} hook")
if changed:
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    print("Saved ~/.claude/settings.json (backup: settings.json.bak-speak)")

print("Done. Restart Claude Code, then try /speak on and /speak test.")
