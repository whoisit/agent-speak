"""Queued speech player for the background server: pause/resume, skip, and a live output level.

Each reply is split into paragraphs and chunks. A producer thread synthesizes a few chunks ahead
of playback; audio goes through one sounddevice stream so pausing keeps the exact position.
"""
import os
import re
import shutil
import tempfile
import threading
from collections import deque

import numpy as np
import sounddevice as sd
import soundfile as sf

import speak

RATE = 24000      # Kokoro's native rate; other engines are resampled to it
LOOKAHEAD = 3     # chunks synthesized ahead of the one playing


def load_audio(path):
    """Read a WAV/MP3 as mono float32 at RATE."""
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    data = data.mean(axis=1)
    if sr != RATE:
        data = np.interp(np.arange(0, len(data), sr / RATE), np.arange(len(data)), data).astype(np.float32)
    return data


SOURCE_KEYS = ("session", "label", "project", "agent", "window", "engine", "voice")


class Reply:
    def __init__(self, text, source=None):
        source = source or {}
        self.session = source.get("session")   # which agent session it came from; None = one-off
        self.label = source.get("label") or ""  # shown in the tray and panel, e.g. the project folder
        self.project = source.get("project")   # session's folder, for per-project voices
        self.agent = source.get("agent")       # "claude" or "antigravity", for per-agent voices
        self.window = source.get("window")     # handle of the session's window, for the glow
        self.engine = source.get("engine")     # speak.py --engine / --voice; None = from state
        self.voice = source.get("voice")
        # (paragraph index, chunk text) pairs; paragraphs are separated by blank lines
        self.paragraphs = re.split(r"\n\s*\n", text.strip())
        self.chunks = [(p, chunk) for p, para in enumerate(self.paragraphs)
                       for chunk in speak.split_chunks(para) if chunk.strip()]
        self.audio = [None] * len(self.chunks)
        self.ready = [threading.Event() for _ in self.chunks]
        self.index = 0             # chunk being played
        self.cancelled = False
        self.skip_paragraph = False


