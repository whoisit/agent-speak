"""Tray icon for the speech server: a blue LED that follows the voice.

Left click opens the control panel (ui.py); right click opens Pause / Skip paragraph / Next reply /
Stop and clear / Quit.
"""
import math
import ctypes
import time
import threading

import pystray
from PIL import Image, ImageDraw

LEVELS = 16
DIM, BRIGHT, GREY = (20, 40, 90), (80, 170, 255), (110, 110, 120)
IDLE_LEVEL = 3
SPEAKING_FPS, PAUSED_FPS, IDLE_FPS = 12, 5, 4
RELEASE = 0.15          # seconds for the glow to fall back after a loud syllable
FULL_SCALE_RMS = 0.15   # speech RMS that lights the LED fully
TRAY_ID = 0x5EA4        # fixed so Windows remembers the icon's taskbar visibility


def disk(color, size=64):
    """Anti-aliased filled circle (drawn at 4x and scaled down)."""
    big = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    ImageDraw.Draw(big).ellipse((8, 8, size * 4 - 8, size * 4 - 8), fill=color + (255,))
    return big.resize((size, size), Image.LANCZOS)


class LedIcon(pystray.Icon):
    """pystray icon that keeps one Windows icon handle per image.

    Stock pystray writes a temp .ico and reloads it on every change; with a fixed set of
    LED images each frame is then just a handle swap. Relies on pystray's win32 internals.
    """

    def __init__(self, *args, **kwargs):
        self._handles = {}
        self.on_click = None
        super().__init__(*args, **kwargs)

    def __call__(self):
        # Left click: pystray would run the menu's default item; we open the panel instead
        if self.on_click:
            self.on_click()

    def _assert_icon_handle(self):
        if not self._icon_handle:
            key = id(self.icon)
            if key not in self._handles:
                super()._assert_icon_handle()
                self._handles[key] = self._icon_handle
            self._icon_handle = self._handles[key]

    def _release_icon(self):
        self._icon_handle = None  # cached handles live as long as the process

    def _message(self, code, flags, **kwargs):
        # Windows remembers "show on taskbar" per executable + icon ID; stock pystray uses id(self),
        # which changes every launch, so the icon would fall back into the overflow menu each time
        from pystray._util import win32
        win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=self._hwnd, hID=TRAY_ID, uFlags=flags, **kwargs))


class Tray:
    def __init__(self, player, on_quit):
        self.player = player
        self.on_quit = on_quit
        self.colors = [tuple(round(d + (b - d) * i / (LEVELS - 1)) for d, b in zip(DIM, BRIGHT))
                       for i in range(LEVELS)]
        self.levels = [disk(c) for c in self.colors]
        self.grey = disk(GREY)
        self.color = GREY  # current LED color, mirrored by the panel
        self.brightness = 0.0  # current LED level 0..1, mirrored by the window glow
        item = pystray.MenuItem
        speaking = lambda _: self.player.current is not None
        menu = pystray.Menu(
            item(lambda _: "Resume" if self.player.paused else "Pause",
                 lambda: self.player.toggle_pause()),
            item("Skip paragraph", lambda: self.player.skip_paragraph(), enabled=speaking),
            item("Next reply", lambda: self.player.next_reply(), enabled=speaking),
            item("Stop and clear queue", lambda: self.player.stop(),
                 enabled=lambda _: speaking(_) or bool(self.player.queue)),
            pystray.Menu.SEPARATOR,
            item("Quit (unloads the voice model)", lambda: self.on_quit()),
        )
        self.icon = LedIcon("speak", self.grey, "Speak: loading voice model", menu)

    def run(self):
        """Blocks until stop() is called."""
        self.icon.run(setup=self._setup)

    def stop(self):
        self.icon.stop()

    def _setup(self, icon):
        icon.visible = True
        threading.Thread(target=self._animate, daemon=True).start()

    def _animate(self):
        glow, shown, last_status = 0.0, None, None
        while self.icon.visible:
            state, queued, source = self.player.status()
            if state == "speaking":
                fps = SPEAKING_FPS
                level = min(1.0, math.sqrt(self.player.read_level() / FULL_SCALE_RMS))
                glow = max(level, glow * math.exp(-1 / (fps * RELEASE)))  # instant attack, smooth release
                index = round(glow * (LEVELS - 1))
            elif state == "paused":
                fps = PAUSED_FPS
                breath = (1 - math.cos(time.monotonic() * math.pi)) / 2  # 2 s cycle
                index = 1 + round(breath * 6)
            else:
                fps, glow = IDLE_FPS, 0.0
                index = None if state == "loading" else IDLE_LEVEL
            image = self.grey if index is None else self.levels[index]
            self.color = GREY if index is None else self.colors[index]
            self.brightness = 0.0 if index is None else index / (LEVELS - 1)
            if image is not shown:
                self.icon.icon = shown = image
            if (state, queued, source) != last_status:
                last_status = (state, queued, source)
                label = {"loading": "loading voice model", "idle": "ready", "speaking": "speaking",
                         "paused": "paused"}[state]
                self.icon.title = (f"Speak: {label}" + (f" · {source}" if source else "")
                                   + (f" ({queued} queued)" if queued else ""))
                self.icon.update_menu()
            time.sleep(1 / fps)
