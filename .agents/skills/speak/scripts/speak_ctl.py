"""Backend for the Claude Code and Antigravity /speak slash command.

Usage: speak_ctl.py [on|off|default [--session|--project|--agent|--all]|status|voices|voice <name|default> [--session|--project|--agent]|
       engine <google|kokoro|edge|piper>|speed <1.1>|glow <on|off|size <px>|corner <px|auto>>|highlight <on|off>|idle <minutes>|model <small|full>|test]
Plain on/off/default and no arguments (toggle) act on this session only; --all sets what every session inherits.
"""
import os
import re
import difflib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from speak import load_state, save_state, stop_standalone, override_key, voices_for, PIPER_MODELS_DIR
import speak_server

SCOPES = ("--session", "--project", "--agent", "--all")
AGENTS = {"antigravity": "antigravity", "agy": "antigravity", "claude": "claude"}
MAX_SESSION_OVERRIDES = 50  # session overrides outlive their sessions; keep only the newest
# {speak off} / {speak on --project} tag inside a task prompt: braces only, so prose about /speak never matches
INLINE = re.compile(r"\{\s*/?speak\s+(on|off)(?:\s+(--(?:session|project|agent|all)))?\s*\}", re.I)


def inline_directive(prompt):
    """speak_ctl args for a {speak on|off [--scope]} tag in a task prompt (default: this session), else None."""
    match = INLINE.search(prompt or "")
    return [match.group(1).lower(), (match.group(2) or "--session").lower()] if match else None


def clear_enabled_overrides(state):
    """Drop every session / project / agent on-off override, keeping their voices."""
    overrides = state.get("voice_overrides", {})
    for name in list(overrides):
        overrides[name].pop("enabled", None)
        if not overrides[name]:
            del overrides[name]


def this_agent():
    return "claude" if os.environ.get("CLAUDECODE") else "antigravity"


def scope_target(scope, explicit_agent=None):
    """(scope, value) for the session, project or agent running this command; value is None if unknown."""
    if scope == "session":
        return scope, os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
    if scope == "project":
        return scope, os.getcwd()
    return scope, explicit_agent or this_agent()


def set_override(state, scope, value, key, val):
    """Set (or with val=None clear) one scope's voice or enabled override; returns a sentence describing it."""
    overrides = state.setdefault("voice_overrides", {})
    name = override_key(scope, value)
    where = {"session": "this session", "project": f"project {os.path.basename(value) or value}",
             "agent": value.capitalize()}[scope]
    entry = dict(overrides.pop(name, {}))  # re-insert so the newest sessions are kept when pruning
    if val is None:
        if key == "enabled":
            entry.pop("enabled", None)
            msg = f"Speech for {where} reset to the default."
        else:
            for k in ("kokoro_voice", "voice", "edge_voice"):
                entry.pop(k, None)
            msg = f"Voice for {where} reset to the default."
        if entry:
            overrides[name] = entry
        return msg
    entry[key] = val
    overrides[name] = entry
    sessions = [k for k in overrides if k.startswith("session:")]
    for old in sessions[:-MAX_SESSION_OVERRIDES]:
        del overrides[old]
    if key == "enabled":
        return f"Speech for {where} turned {'ON' if val else 'OFF'}."
    return f"Voice for {where} set to {val}."


KOKORO_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole", "af_nova",
    "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
    "am_michael", "am_onyx", "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
CHIRP_VOICES = [
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe", "Callirrhoe", "Charon",
    "Despina", "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia", "Leda", "Orus",
    "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel",
    "Vindemiatrix", "Zephyr", "Zubenelgenubi",
]
EDGE_VOICES = [
    "en-US-AndrewNeural", "en-US-AvaNeural", "en-US-BrianNeural", "en-US-EmmaNeural", "en-US-AriaNeural",
    "en-US-ChristopherNeural", "en-US-EricNeural", "en-US-GuyNeural", "en-US-JennyNeural", "en-US-MichelleNeural",
    "en-US-RogerNeural", "en-US-SteffanNeural", "en-GB-LibbyNeural", "en-GB-MaisieNeural", "en-GB-RyanNeural",
    "en-GB-SoniaNeural", "en-GB-ThomasNeural",
]


