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
CHUNK_CHARS = 250  # first chunk is synthesized alone so playback starts quickly

DEFAULT_STATE = {
    "enabled": False,
    "engine": "kokoro",  # preferred engine: "kokoro", "google", "edge" or "piper"
    "kokoro_voice": "am_michael",
    "kokoro_model": "small",  # "small" (int8, ~190 MB RAM) or "full" (~400 MB RAM)
    "speed": 1.0,  # 1.1 = 10% faster, applied to every engine
    "voice": "en-US-Chirp3-HD-Charon",  # Google voice
    "fallback_voice": "en-US-Wavenet-D",  # used once the main voice's family is out of free characters
    "edge_voice": "en-US-AndrewNeural",
    "glow_size": 27,  # how far the glow reaches outside the window, in pixels at 96 dpi
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
    """state with the voice overrides for a reply's agent, project and session applied, most specific last.

    Overrides hold the same keys as state (kokoro_voice, voice, edge_voice), so each one only
    changes the engine it was set for.
    """
    overrides = state.get("voice_overrides", {})
    merged = dict(state)
    for scope, value in (("agent", agent), ("project", project), ("session", session)):
        if value:
            merged.update(overrides.get(override_key(scope, value), {}))
    return merged


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


def stop_previous():
    """Terminate the previous speak process tree so voices never overlap, then register this one."""
    stop_standalone()
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
    return _kokoro


def synth_kokoro(text, voice, out_path, speed=1.0):
    # Local Kokoro-82M via ONNX: free and unlimited, ~2x realtime on CPU
    import soundfile as sf
    audio, sample_rate = load_kokoro().create(text, voice=voice, speed=speed,
                                               lang="en-gb" if voice.startswith("b") else "en-us")
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


def split_chunks(text, size=CHUNK_CHARS):
    """Split into sentence-aligned chunks of roughly `size` characters.

    The first sentence is its own chunk so playback starts as early as possible.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    # A long first sentence is split at its first comma past 15 chars, so the opening chunk stays short
    first = re.match(r"(.{15,100}?,)\s+(.+)", sentences[0]) if len(sentences[0]) > 100 else None
    if first:
        sentences[:1] = [first.group(1), first.group(2)]
    chunks, current = sentences[:1], ""
    for s in sentences[1:]:
        if current and len(current) + len(s) + 1 > size:
            chunks.append(current)
            current = s
        else:
            current = f"{current} {s}".strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if re.search(r"\w", c)]  # a chunk like "---" or a lone emoji would fail to synthesize


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

    def synth(self, text, base_path):
        """Synthesize one chunk with the first engine that works. Online engines that fail are
        skipped for the rest of the reply; local ones only for this chunk (a failure there is
        usually about the text, and the next chunk should still get the chosen voice)."""
        for engine in list(self.order):
            if engine == "google":
                voice, family = self.google_voice_within_budget(text)
                if not voice:
                    print("Monthly free Google characters used up, skipping Google.", file=sys.stderr)
                    self.order.remove(engine)
                    continue
            path = base_path + (".mp3" if engine == "edge" else ".wav")
            speed = self.state["speed"]
            try:
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
    chunks = split_chunks(text)
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
    from speak_server import request
    request({"cmd": "stop"}, timeout=0.3)  # silence the background server if it's speaking
    stop_previous()
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

    success = speak(args.text, args.model, args.engine, args.voice)
    sys.exit(0 if success else 1)
