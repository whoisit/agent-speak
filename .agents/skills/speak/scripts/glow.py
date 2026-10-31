"""Glowing outline around the window of the session that is speaking, pulsing with the tray LED.

A borderless Tk window sits just above the target window in the z-order, sized to its visible
frame plus a margin. Its pixels carry their own transparency (a per-pixel alpha layered window):
the outline is the LED's bright blue at the window edge and fades to fully transparent outside,
with rounded corners, and the middle is fully transparent. The whole overlay's opacity follows the LED. It ignores the
mouse and never takes focus. Runs on the UI's Tk thread.

The ring is only rendered when the overlay's size changes; every other frame just moves it and
sets its opacity, so the animation stays cheap.
"""
import ctypes
import sys
import time
import tkinter as tk
from ctypes import wintypes as w

import numpy as np

import speak
import windows

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

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
GW_HWNDPREV = 3
HWND_TOP = 0
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA, DIB_RGB_COLORS = 0x2, 0x0, 0x1, 0


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
user32.GetWindow.restype = w.HWND
user32.GetWindowLongW.restype = ctypes.c_long
user32.UpdateLayeredWindow.argtypes = [w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE),
                                       w.HDC, ctypes.POINTER(w.POINT), w.COLORREF,
                                       ctypes.POINTER(BLENDFUNCTION), w.DWORD]
user32.UpdateLayeredWindow.restype = w.BOOL
gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.POINTER(BITMAPINFOHEADER), w.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
gdi32.CreateDIBSection.restype = w.HBITMAP
gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
gdi32.DeleteDC.argtypes = [w.HDC]


def ring_alpha(dist, outside, inside):
    """Opacity by signed distance to the window's edge (negative inside): transparent at `outside`
    beyond it, brightening quickly toward it, then opaque for `inside` pixels in, with an
    anti-aliased inner edge."""
    fade = np.clip((outside - dist) / outside, 0.0, 1.0) ** 2
    return fade * np.clip(dist + inside + 0.5, 0.0, 1.0)


class Glow:
    def __init__(self, root, player, tray):
        self.player, self.tray = player, tray
        self.win = win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.update_idletasks()
        self.hwnd = int(win.wm_frame(), 16)
        self._click_through()
        self.scale = win.winfo_fpixels("1i") / 96
        self.inside = round(INSIDE * self.scale)
        self._apply_settings(speak.DEFAULT_STATE)
        self.shown = False
        self.alpha = 0.0
        self.checked = 0.0
        self._tick()

    def _apply_settings(self, state):
        """Glow on/off, size and corner from the settings; a new size or corner re-renders the ring."""
        self.enabled = state.get("glow", True)
        size = state.get("glow_size", speak.DEFAULT_STATE["glow_size"])
        corner = state.get("glow_corner", "auto")
        corner = AUTO_CORNER if corner == "auto" else corner
        outside, corner = max(1, round(size * self.scale)), round(corner * self.scale)
        if (outside, corner) != (getattr(self, "outside", None), getattr(self, "corner", None)):
            self.outside, self.corner = outside, corner
            self.size = None

    def _click_through(self):
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    def _tick(self):
        try:
            self._update()
        except Exception as e:  # never let a bad frame stop the animation loop
            print(f"Glow error: {e}", flush=True)
        self.win.after(1000 // FPS, self._tick)

    def _update(self):
        now = time.monotonic()
        if now - self.checked > SETTINGS_EVERY:
            self._apply_settings(speak.load_state())
            self.checked = now
        reply = self.player.current
        state = self.player.status()[0]
        target = reply.window if self.enabled and reply and state in ("speaking", "paused") else None
        rect = windows.frame_rect(target) if target else None
        if rect:
            self.alpha = MIN_ALPHA + (MAX_ALPHA - MIN_ALPHA) * self.tray.brightness
            self._place(target, rect)
        elif self.shown:  # fade out where it was, then hide
            self.alpha -= MAX_ALPHA / (FADE * FPS)
            if self.alpha <= 0:
                self.win.withdraw()
                self.shown = False
                return
            self._present()

    def _place(self, target, rect):
        left, top, right, bottom = rect
        out = self.outside
        width, height = right - left + 2 * out, bottom - top + 2 * out
        x, y = left - out, top - out
        self.win.geometry(f"{width}x{height}+{x}+{y}")  # keep Tk's idea of the window in step
        if not self.shown:
            self.win.deiconify()
            self._click_through()  # Tk may rebuild the window's styles when mapping it
            self.shown = True
            self.size = None       # the ring has to be handed over again after mapping
        if (width, height) != self.size:
            self._render(x, y, width, height)
        else:
            self._present((x, y))
        # Directly above the target: below whatever is above it, so windows in front still cover the glow
        above = user32.GetWindow(w.HWND(target), GW_HWNDPREV)
        if above == self.hwnd:
            return
        user32.SetWindowPos(self.hwnd, above or HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _blend(self):
        return BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(self.alpha))), AC_SRC_ALPHA)

    def _present(self, pos=None):
        """Move the overlay and set its opacity, reusing the ring Windows already holds."""
        point = w.POINT(*pos) if pos else None
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(point) if point else None, None,
                                   None, None, 0, ctypes.byref(self._blend()), ULW_ALPHA)

    def _render(self, x, y, width, height):
        """Draw the ring into a 32-bit bitmap and hand it to Windows, which keeps its own copy."""
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0)  # top-down
        bits = ctypes.c_void_p()
        dc = gdi32.CreateCompatibleDC(None)
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap:
            gdi32.DeleteDC(dc)
            raise RuntimeError("CreateDIBSection failed")
        try:
            old = gdi32.SelectObject(dc, bitmap)
            pixels = np.ctypeslib.as_array(ctypes.cast(bits, ctypes.POINTER(ctypes.c_uint8)),
                                           (height, width, 4))
            pixels[:] = 0
            self._paint_ring(pixels, width, height)
            ok = user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(w.POINT(x, y)),
                                            ctypes.byref(w.SIZE(width, height)), dc,
                                            ctypes.byref(w.POINT(0, 0)), 0, ctypes.byref(self._blend()),
                                            ULW_ALPHA)
            gdi32.SelectObject(dc, old)
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(dc)
        if not ok:
            raise RuntimeError(f"UpdateLayeredWindow failed (error {ctypes.GetLastError()})")
        self.size = (width, height)

    def _paint_ring(self, pixels, width, height):
        """Fill the four edge strips, coloring each pixel by its distance to the window's
        (rounded) rectangle, so the corners curve."""
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
            alpha = ring_alpha(dist, out, self.inside)
            area = pixels[rows, cols]
            for channel, value in enumerate(COLOR[::-1]):  # BGRA, premultiplied by alpha
                area[..., channel] = np.round(value * alpha)
            area[..., 3] = np.round(255 * alpha)