def list_voices(state):
    """Voices for the active engine, with the current one marked by *."""
    engine = state["engine"]
    mark = lambda name, current: f"*{name}*" if name == current else name
    if engine == "kokoro":
        groups = {"American female": "af_", "American male": "am_", "British female": "bf_", "British male": "bm_"}
        lines = [f"{label}: " + ", ".join(mark(n.split("_")[1], state["kokoro_voice"].split("_")[1])
                                          for n in KOKORO_VOICES if n.startswith(prefix))
                 for label, prefix in groups.items()]
    elif engine == "google":
        current = state["voice"].split("-")[-1]
        lines = ["Chirp 3 HD: " + ", ".join(mark(n, current) for n in CHIRP_VOICES),
                 "WaveNet (fallback family): Wavenet-A to Wavenet-J, e.g. /speak voice Wavenet-D"]
    elif engine == "edge":
        current = state["edge_voice"]
        short = lambda n: n.split("-")[2].removesuffix("Neural")
        lines = [f"Edge {region}: " + ", ".join(mark(short(n), short(current)) for n in EDGE_VOICES if f"-{region}-" in n)
                 for region in ("US", "GB")]
    else:
        models = sorted(f.removesuffix(".onnx") for f in os.listdir(PIPER_MODELS_DIR) if f.endswith(".onnx"))
        lines = ["Piper models: " + ", ".join(models) + " (uses en_US-joe-medium)"]
    return f"Voices for engine {engine} (current marked with *):\n" + "\n".join(lines)


def resolve_voice(arg, engine):
    """Map a user-typed voice to (state key, full voice name), tolerating short names and typos."""
    if re.fullmatch(r"[a-z]{2}_\w+", arg):  # full Kokoro name
        return "kokoro_voice", arg
    if arg.endswith("Neural"):  # full Edge name
        return "edge_voice", arg
    if arg.count("-") >= 3:  # full Google name
        return "voice", arg
    if arg.count("-") == 1:  # Google family short name, e.g. Wavenet-D
        return "voice", f"en-US-{arg}"
    if engine == "edge":
        edge = {n.split("-")[2].removesuffix("Neural").lower(): n for n in EDGE_VOICES}
        close = difflib.get_close_matches(arg.lower(), edge, n=1, cutoff=0.75)
        return ("edge_voice", edge[close[0]]) if close else None
    kokoro = {n.split("_")[1]: n for n in KOKORO_VOICES}
    chirp = {n.lower(): n for n in CHIRP_VOICES}
    # Prefer the current engine's voices when a name exists in both (Kore, Puck, Fenrir, Aoede)
    tables = [("kokoro_voice", kokoro), ("voice", chirp)]
    if engine == "google":
        tables.reverse()
    name = arg.lower()
    for key, table in tables:
        if name in table:
            return key, table[name] if key == "kokoro_voice" else f"en-US-Chirp3-HD-{table[name]}"
    for key, table in tables:
        close = difflib.get_close_matches(name, table, n=1, cutoff=0.75)
        if close:
            return key, table[close[0]] if key == "kokoro_voice" else f"en-US-Chirp3-HD-{table[close[0]]}"
    return None


def status(state):
    # Voice and enabled state as this session would hear it, with its session / project / agent override applied
    global_enabled = state["enabled"]
    state = voices_for(state, scope_target("session")[1], os.getcwd(), this_agent())
    usage = state["usage"]
    voice = {"edge": state["edge_voice"], "kokoro": state["kokoro_voice"]}.get(
        state["engine"], f"{state['voice']} (then {state['fallback_voice']})")
    google = ", ".join(f"{family} {usage.get(family, 0):,} / {budget:,}"
                       for family, budget in state["budgets"].items())
    idle = state.get("idle_minutes", 20)
    enabled_str = f"{'ON' if state['enabled'] else 'OFF'}"
    if state["enabled"] != global_enabled:
        enabled_str += f" (default {'ON' if global_enabled else 'OFF'})"
    return (
        f"Speech is {enabled_str} | "
        f"server: {'running' if speak_server.request({'cmd': 'ping'}) else 'not running'} | engine: {state['engine']} | "
        f"voice: {voice} | local fallback: Kokoro {state['kokoro_voice']} ({state['kokoro_model']} model) | speed {state['speed']}x | "
        f"glow {'on' if state.get('glow', True) else 'off'} ({state['glow_size']} px, corners {state['glow_corner']}) | "
        f"highlight {'on' if state['highlight'] else 'off'} | "
        f"{f'unloads after {idle} min idle' if idle else 'stays loaded'} | "
        f"Google free chars used {usage.get('month') or 'this month'}: {google}"
    )


