"""Glowing outline around the window of the session that is speaking, pulsing with the tray LED.

A borderless Tk window sits just above the target window in the z-order, sized to its visible
frame plus a margin. Its middle is a color key (fully transparent), the outline fades from the
LED's bright blue at the window edge to dark outside, and the whole overlay's opacity follows the
LED. It ignores the mouse and never takes focus. Runs on the UI's Tk thread.
"""
import ctypes
import time
import tkinter as tk
from ctypes import wintypes as w

import speak
import windows

KEY = "#010203"                       # transparent color; never drawn otherwise
KEY_REF = 0x030201                    # same color as a Win32 COLORREF (0x00BBGGRR)
BRIGHT, DARK = (80, 170, 255), (10, 25, 60)
OUTSIDE, INSIDE = 9, 3                # outline width outside / inside the window edge, at 96 dpi
MIN_ALPHA, MAX_ALPHA = 70, 255        # overlay opacity at LED level 0 / 1
FADE = 0.25                           # seconds to fade out after the reply ends
FPS = 25
SETTINGS_EVERY = 1.0                  # seconds between re-reading the glow on/off setting

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
GW_HWNDPREV = 3
HWND_TOP = 0

user32 = ctypes.WinDLL("user32")
user32.GetWindow.restype = w.HWND
user32.GetWindowLongW.restype = ctypes.c_long


def blend(a, b, t):
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Glow:
    def __init__(self, root, player, tray):
        self.player, self.tray = player, tray
        self.win = win = tk.Toplevel(root, bg=KEY)
        win.withdraw()
        win.overrideredirect(True)
        self.canvas = tk.Canvas(win, bg=KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        win.update_idletasks()
        self.hwnd = int(win.wm_frame(), 16)
        self._click_through()
        k = win.winfo_fpixels("1i") / 96
        self.outside, self.inside = round(OUTSIDE * k), round(INSIDE * k)
        self.size = None
        self.shown = False
        self.alpha = 0.0
        self.enabled, self.checked = True, 0.0
        self._tick()

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
            self.enabled, self.checked = speak.load_state().get("glow", True), now
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
        else:
            return
        user32.SetLayeredWindowAttributes(self.hwnd, KEY_REF, int(self.alpha), LWA_COLORKEY | LWA_ALPHA)

    def _place(self, target, rect):
        left, top, right, bottom = rect
        out = self.outside
        width, height = right - left + 2 * out, bottom - top + 2 * out
        if (width, height) != self.size:
            self.size = (width, height)
            self._draw(width, height)
        self.win.geometry(f"{width}x{height}+{left - out}+{top - out}")
        if not self.shown:
            self.win.deiconify()
            self._click_through()  # Tk may rebuild the window's styles when mapping it
            self.shown = True
        # Directly above the target: below whatever is above it, so windows in front still cover the glow
        above = user32.GetWindow(w.HWND(target), GW_HWNDPREV)
        if above == self.hwnd:
            return
        user32.SetWindowPos(self.hwnd, above or HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _draw(self, width, height):
        """1 px rectangles from far outside (dark) to the window edge and just inside it (bright)."""
        canvas, out = self.canvas, self.outside
        canvas.delete("all")
        for d in range(out + self.inside):  # d = distance in from the overlay's outer edge
            t = min(d / out, 1.0) ** 2      # brighten quickly near the window edge
            canvas.create_rectangle(d, d, width - 1 - d, height - 1 - d, outline=blend(DARK, BRIGHT, t))
