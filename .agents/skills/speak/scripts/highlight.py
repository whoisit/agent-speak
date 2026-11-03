"""Highlights, in the speaking session's window, the sentence being read.

A worker thread looks the sentence up on screen (screentext.py) a few times a second, and right away
when the sentence changes. The UI's Tk thread draws soft blue boxes behind it, one per line, in an
overlay (overlay.py) kept just above the window. Positions are kept relative to the window, so the
boxes follow it smoothly when it moves. Scrolling is picked up on the next lookup.

Chunks can hold a few sentences. Which one is being heard is estimated from how much of the chunk's
audio has played, taking each sentence's share of the characters as its share of the time.
"""
import re
import threading
import time

import numpy as np

import speak
import windows
from overlay import Overlay, fill
from screentext import ScreenText, words

COLOR = (80, 170, 255)  # the glow's blue
OPACITY = 0.32          # of the boxes over the text
PAD_X, PAD_Y = 3, 1     # box margin around the text, at 96 dpi
RADIUS = 4              # box corner radius, at 96 dpi
POLL = 0.25             # seconds between lookups while the sentence stays the same
FADE = 0.15             # seconds to fade out
FPS = 25
SETTINGS_EVERY = 1.0


def sentence_at(chunk, progress):
    """(first, last) word indexes, into words(chunk), of the sentence heard `progress` (0..1) of the way through."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", chunk) if words(s)]
    total = sum(len(s) for s in sentences) or 1
    first, heard = 0, 0
    for s in sentences:
        count = len(words(s))
        heard += len(s)
        if heard / total > progress or s is sentences[-1]:
            return first, first + count - 1
        first += count
    return 0, -1


def paint_boxes(pixels, boxes, radius, color, opacity):
    """Rounded, anti-aliased boxes (left, top, right, bottom in pixels) on a transparent background."""
    alpha = np.zeros(pixels.shape[:2])
    for left, top, right, bottom in boxes:
        r = min(radius, (right - left) / 2, (bottom - top) / 2)
        rows = slice(max(0, int(top)), int(np.ceil(bottom)))
        cols = slice(max(0, int(left)), int(np.ceil(right)))
        ys = (np.arange(rows.start, rows.stop) + 0.5)[:, None]
        xs = (np.arange(cols.start, cols.stop) + 0.5)[None, :]
        qx = np.abs(xs - (left + right) / 2) - ((right - left) / 2 - r)
        qy = np.abs(ys - (top + bottom) / 2) - ((bottom - top) / 2 - r)
        dist = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r
        np.maximum(alpha[rows, cols], np.clip(0.5 - dist, 0, 1), out=alpha[rows, cols])
    fill(pixels, color, alpha * opacity)


class Highlight:
    def __init__(self, root, player):
        self.player = player
        self.overlay = Overlay(root)
        scale = self.overlay.scale
        self.pad_x, self.pad_y, self.radius = PAD_X * scale, PAD_Y * scale, RADIUS * scale
        self.enabled = speak.DEFAULT_STATE["highlight"]
        self.checked = 0.0
        self.job = None    # (window, reply, chunk index, (first, last) words) for the worker to find
        self.found = None  # (job, boxes relative to the window's top left) from the worker
        self.wake = threading.Event()
        threading.Thread(target=self._work, daemon=True).start()
        self._tick()

    # --- Tk thread ---

    def _tick(self):
        try:
            self._update()
        except Exception as e:  # never let a bad frame stop the loop
            print(f"Highlight error: {e}", flush=True)
        self.overlay.win.after(1000 // FPS, self._tick)

    def _update(self):
        now = time.monotonic()
        if now - self.checked > SETTINGS_EVERY:
            self.enabled = speak.load_state().get("highlight", True)
            self.checked = now
        player, overlay = self.player, self.overlay
        reply = player.current
        job = None
        if self.enabled and reply and reply.window and player.status()[0] in ("speaking", "paused"):
            index = reply.index
            job = (reply.window, reply, index, sentence_at(reply.chunks[index][1], player.chunk_progress()))
        if job != self.job:
            self.job = job
            self.wake.set()
        found = self.found
        # Until the worker has found a new sentence, the previous one of the same reply stays up
        boxes = found[1] if job and found and found[0][:2] == job[:2] else None
        rect = windows.frame_rect(job[0]) if boxes else None
        if rect:
            left, top = rect[:2]
            x0 = int(min(b[0] for b in boxes) - self.pad_x) - 1
            y0 = int(min(b[1] for b in boxes) - self.pad_y) - 1
            x1 = int(max(b[2] for b in boxes) + self.pad_x) + 2
            y1 = int(max(b[3] for b in boxes) + self.pad_y) + 2
            local = tuple((l - self.pad_x - x0, t - self.pad_y - y0, r + self.pad_x - x0, b + self.pad_y - y0)
                          for l, t, r, b in boxes)
            overlay.alpha = 255
            overlay.show(job[0], left + x0, top + y0, x1 - x0, y1 - y0, local,
                         lambda pixels: paint_boxes(pixels, local, self.radius, COLOR, OPACITY))
        elif overlay.shown:
            overlay.alpha -= 255 / (FADE * FPS)
            if overlay.alpha <= 0:
                overlay.hide()
            else:
                overlay.present()

    # --- worker thread ---

    def _work(self):
        try:
            screen = ScreenText()
        except Exception as e:
            print(f"Highlight unavailable: {e}", flush=True)
            return
        last_error = None
        while True:
            self.wake.wait(POLL)
            self.wake.clear()
            job = self.job
            if not job:
                self.found = None
                continue
            window, reply, index, (first, last) = job
            boxes = None
            try:
                rect = windows.frame_rect(window)
                rects = screen.find(window, reply.chunks[index][1], first, last) if rect else []
                if rects:
                    boxes = [(l - rect[0], t - rect[1], r - rect[0], b - rect[1]) for l, t, r, b in rects]
                last_error = None
            except Exception as e:
                if str(e) != last_error:
                    print(f"Highlight lookup failed: {e}", flush=True)
                    last_error = str(e)
            self.found = (job, boxes)
