"""Install the speak skill and the auto-speak Stop hook into Google Antigravity (user scope).

Run once from the repo's venv:  venv\\Scripts\\python .agents\\skills\\speak\\scripts\\install_antigravity.py
Re-running is safe (also after moving the repo); hooks.json is backed up to hooks.json.bak-speak first.
"""
import os
import sys
import json
import shutil

SCRIPTS = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
SKILL_SRC = os.path.join(os.path.dirname(SCRIPTS), "SKILL.md")
REPO = SCRIPTS.rsplit("/", 4)[0]  # <repo>/.agents/skills/speak/scripts
VENV_PY = f"{REPO}/venv/Scripts/python.exe"
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable.replace("\\", "/")
CONFIG_DIR = os.path.expanduser("~/.gemini/config")
SKILL_DIR = os.path.join(CONFIG_DIR, "skills", "speak")
HOOKS_PATH = os.path.join(CONFIG_DIR, "hooks.json")
HOOK_CMD = f'"{PY}" "{SCRIPTS}/agy_stop_hook.py"'

# Global skill: the repo's SKILL.md with its repo-relative paths made absolute
with open(SKILL_SRC, "r", encoding="utf-8") as f:
    skill = f.read()
win = lambda path: path.replace("/", "\\")
skill = (skill.replace('"venv\\Scripts\\python.exe"', f'"{win(PY)}"')
              .replace('".agents\\skills\\speak\\scripts\\', f'"{win(SCRIPTS)}\\')
              .replace("Paths below are relative to\nthe Speak repository root.", f"Installed from `{win(REPO)}`."))
os.makedirs(SKILL_DIR, exist_ok=True)
with open(os.path.join(SKILL_DIR, "SKILL.md"), "w", encoding="utf-8") as f:
    f.write(skill)
print("Wrote ~/.gemini/config/skills/speak/SKILL.md")

# Earlier installs copied the hook and a wrapper next to the skill; they now run from the repo
old_scripts = os.path.join(SKILL_DIR, "scripts")
if os.path.isdir(old_scripts):
    shutil.rmtree(old_scripts)
    print("Removed the old copied scripts from ~/.gemini/config/skills/speak/scripts")

hooks = {}
if os.path.exists(HOOKS_PATH):
    shutil.copyfile(HOOKS_PATH, HOOKS_PATH + ".bak-speak")
    with open(HOOKS_PATH, "r", encoding="utf-8") as f:
        hooks = json.load(f)
hooks["speak-stop"] = {"Stop": [{"type": "command", "command": HOOK_CMD, "timeout": 10}]}
with open(HOOKS_PATH, "w", encoding="utf-8") as f:
    json.dump(hooks, f, indent=2)
print("Installed the Stop hook in ~/.gemini/config/hooks.json (backup: hooks.json.bak-speak)")

print("Done. Restart Antigravity, then try /speak on and /speak test.")
