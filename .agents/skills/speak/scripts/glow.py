"""Glowing outline on the window of the session that is speaking, pulsing with the tray LED.

An overlay (see overlay.py) covers the target window's visible frame, plus a margin when the glow
is outside. The outline is the LED's bright blue at the window edge and fades to fully transparent
outside it (/speak glow outside) or toward the middle (/speak glow inside), with rounded corners;
the middle is fully transparent either way. The whole overlay's opacity follows the LED, scaled by
state["glow_opacity"]. Runs on the UI's Tk thread.

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
# The glow's width comes from state["glow_size"] and the window's corner radius from
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


def inner_alpha(dx, dy, size):
    """Opacity for a glow inside the window, by distance to the nearest side and to the nearest top
    or bottom edge: opaque at the edge, fading to transparent `size` pixels in. The two fades are
    combined like two layers of tint, so corners are smooth (a distance to the whole rectangle
    would leave sharp diagonal seams there)."""
    fade = lambda d: np.clip((size - d) / size, 0.0, 1.0) ** 2
    return 1 - (1 - fade(dx)) * (1 - fade(dy))


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
        """Glow on/off, side, size, opacity and corner from the settings; a new side, size or corner
        re-renders the ring."""
        self.enabled = state.get("glow", True)
        self.inner = state.get("glow_side") == "inside"
        self.opacity = min(max(state.get("glow_opacity", 100), 1), 100) / 100
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
        reply = self.player.current
        overlay = self.overlay
        if not reply and not overlay.shown:
            return
        now = time.monotonic()
        if now - self.checked > SETTINGS_EVERY:
            self._apply_settings(speak.load_state())
            self.checked = now
        state = self.player.status()[0]
        target = reply.window if self.enabled and reply and state in ("speaking", "paused") else None
        rect = windows.frame_rect(target) if target else None
        if rect:
            overlay.alpha = (MIN_ALPHA + (MAX_ALPHA - MIN_ALPHA) * self.tray.brightness) * self.opacity
            left, top, right, bottom = rect
            margin = 0 if self.inner else self.outside
            overlay.show(target, left - margin, top - margin, right - left + 2 * margin,
                         bottom - top + 2 * margin, (self.inner, self.outside, self.corner), self._paint_ring)
        elif overlay.shown:  # fade out where it was, then hide
            overlay.alpha -= MAX_ALPHA * self.opacity / (FADE * FPS)
            if overlay.alpha <= 0:
                overlay.hide()
            else:
                overlay.present()

    def _paint_ring(self, pixels):
        """Fill the four edge strips, coloring each pixel by its distance to the window's
        (rounded) rectangle, so the corners curve."""
        height, width = pixels.shape[:2]
        size, corner = self.outside, self.corner
        margin = 0 if self.inner else size  # the overlay reaches this far outside the window
        strip = margin + (size if self.inner else self.inside) + corner + 1  # holds a rounded inner corner
        cx, cy = width / 2, height / 2  # window center in overlay pixels
        ys, xs = np.arange(height) + 0.5, np.arange(width) + 0.5
        regions = [(slice(0, strip), slice(None)), (slice(height - strip, None), slice(None)),
                   (slice(strip, height - strip), slice(0, strip)),
                   (slice(strip, height - strip), slice(width - strip, None))]
        for rows, cols in regions:
            if not len(ys[rows]) or not len(xs[cols]):
                continue

            def dist(radius):
                """Signed distance to the window's rectangle with corners rounded by `radius`."""
                qy = (np.abs(ys[rows] - cy) - (cy - margin - radius))[:, None]
                qx = (np.abs(xs[cols] - cx) - (cx - margin - radius))[None, :]
                return (np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
                        + np.minimum(np.maximum(qx, qy), 0) - radius)

            if self.inner:  # faded from each edge, then cut off at the window's rounded corners
                dy = np.minimum(ys[rows], height - ys[rows])[:, None]
                dx = np.minimum(xs[cols], width - xs[cols])[None, :]
                alpha = inner_alpha(dx, dy, size) * np.clip(0.5 - dist(corner), 0.0, 1.0)
            else:
                alpha = ring_alpha(dist(corner), size, self.inside)
            fill(pixels[rows, cols], COLOR, alpha)
