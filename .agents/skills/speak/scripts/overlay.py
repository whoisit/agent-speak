"""Click-through windows drawn on top of another app's window: the glow and the reading highlight.

Each is a borderless Tk window whose pixels carry their own transparency (a per-pixel alpha layered
window). It sits just above its target window in the z-order, so windows in front of the target
still cover it. It ignores the mouse and never takes focus. Windows keeps its own copy of the
pixels, so moving the overlay or changing its opacity doesn't send them again. They're only
repainted when what they show changes.
"""
import ctypes
import tkinter as tk
from ctypes import wintypes as w

import numpy as np

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


def fill(pixels, color, alpha):
    """Color pixels (BGRA, premultiplied) with an RGB color at the given 0..1 opacity per pixel."""
    for channel, value in enumerate(color[::-1]):
        pixels[..., channel] = np.round(value * alpha)
    pixels[..., 3] = np.round(255 * alpha)


class Overlay:
    hwnds = set()  # every overlay, so several can sit above the same window without fighting over the spot

    def __init__(self, root):
        self.win = win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.update_idletasks()
        self.hwnd = int(win.wm_frame(), 16)
        Overlay.hwnds.add(self.hwnd)
        self._click_through()
        self.scale = win.winfo_fpixels("1i") / 96
        self.shown = False
        self.key = None    # what the pixels Windows holds were painted for
        self.alpha = 0.0   # opacity of the whole overlay, 0..255

    def show(self, target, x, y, width, height, key, paint):
        """Place the overlay at (x, y) with this size, just above the target window.

        paint(pixels) fills a zeroed (height, width, 4) BGRA array, premultiplied by alpha. It's only
        called when the size or `key` (whatever the picture depends on) changed since the last time.
        """
        self.win.geometry(f"{width}x{height}+{x}+{y}")  # keep Tk's idea of the window in step
        if not self.shown:
            self.win.deiconify()
            self._click_through()  # Tk may rebuild the window's styles when mapping it
            self.shown = True
            self.key = None        # the pixels have to be handed over again after mapping
        key = (width, height, key)
        if key != self.key:
            self._render(x, y, width, height, paint)
            self.key = key
        else:
            self.present((x, y))
        self._stack_above(target)

    def present(self, pos=None):
        """Move the overlay and set its opacity, reusing the pixels Windows already holds."""
        point = w.POINT(*pos) if pos else None
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(point) if point else None, None,
                                   None, None, 0, ctypes.byref(self._blend()), ULW_ALPHA)

    def hide(self):
        self.win.withdraw()
        self.shown = False

    def _click_through(self):
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

    def _stack_above(self, target):
        """Directly above the target, below whatever is above it. Other overlays may sit between the target and this one."""
        above = user32.GetWindow(w.HWND(target), GW_HWNDPREV)
        while above in Overlay.hwnds:
            if above == self.hwnd:
                return
            above = user32.GetWindow(w.HWND(above), GW_HWNDPREV)
        user32.SetWindowPos(self.hwnd, above or HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _blend(self):
        return BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(self.alpha))), AC_SRC_ALPHA)

    def _render(self, x, y, width, height, paint):
        """Paint into a 32-bit bitmap and hand it to Windows, which keeps its own copy."""
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
            paint(pixels)
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