class Player:
    def __init__(self):
        self.cond = threading.Condition()
        self.queue = deque()
        self.current = None
        self.paused = False
        self.running = True
        self.loaded = threading.Event()  # set once Kokoro is in memory
        # Playback buffer shared with the audio callback, guarded by its own short-held lock
        self.buf_lock = threading.Lock()
        self.buf = None
        self.pos = 0
        self.buf_done = threading.Event()
        self.peak = 0.0
        self.stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="float32",
                                      blocksize=1024, callback=self._callback)
        threading.Thread(target=self._run, daemon=True).start()

    # --- controls (called from the socket server and the tray) ---

    def say(self, text, source=None):
        """Queue text behind everything already playing or waiting; returns its Reply (None if
        nothing speakable).

        Nothing is ever cut off: replies play in the order they arrive, whatever session, window
        or tab they come from, and so do clipboard and one-off readings. Skip paragraph, next
        reply and stop are how to move on. source holds SOURCE_KEYS (all optional).
        """
        reply = Reply(text, source)
        if not reply.chunks:
            return None
        speak.stop_standalone()  # a standalone speak.py may still be playing
        with self.cond:
            self.queue.append(reply)
            self.cond.notify_all()
        return reply

    def toggle_pause(self):
        with self.cond:
            self.paused = not self.paused
            if self.paused:
                if self.stream.active:
                    self.stream.abort()
                    with self.buf_lock:  # replay what was still in the device buffer
                        self.pos = max(0, self.pos - int(self.stream.latency * RATE))
            elif self.current and not self.stream.active:
                self.stream.start()
            self.cond.notify_all()

    def skip_paragraph(self):
        with self.cond:
            if self.current:
                self.current.skip_paragraph = True
                self._interrupt()
                self.cond.notify_all()

    def next_reply(self):
        with self.cond:
            self._cancel_current()
            self.cond.notify_all()

    def cancel(self, reply):
        """Drop one reply, whether playing or still queued."""
        with self.cond:
            if reply is self.current:
                self._cancel_current()
            elif reply in self.queue:
                self.queue.remove(reply)
                reply.cancelled = True
            self.cond.notify_all()

    def stop(self):
        """Stop speaking and drop everything queued."""
        with self.cond:
            self.queue.clear()
            self._cancel_current()
            self.cond.notify_all()

    def shutdown(self):
        self.stop()
        with self.cond:
            self.running = False
            self.cond.notify_all()
        self.stream.abort()

    def status(self):
        """(state, queued replies, label of the current reply); state is loading|idle|speaking|paused."""
        with self.cond:
            if not self.loaded.is_set():
                state = "loading"
            elif self.paused:
                state = "paused"
            else:
                state = "speaking" if self.current else "idle"
            return state, len(self.queue), self.current.label if self.current else ""

    def chunk_progress(self):
        """How much of the playing chunk has been heard, 0..1 (what's still in the device buffer isn't)."""
        with self.buf_lock:
            buf, pos = self.buf, self.pos
        if buf is None or not len(buf):
            return 0.0
        latency = int(self.stream.latency * RATE) if self.stream.active else 0
        return max(0, pos - latency) / len(buf)

    def read_level(self):
        """Loudest block RMS since the last call (0..~0.5)."""
        level, self.peak = self.peak, 0.0
        return level

    # --- internals ---

    def _cancel_current(self):
        if self.current:
            self.current.cancelled = True
            self._interrupt()

    def _interrupt(self):
        with self.buf_lock:
            self.buf = None
        self.buf_done.set()

    def _callback(self, outdata, frames, time_info, status):
        with self.buf_lock:
            buf, pos = self.buf, self.pos
            if buf is None:
                outdata.fill(0)
                return
            block = buf[pos:pos + frames]
            self.pos = pos + len(block)
            if self.pos >= len(buf):
                self.buf = None
                self.buf_done.set()
        outdata[:len(block), 0] = block
        outdata[len(block):] = 0
        if len(block):
            self.peak = max(self.peak, float(np.sqrt(np.mean(block * block))))

    def _run(self):
        self.loaded.wait()
        while True:
            with self.cond:
                while self.running and (not self.queue or self.paused and not self.current):
                    self.cond.wait()
                if not self.running:
                    return
                self.current = reply = self.queue.popleft()
            try:
                self._play_reply(reply)
            except Exception as e:
                print(f"Playback error: {e}", flush=True)
            with self.cond:
                reply.cancelled = True
                self.current = None
                if not self.queue and self.stream.active:
                    self.stream.stop()
                self.cond.notify_all()

    def _play_reply(self, reply):
        threading.Thread(target=self._synthesize, args=(reply,), daemon=True).start()
        i = 0
        while i < len(reply.chunks) and not reply.cancelled:
            with self.cond:
                reply.index = i
                self.cond.notify_all()  # let the producer move ahead
            while not reply.ready[i].wait(0.1):
                if reply.cancelled or reply.skip_paragraph:
                    break
            audio = reply.audio[i] if reply.ready[i].is_set() else None
            if isinstance(audio, Exception):  # skip just this chunk; the rest of the reply still plays
                print(f"Error synthesizing speech: {audio}", flush=True)
                audio = None
            if audio is not None and not (reply.cancelled or reply.skip_paragraph):
                self._play_buffer(audio)
            if reply.skip_paragraph:
                reply.skip_paragraph = False
                para = reply.chunks[i][0]
                while i < len(reply.chunks) and reply.chunks[i][0] == para:
                    i += 1
            else:
                i += 1

    def _play_buffer(self, audio):
        with self.buf_lock:
            self.buf, self.pos = audio, 0
            self.buf_done.clear()
        with self.cond:
            if not self.paused and not self.stream.active:
                self.stream.start()
        self.buf_done.wait()

    def _synthesize(self, reply):
        state = speak.voices_for(speak.load_state(), reply.session, reply.project, reply.agent)
        synth = speak.Synthesizer(state, reply.engine or state["engine"], reply.voice, speak.VOICE_MODEL)
        tmp_dir = tempfile.mkdtemp(prefix="speak_")
        try:
            i = 0
            while True:
                with self.cond:
                    while not reply.cancelled and i > reply.index + LOOKAHEAD:
                        self.cond.wait()
                    i = max(i, reply.index)  # skipped chunks are never synthesized
                    if reply.cancelled or i >= len(reply.chunks):
                        return
                synth.state["speed"] = speak.load_state()["speed"]  # speed changes apply from the next chunk
                try:
                    path = synth.synth(reply.chunks[i][1], os.path.join(tmp_dir, str(i)))
                    reply.audio[i] = load_audio(path)
                    os.remove(path)
                except Exception as e:
                    reply.audio[i] = e
                reply.ready[i].set()
                i += 1
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
