import os
import re
import sys
import json
import base64
import ctypes
import asyncio
import subprocess
import tempfile
import argparse
import functools
import threading
import time
import urllib.request
import urllib.error
from datetime import date

# Everything is found relative to this file: <repo>/.agents/skills/speak/scripts/speak.py
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))
PIPER_EXE = os.path.join(WORKSPACE_DIR, "piper", "piper.exe")
PIPER_MODELS_DIR = os.path.join(WORKSPACE_DIR, "piper_models")
VOICE_MODEL = os.path.join(PIPER_MODELS_DIR, "en_US-joe-medium.onnx")
KOKORO_DIR = os.path.join(WORKSPACE_DIR, "kokoro_models")
KOKORO_MODELS = {"small": "kokoro-v1.0.int8.onnx", "full": "kokoro-v1.0.onnx"}  # /speak model small|full
PID_FILE = os.path.join(SKILL_DIR, "speak.pid")
STATE_FILE = os.path.join(SKILL_DIR, "state.json")
ENV_FILE = os.path.join(WORKSPACE_DIR, ".env")

GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
NO_WINDOW = subprocess.CREATE_NO_WINDOW
CHUNK_CHARS = 400  # Kokoro reads at most 510 phonemes, about 430 characters, in one pass
CHUNK_GROWTH = 2   # a chunk is at most twice as long as the one before, so it's ready before that one ends
MIN_GROWN = 100    # ...but may always be this long, so a short opening doesn't hold the rest back
PAUSES = {"clause": 0.1, "sentence": 0.25, "paragraph": 0.5}  # seconds of silence after a chunk, at speed 1

DEFAULT_STATE = {
    "enabled": False,
    "engine": "kokoro",  # preferred engine: "kokoro", "google", "edge" or "piper"
    "kokoro_voice": "am_michael",
    "kokoro_model": "small",  # "small" (int8, ~190 MB RAM) or "full" (~400 MB RAM)
    "speed": 1.0,  # 1.1 = 10% faster, applied to every engine
    "voice": "en-US-Chirp3-HD-Charon",  # Google voice
    "fallback_voice": "en-US-Wavenet-D",  # used once the main voice's family is out of free characters
    "edge_voice": "en-US-AndrewNeural",
    "glow_size": 100,  # how far the glow reaches from the window's edge, in pixels at 96 dpi
    "glow_side": "inside",  # "inside": over the window's edge, fading inward; "outside": around the window
    "glow_opacity": 50,  # percent, for the whole glow
    "glow_corner": "auto",  # window corner radius in pixels at 96 dpi; "auto": rounded on Windows 11 only
    "highlight": True,  # highlight the sentence being read in the session's terminal
    # Monthly character limits per Google voice family, kept under each free tier
    # (Chirp 3 HD: 1M free, WaveNet: 4M free per the Cloud TTS pricing page)
    "budgets": {"Chirp3": 900000, "Wavenet": 3800000},
    "usage": {"month": ""},  # characters used per voice family this month
}
DEFAULT_FAMILY_BUDGET = 900000  # for families without a known free tier


def voice_family(voice):
    """en-US-Chirp3-HD-Charon -> Chirp3, en-US-Wavenet-D -> Wavenet."""
    parts = voice.split("-")
    return parts[2] if len(parts) > 3 else "unknown"


def load_state():
    state = dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
    except Exception:
        pass
    if state["engine"] == "auto":  # pre-Edge name for "google first"
        state["engine"] = "google"
    if "chars" in state["usage"]:  # older single counter was all Chirp 3 HD
        state["usage"]["Chirp3"] = state["usage"].pop("chars")
    state.pop("monthly_char_budget", None)
    state.pop("max_chars", None)  # replies are always read in full now
    return state


