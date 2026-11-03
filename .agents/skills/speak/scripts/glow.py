"""Glowing outline around the window of the session that is speaking, pulsing with the tray LED.

An overlay (see overlay.py) covers the target window's visible frame plus a margin. The outline is
the LED's bright blue at the window edge and fades to fully transparent outside, with rounded
corners, and the middle is fully transparent. The whole overlay's opacity follows the LED. Runs on
the UI's Tk thread.

The ring is only rendered when the overlay's size changes; every other frame just moves it and
sets its opacity, so the animation stays cheap.
"""
import sys
import time

import numpy as np

import speak
import windows
from overlay import Overlay, fill

COLOR = (80, 170, 255)                # the LED's bright blue
INSIDE = 3                            # outline width inside the window edge, at 96 dpi
# The width outside comes from state["glow_size"] and the window's corner radius from
# state["glow_corner"] (/speak glow size|corner). For "auto": Windows 11 rounds window corners,
# Windows 10 doesn't. The glow's outer corners are always rounded, since it fades by true distance.
AUTO_CORNER = 8 if sys.getwindowsversion().build >= 22000 else 0
MIN_ALPHA, MAX_ALPHA = 70, 255        # overlay opacity at LED level 0 / 1
FADE = 0.25                           # seconds to fade out after the reply ends
FPS = 25
SETTINGS_EVERY = 1.0                  # seconds between re-reading the glow on/off setting


def ring_alpha(dist, outside, inside):
    """Opacity by signed distance to the window's edge (negative inside): transparent at `outside`
    beyond it, brightening quickly toward it, then opaque for `inside` pixels in, with an
    anti-aliased inner edge."""
    fade = np.clip((outside - dist) / outside, 0.0, 1.0) ** 2
    return fade * np.clip(dist + inside + 0.5, 0.0, 1.0)


class Glow:
    def __init__(self, root, player, tray):
        self.player, self.tray = player, tray
        self.overlay = Overlay(root)
        self.scale = self.overlay.scale
        self.inside = round(INSIDE * self.scale)
        self._apply_settings(speak.DEFAULT_STATE)
        self.checked = 0.0
        self._tick()

    def _apply_settings(self, state):
        """Glow on/off, size and corner from the settings; a new size or corner re-renders the ring."""
        self.enabled = state.get("glow", True)
        size = state.get("glow_size", speak.DEFAULT_STATE["glow_size"])
        corner = state.get("glow_corner", "auto")
        corner = AUTO_CORNER if corner == "auto" else corner
        self.outside, self.corner = max(1, round(size * self.scale)), round(corner * self.scale)

    def _tick(self):
        try:
            self._update()
        except Exception as e:  # never let a bad frame stop the animation loop
            print(f"Glow error: {e}", flush=True)
        self.overlay.win.after(1000 // FPS, self._tick)

    def _update(self):
        now = time.monotonic()
        if now - self.checked > SETTINGS_EVERY:
            self._apply_settings(speak.load_state())
            self.checked = now
        reply = self.player.current
        state = self.player.status()[0]
        target = reply.window if self.enabled and reply and state in ("speaking", "paused") else None
        rect = windows.frame_rect(target) if target else None
        overlay = self.overlay
        if rect:
            overlay.alpha = MIN_ALPHA + (MAX_ALPHA - MIN_ALPHA) * self.tray.brightness
            left, top, right, bottom = rect
            out = self.outside
            overlay.show(target, left - out, top - out, right - left + 2 * out, bottom - top + 2 * out,
                         (out, self.corner), self._paint_ring)
        elif overlay.shown:  # fade out where it was, then hide
            overlay.alpha -= MAX_ALPHA / (FADE * FPS)
            if overlay.alpha <= 0:
                overlay.hide()
            else:
                overlay.present()

    def _paint_ring(self, pixels):
        """Fill the four edge strips, coloring each pixel by its distance to the window's
        (rounded) rectangle, so the corners curve."""
        height, width = pixels.shape[:2]
        out, radius = self.outside, self.corner
        strip = out + self.inside + radius + 1  # deep enough to hold a rounded inner corner
        # Window rectangle in overlay pixels: center and half size, shrunk by the corner radius
        cx, cy = width / 2, height / 2
        hx, hy = cx - out - radius, cy - out - radius
        ys, xs = np.arange(height) + 0.5, np.arange(width) + 0.5
        regions = [(slice(0, strip), slice(None)), (slice(height - strip, None), slice(None)),
                   (slice(strip, height - strip), slice(0, strip)),
                   (slice(strip, height - strip), slice(width - strip, None))]
        for rows, cols in regions:
            qy = (np.abs(ys[rows] - cy) - hy)[:, None]
            qx = (np.abs(xs[cols] - cx) - hx)[None, :]
            if not qy.size or not qx.size:
                continue
            dist = (np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
                    + np.minimum(np.maximum(qx, qy), 0) - radius)  # rounded-rectangle distance
            fill(pixels[rows, cols], COLOR, ring_alpha(dist, out, self.inside))