def main(args):
    state = load_state()
    scope = next((a[2:].lower() for a in args if a.lower() in SCOPES), None)
    args = [a for a in args if a.lower() not in SCOPES]
    explicit_agent = None
    if scope == "agent":
        explicit_agent = next((AGENTS[a.lower()] for a in args if a.lower() in AGENTS), None)
        if explicit_agent:
            args = [a for a in args if a.lower() not in AGENTS]
    cmd = args[0].lower() if args else "toggle"
    arg = " ".join(args[1:]).strip()
    note = ""

    if cmd in ("toggle", "on", "off", "default"):
        # Plain on/off/toggle acts on this session; --all is the global switch every session inherits
        if scope is None:
            scope = "session" if scope_target("session")[1] else "all"
            if scope == "all":
                note = "No session ID here, so this applies to every session."
        if scope == "all":
            if cmd != "default":
                state["enabled"] = not state["enabled"] if cmd == "toggle" else cmd == "on"
            clear_enabled_overrides(state)
            if not note:
                note = (f"Every session now follows the default (speech {'ON' if state['enabled'] else 'OFF'})."
                        if cmd == "default" else f"Speech turned {'ON' if state['enabled'] else 'OFF'} everywhere.")
        else:
            scope, value = scope_target(scope, explicit_agent)
            if not value:
                print("This session's ID isn't available here; use --project, --agent or --all instead.")
                return
            if cmd == "default":
                val = None
            elif cmd == "toggle":
                # What this scope hears now: its own override, else the broader ones above it
                session = scope_target("session")[1] if scope == "session" else None
                project = os.getcwd() if scope in ("session", "project") else None
                agent = value if scope == "agent" else this_agent()
                val = not voices_for(state, session, project, agent)["enabled"]
            else:
                val = cmd == "on"
            note = set_override(state, scope, value, "enabled", val)
    elif cmd == "voice" and arg and scope and scope != "all":
        scope, value = scope_target(scope, explicit_agent)
        if not value:
            print("This session's ID isn't available here; use --project or --agent instead.")
            return
        if arg.lower() == "default":
            note = set_override(state, scope, value, None, None)
        else:
            match = resolve_voice(arg, state["engine"])
            if not match:
                print(f"Unknown voice '{arg}'. Try /speak voices.")
                return
            note = set_override(state, scope, value, *match)
    elif cmd == "voice" and arg:
        match = resolve_voice(arg, state["engine"])
        if not match:
            print(f"Unknown voice '{arg}'. Kokoro: {', '.join(n.split('_')[1] for n in KOKORO_VOICES)}. "
                  f"Google Chirp 3 HD: {', '.join(CHIRP_VOICES)}.")
            return
        key, value = match
        state[key] = value
    elif cmd == "engine" and arg in ("google", "kokoro", "edge", "piper"):
        state["engine"] = arg
    elif cmd == "speed" and re.fullmatch(r"\d+(\.\d+)?%?", arg):
        # "1.1", "110%" -> 1.1; clamped to a range every engine accepts
        value = float(arg.rstrip("%")) / (100 if arg.endswith("%") else 1)
        state["speed"] = round(min(max(value, 0.5), 2.0), 2)
    elif cmd == "glow" and arg in ("on", "off"):
        state["glow"] = arg == "on"
    elif cmd == "glow" and re.fullmatch(r"size \d+", arg):
        state["glow_size"] = min(max(int(arg.split()[1]), 1), 100)
    elif cmd == "glow" and re.fullmatch(r"corner (\d+|auto)", arg):
        value = arg.split()[1]
        state["glow_corner"] = value if value == "auto" else min(int(value), 50)
    elif cmd == "highlight" and arg in ("on", "off"):
        state["highlight"] = arg == "on"
    elif cmd == "model" and arg in ("small", "full"):
        if state["kokoro_model"] != arg:
            state["kokoro_model"] = arg
            if speak_server.request({"cmd": "shutdown"}):  # restarted with the new model below
                for _ in range(50):
                    if not speak_server.request({"cmd": "ping"}, timeout=0.2):
                        break
                    time.sleep(0.1)
    elif cmd == "idle" and arg.isdigit():
        state["idle_minutes"] = int(arg)  # 0 keeps the server (and its RAM) loaded until /speak off
    elif cmd == "voices":
        print(list_voices(state))
        return
    elif cmd == "test":
        # In the voice this session would get, and queued like one of its replies
        source = {"session": scope_target("session")[1], "project": os.getcwd(), "agent": this_agent(),
                  "label": os.path.basename(os.path.normpath(os.getcwd()))}
        if not speak_server.request({"cmd": "speak", "text": "This is how I sound.", **source}):
            speak_server.start_detached("This is how I sound.", source)
    elif cmd != "status":
        print("Usage: /speak [on|off|default [--session|--project|--agent|--all]|status|voices|voice <name|default> [--session|--project|--agent]|engine <google|kokoro|edge|piper>|speed <1.1>|glow <on|off|size <px>|corner <px|auto>>|highlight <on|off>|idle <minutes>|model <small|full>|test]")
        return

    save_state(state)
    # The background server (models preloaded) runs while global or any scoped speech is on
    any_enabled = state["enabled"] or any(v.get("enabled") for v in state.get("voice_overrides", {}).values())
    if any_enabled and not speak_server.request({"cmd": "ping"}):
        speak_server.start_detached()
    elif not any_enabled:
        speak_server.request({"cmd": "shutdown"})
        stop_standalone()
    print((note + " " if note else "") + status(state))


if __name__ == "__main__":
    main(sys.argv[1:])