def save_state(state):
    os.makedirs(SKILL_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def override_key(scope, value):
    """Key in state["voice_overrides"]: "session:<id>", "project:<folder>" or "agent:<claude|antigravity>"."""
    if scope == "project":
        value = os.path.normcase(os.path.normpath(value))
    return f"{scope}:{value}"


def voices_for(state, session=None, project=None, agent=None):
    """state with the overrides for a reply's agent, project and session applied, most specific last.

    Overrides hold the same keys as state (enabled, kokoro_voice, voice, edge_voice), so each scope
    can override speech on/off or the voice for a specific engine.
    """
    overrides = state.get("voice_overrides", {})
    merged = dict(state)
    for scope, value in (("agent", agent), ("project", project), ("session", session)):
        if value:
            merged.update(overrides.get(override_key(scope, value), {}))
    return merged


def antigravity_session(data):
    """Session ID for an Antigravity hook payload: its conversationId, else the conversation folder
    in transcriptPath (.../brain/<uuid>/...), else the transcript's folder."""
    if data.get("conversationId"):
        return data["conversationId"]
    transcript = data.get("transcriptPath") or ""
    match = re.search(r"[/\\]brain[/\\]([0-9a-fA-F-]{36})[/\\]", transcript)
    if match:
        return match.group(1)
    return ("agy:" + os.path.dirname(transcript)) if transcript else None


AGY_SESSIONS_FILE = os.path.join(SKILL_DIR, "agy_sessions.json")


def remember_antigravity_session(session):
    """Note which conversation the agy process above this (prompt hook) process is running.

    agy doesn't pass the conversation ID to the commands its model runs, so a speak_ctl run from
    there looks it up by its agy ancestor (antigravity_session_here)."""
    import windows
    agent, pid = windows.agent_process()
    if agent != "antigravity" or not session:
        return
    try:
        with open(AGY_SESSIONS_FILE, "r", encoding="utf-8") as f:
            sessions = json.load(f)
    except Exception:
        sessions = {}
    if sessions.get(str(pid)) == session:
        return
    alive = windows.running_pids()
    sessions = {k: v for k, v in sessions.items() if k.isdigit() and int(k) in alive}
    sessions[str(pid)] = session
    try:
        with open(AGY_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f)
    except Exception:
        pass


def antigravity_session_here():
    """Conversation of the agy process above this one, as noted by its prompt hook, or None."""
    import windows
    agent, pid = windows.agent_process()
    if agent != "antigravity":
        return None
    try:
        with open(AGY_SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get(str(pid))
    except Exception:
        return None


def agy_print_mode(args):
    """True if agy arguments (without the program) run it headless: -p, --print or --prompt, also
    written -print or --print=... (Go-style flags). --print-timeout is another flag."""
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-") and arg.lstrip("-").split("=", 1)[0] in ("p", "print", "prompt"):
            return True
    return False


@functools.lru_cache(maxsize=None)
def unattended(agent=None):
    """True for a run nobody is attending (agy -p, claude -p, SDK runs): never spoken, and it can't
    change speech settings. agent ("claude" / "antigravity") if the caller knows it, else found here.

    SPEAK_UNATTENDED=1 marks any agent's run as unattended. Anything unknown counts as attended."""
    if os.environ.get("SPEAK_UNATTENDED") == "1":
        return True
    try:
        import windows
        if agent is None:
            agent = windows.agent_process()[0] or ("claude" if os.environ.get("CLAUDECODE") else None)
        if agent == "claude":  # Claude Code sets this in every child process: 1 interactive, 0 for -p
            return os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0"
        if agent == "antigravity":
            found, pid = windows.agent_process()
            return found == "antigravity" and agy_print_mode((windows.process_argv(pid) or [])[1:])
    except Exception:
        pass
    return False


def is_enabled(state=None, session=None, project=None, agent=None):
    """True if speech is enabled for this session, project, and agent after applying overrides,
    and someone is attending the run."""
    if not voices_for(state or load_state(), session, project, agent).get("enabled"):
        return False
    return not unattended(agent)


def google_key():
    key = os.environ.get("GOOGLE_TTS_API_KEY")
    if key:
        return key
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("GOOGLE_TTS_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return None


def stop_standalone():
    """Terminate a standalone speak.py process (tracked in PID_FILE) if one is playing."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid():
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(old_pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW
                )
            os.remove(PID_FILE)
        except Exception:
            pass


def _standalone_alive(pid):
    """True if pid is a running Python process (a speak.py still playing), not a reused PID."""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)) or code.value != 259:
            return False  # 259 = STILL_ACTIVE
        name, size = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
        k32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, name, ctypes.byref(size))
        return "python" in os.path.basename(name.value).lower()
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def wait_turn():
    """Wait while another standalone speak.py is playing, then register this one, so one-off
    readings take turns instead of cutting each other off (stop_standalone still stops one)."""
    while True:
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            break
        if pid == os.getpid() or not _standalone_alive(pid):
            break
        time.sleep(0.2)
    try:
        os.makedirs(SKILL_DIR, exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def synth_google(text, voice, out_path, speed=1.0):
    key = google_key()
    if not key:
        raise RuntimeError("GOOGLE_TTS_API_KEY not set (env var or .env)")
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
        "audioConfig": {"audioEncoding": "LINEAR16", "speakingRate": speed},
    }
    req = urllib.request.Request(
        f"{GOOGLE_TTS_URL}?key={key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Google TTS HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(data["audioContent"]))


def synth_edge(text, voice, out_path, speed=1.0):
    # Unofficial Edge "Read Aloud" service: free, but occasionally stalls, hence short timeouts
    import edge_tts
    for attempt in range(2):  # ~1 in 3 requests returns no audio; a retry usually works
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=f"{round((speed - 1) * 100):+d}%",
                                                connect_timeout=5, receive_timeout=8).save(out_path))
            return
        except edge_tts.exceptions.NoAudioReceived:
            if attempt:
                raise


_kokoro = None


def load_kokoro():
    """Load the Kokoro model once per process (~2s); the speech server calls this at startup.

    onnxruntime's CPU memory arena keeps every buffer it ever allocated and would more than
    double the server's RAM (about 1.1 GB vs 400 MB) for no measurable speed gain, so it's off.
    """
    global _kokoro
    if _kokoro is None:
        import onnxruntime
        from kokoro_onnx import Kokoro
        # The chosen size, or the other one if only that was downloaded
        wanted = KOKORO_MODELS.get(load_state()["kokoro_model"], KOKORO_MODELS["small"])
        model = next((os.path.join(KOKORO_DIR, name) for name in [wanted, *KOKORO_MODELS.values()]
                      if os.path.exists(os.path.join(KOKORO_DIR, name))), None)
        if not model:
            raise RuntimeError("No Kokoro model in kokoro_models/; run scripts/download_models.py")
        options = onnxruntime.SessionOptions()
        options.enable_cpu_mem_arena = False
        session = onnxruntime.InferenceSession(model, options, providers=["CPUExecutionProvider"])
        _kokoro = Kokoro.from_session(session, os.path.join(KOKORO_DIR, "voices-v1.0.bin"))
        _kokoro.create("a", voice="af_heart", speed=1.0, lang="en-us")  # warm up espeak-ng and ONNX buffers
    return _kokoro


def create_kokoro(text, voice, speed=1.0):
    """Synthesize with local Kokoro-82M in memory; returns (float32 mono array, 24000)."""
    audio, sample_rate = load_kokoro().create(text, voice=voice, speed=speed,
                                               lang="en-gb" if voice.startswith("b") else "en-us")
    if len(audio) == 0:
        raise RuntimeError("empty audio")
    return audio, sample_rate


def synth_kokoro(text, voice, out_path, speed=1.0):
    # Local Kokoro-82M via ONNX: free and unlimited, ~2x realtime on CPU
    import soundfile as sf
    audio, sample_rate = create_kokoro(text, voice, speed)
    sf.write(out_path, audio, sample_rate)


def synth_piper(text, voice_model, out_path, speed=1.0):
    if not os.path.exists(PIPER_EXE):
        raise RuntimeError(f"piper.exe not found at {PIPER_EXE}")
    if not os.path.exists(voice_model):
        raise RuntimeError(f"Voice model not found at {voice_model}")
    process = subprocess.Popen(
        [PIPER_EXE, "-m", voice_model, "-f", out_path, "--length_scale", str(1 / speed)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",  # Windows would default to cp1252 and crash on emoji
        errors="replace",
        creationflags=NO_WINDOW
    )
    _, stderr = process.communicate(input=text)
    if process.returncode != 0:
        raise RuntimeError(f"Piper synthesis failed: {stderr}")


def play(path, stop_event=None):
    """Play a WAV or MP3 file via the Windows MCI API until it ends or stop_event is set."""
    mci = ctypes.windll.winmm.mciSendStringW
    alias = f"speak{threading.get_ident()}"
    if mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None) != 0:
        raise RuntimeError(f"Could not open audio file {path}")
    try:
        mci(f"play {alias}", None, 0, None)
        status = ctypes.create_unicode_buffer(32)
        while not (stop_event and stop_event.is_set()):
            mci(f"status {alias} mode", status, 32, None)
            if status.value != "playing":
                break
            time.sleep(0.05)
    finally:
        mci(f"close {alias}", None, 0, None)


def paragraphs(text):
    """The text's paragraphs (separated by blank lines), each on one line, leaving out ones with
    nothing to say, like "---" or a lone emoji (they would fail to synthesize)."""
    paras = (" ".join(p.split()) for p in re.split(r"\n\s*\n", text.strip()))
    return [p for p in paras if re.search(r"\w", p)]


def split_chunks(paras, size=CHUNK_CHARS):
    """Split paragraphs into chunks to synthesize one at a time: [(first paragraph index, text, pause)].

    Every synthesis call gets its own intonation, falling at its end, so each join resets the
    voice: the fewer joins the better. The first chunk is one sentence (or clause), so playback
    starts in about a second. Synthesis runs ~2.6x faster than playback, so each later chunk can be
    CHUNK_GROWTH times longer than the one before and still be ready in time, up to `size`.

    Short paragraphs and bullets share a chunk, one per line of its text. A chunk ends inside a
    paragraph only if it began in it, so skipping a paragraph can always move to a chunk boundary.
    `pause` is the silence (seconds at speed 1) to play after the chunk, by what kind of break follows.
    """
    sentences = [(p, s) for p, para in enumerate(paras)
                 for s in re.split(r"(?<=[.!?])\s+", para) if re.search(r"\w", s)]
    if not sentences:
        return []
    # A long first sentence is split at its first comma past 15 chars, so the opening chunk stays short
    p0, s0 = sentences[0]
    first = re.match(r"(.{15,100}?,)\s+(.+)", s0) if len(s0) > 100 else None
    if first:
        sentences[:1] = [(p0, first.group(1)), (p0, first.group(2))]

    chunks, start, text, limit = [], None, "", 0  # limit 0: the first sentence goes alone
    for p, s in sentences:
        last = start + text.count("\n") if text else None  # paragraph the chunk so far ends in
        # A new paragraph joins the chunk only if all of it fits; within one, sentence by sentence
        added = len(paras[p]) if p != last else len(s)
        if text and len(text) + 1 + added > limit:
            chunks.append((start, text))
            limit = min(size, max(MIN_GROWN, CHUNK_GROWTH * len(text)))
            text = ""
        if not text:
            start, text = p, s
        else:
            text += ("\n" if p != last else " ") + s
    chunks.append((start, text))

    result = []
    for i, (p, text) in enumerate(chunks):
        ends_para = i + 1 == len(chunks) or chunks[i + 1][0] != p + text.count("\n")
        kind = "paragraph" if ends_para else "clause" if text.endswith(",") else "sentence"
        result.append((p, text, PAUSES[kind]))
    return result


def next_paragraph(chunks, i):
    """Index of the first chunk after chunk i that starts a paragraph (len(chunks) if none)."""
    last = chunks[i][0] + chunks[i][1].count("\n")  # paragraph chunk i ends in
    i += 1
    while i < len(chunks) and chunks[i][0] == last:  # carries on the paragraph chunk i ended inside
        i += 1
    return i


def speakable(chunk):
    """A chunk's text as one line for the engine. A paragraph or bullet with no closing punctuation
    gets a full stop, or it would run straight into the next one with no pause."""
    return " ".join(line if re.search(r"[.!?:;,]$", line) else line + "."
                    for line in chunk.split("\n"))


class Synthesizer:
    """Tries engines in order, sticking with the first one that works."""

    def __init__(self, state, engine, voice, voice_model):
        self.state = state
        self.voice_model = voice_model
        is_kokoro = bool(voice and re.fullmatch(r"[a-z]{2}_\w+", voice))
        is_edge = bool(voice and "Neural" in voice)
        self.voices = {"google": voice if voice and not (is_kokoro or is_edge) else state["voice"],
                       "edge": voice if is_edge else state["edge_voice"],
                       "kokoro": voice if is_kokoro else state["kokoro_voice"]}
        # Every engine reads the whole reply. Google moves from Chirp 3 HD to WaveNet as each free
        # tier runs out (google_voice_within_budget), and online engines fall back to local Kokoro
        self.order = {
            "google": ["google", "kokoro", "piper"],
            "kokoro": ["kokoro", "piper"],
            "edge": ["edge", "kokoro", "piper"],
            "piper": ["piper"],
        }[engine]

    def google_voice_within_budget(self, text):
        """First Google voice whose family (Chirp3, Wavenet, ...) still has free characters this month."""
        usage = self.state["usage"]
        month = date.today().strftime("%Y-%m")
        if usage.get("month") != month:
            usage.clear()
            usage["month"] = month
        for voice in (self.voices["google"], self.state["fallback_voice"]):
            family = voice_family(voice)
            budget = self.state["budgets"].get(family, DEFAULT_FAMILY_BUDGET)
            if usage.get(family, 0) + len(text) <= budget:
                return voice, family
        return None, None

    def synth(self, text, base_path, in_memory=False):
        """Synthesize one chunk with the first engine that works. Online engines that fail are
        skipped for the rest of the reply; local ones only for this chunk (a failure there is
        usually about the text, and the next chunk should still get the chosen voice).
        With in_memory=True, Kokoro returns its float32 mono array directly without touching disk.
        base_path may be a function returning the path, so a temp folder is only made when a file is."""
        for engine in list(self.order):
            if engine == "google":
                voice, family = self.google_voice_within_budget(text)
                if not voice:
                    print("Monthly free Google characters used up, skipping Google.", file=sys.stderr)
                    self.order.remove(engine)
                    continue
            speed = self.state["speed"]
            try:
                if engine == "kokoro" and in_memory:
                    return create_kokoro(text, self.voices["kokoro"], speed)[0]
                if callable(base_path):
                    base_path = base_path()
                path = base_path + (".mp3" if engine == "edge" else ".wav")
                if engine == "google":
                    synth_google(text, voice, path, speed)
                    self.state["usage"][family] = self.state["usage"].get(family, 0) + len(text)
                    save_state(self.state)
                elif engine == "edge":
                    synth_edge(text, self.voices["edge"], path, speed)
                elif engine == "kokoro":
                    synth_kokoro(text, self.voices["kokoro"], path, speed)
                else:
                    synth_piper(text, self.voice_model, path, speed)
                if os.path.getsize(path) == 0:
                    raise RuntimeError("empty audio")
                return path
            except Exception as e:
                print(f"{engine} failed, trying next engine: {e}", file=sys.stderr)
                if engine in ("google", "edge"):
                    self.order.remove(engine)
        raise RuntimeError("All TTS engines failed")


def speak_text(text, voice_model=VOICE_MODEL, engine=None, voice=None, stop_event=None):
    """Synthesize and play text chunk by chunk; returns early if stop_event is set."""
    state = load_state()
    synth = Synthesizer(state, engine or state["engine"], voice, voice_model)
    stop_event = stop_event or threading.Event()
    tmp_dir = tempfile.mkdtemp(prefix="speak_")
    chunks = [speakable(chunk) for _, chunk, _ in split_chunks(paragraphs(text))]
    results = [None] * len(chunks)
    ready = [threading.Event() for _ in chunks]

    def producer():
        # Synthesize ahead of playback so the gap between chunks stays small
        for i, chunk in enumerate(chunks):
            if stop_event.is_set():
                results[i] = None
            else:
                try:
                    results[i] = synth.synth(chunk, os.path.join(tmp_dir, f"{i}"))
                except Exception as e:
                    results[i] = e
            ready[i].set()

    producer_thread = threading.Thread(target=producer, daemon=True)
    producer_thread.start()
    ok = True
    try:
        for i in range(len(chunks)):
            ready[i].wait()
            if stop_event.is_set():
                break
            if isinstance(results[i], Exception):
                print(f"Error synthesizing speech: {results[i]}", file=sys.stderr)
                ok = False
                break
            play(results[i], stop_event)
        return ok
    except Exception as e:
        print(f"Error playing speech: {e}", file=sys.stderr)
        return False
    finally:
        stop_event.set()
        producer_thread.join(timeout=30)
        for name in os.listdir(tmp_dir):
            try:
                os.remove(os.path.join(tmp_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def speak(text, voice_model=VOICE_MODEL, engine=None, voice=None):
    """Read text once, after anything already playing. While the speech server runs (or speech is
    on, which starts it), the text joins its queue and this returns at once; otherwise it plays
    here, after any other standalone reading."""
    import speak_server
    import windows
    cwd = os.getcwd()
    source = {"label": os.path.basename(os.path.normpath(cwd)) or "speak.py", "project": cwd,
              "window": windows.session_window(), "engine": engine, "voice": voice}
    if voice_model == VOICE_MODEL:  # the server only knows the default Piper model
        if speak_server.request({"cmd": "speak", "text": text, **source}):
            return True
        if load_state()["enabled"]:
            speak_server.start_detached(text, source)
            return True
    wait_turn()
    try:
        return speak_text(text, voice_model, engine, voice)
    finally:
        # Clean up PID file if it's still ours
        try:
            if os.path.exists(PID_FILE):
                with open(PID_FILE, "r") as f:
                    content = f.read().strip()
                if content and int(content) == os.getpid():
                    os.remove(PID_FILE)
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Speak text using Google, Edge or local Piper TTS")
    parser.add_argument("text", type=str, help="Text to speak out loud")
    parser.add_argument("--model", type=str, default=VOICE_MODEL, help="Path to Piper ONNX voice model")
    parser.add_argument("--engine", choices=["google", "kokoro", "edge", "piper"], help="Override the engine from state.json")
    parser.add_argument("--voice", type=str, help="Google (…Chirp3-HD-…), Kokoro (am_michael) or Edge (…Neural) voice name")
    args = parser.parse_args()
    if unattended():
        print("Speech is off in headless runs.")
        sys.exit(0)

    success = speak(args.text, args.model, args.engine, args.voice)
    sys.exit(0 if success else 1)
