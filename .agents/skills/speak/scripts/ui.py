"""Tray flyout panel and clipboard reader window, on their own Tk thread.

Panel: play/pause, next paragraph, next reply (the next queued one),
stop and clear, speed, window glow on/off, read clipboard, quit. The status line names the session speaking.
Reader: shows the clipboard text and highlights the chunk being spoken; closing it stops that reading.
"""
import ctypes
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes

import speak
from glow import Glow
from highlight import Highlight
from textclean import clean

BG, HOVER, FG, DIM_FG = "#202020", "#383838", "#f2f2f2", "#8a8a8a"
CURRENT_BG = "#1f4a80"
ICONS = ("Segoe MDL2 Assets", 15)
TEXT = ("Segoe UI", 10)
CAPTION = ("Segoe UI", 8)
PLAY, PAUSE, PARAGRAPH, NEXT, STOP = "", "", "", "", ""
PASTE, POWER, MINUS, PLUS, GLOW = "", "", "", "", ""
STATUS = {"loading": "Loading voice model", "idle": "Ready", "speaking": "Speaking", "paused": "Paused"}


def work_area():
    """Screen area not covered by the taskbar: (left, top, right, bottom)."""
    rect = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def hex_color(rgb):
    return "#%02x%02x%02x" % rgb


class UI:
    """Owns the Tk thread; other threads hand it work through a queue."""

    def __init__(self, player, tray, on_quit):
        self.player, self.tray, self.on_quit = player, tray, on_quit
        self.calls = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def toggle_panel(self):
        self.calls.put(lambda: self.panel.toggle())

    def _run(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = Panel(self)
        self.glow = Glow(self.root, self.player, self.tray)
        self.highlight = Highlight(self.root, self.player)
        self._poll()
        self.root.mainloop()

    def _poll(self):
        while not self.calls.empty():
            self.calls.get()()
        self.root.after(100, self._poll)


class Panel:
    def __init__(self, ui):
        self.ui = ui
        player = ui.player
        self.hidden_at = 0.0
        self.win = win = tk.Toplevel(ui.root, bg=BG, padx=14, pady=12,
                                     highlightthickness=1, highlightbackground="#3a3a3a")
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.bind("<FocusOut>", lambda e: self.hide())
        win.bind("<Escape>", lambda e: self.hide())

        header = tk.Frame(win, bg=BG)
        header.pack(fill="x")
        size = round(12 * win.winfo_fpixels("1i") / 96)
        self.led = tk.Canvas(header, width=size, height=size, bg=BG, highlightthickness=0)
        self.dot = self.led.create_oval(0, 0, size - 1, size - 1, outline="")
        self.led.pack(side="left")
        self.status = tk.Label(header, bg=BG, fg=FG, font=TEXT)
        self.status.pack(side="left", padx=8)

        controls = tk.Frame(win, bg=BG)
        controls.pack(pady=(12, 8))
        self.play = self.button(controls, PLAY, "Play", player.toggle_pause)
        self.button(controls, PARAGRAPH, "Paragraph", player.skip_paragraph)
        self.button(controls, NEXT, "Reply", player.next_reply)
        self.button(controls, STOP, "Stop all", player.stop)

        bottom = tk.Frame(win, bg=BG)
        bottom.pack(fill="x", pady=(4, 0))
        tk.Label(bottom, text="Speed", bg=BG, fg=DIM_FG, font=TEXT).pack(side="left")
        self.small_button(bottom, MINUS, lambda: self.change_speed(-0.1))
        self.speed = tk.Label(bottom, bg=BG, fg=FG, font=TEXT, width=5)
        self.speed.pack(side="left")
        self.small_button(bottom, PLUS, lambda: self.change_speed(0.1))
        self.small_button(bottom, POWER, self.quit, side="right")
        self.small_button(bottom, PASTE, self.read_clipboard, side="right", caption="Read clipboard")
        self.glow_label = self.small_button(bottom, GLOW, self.toggle_glow, side="right", caption="Glow")

    # --- widgets ---

    def button(self, parent, glyph, caption, action):
        frame = tk.Frame(parent, bg=BG, padx=10, pady=6, cursor="hand2")
        frame.pack(side="left", padx=2)
        icon = tk.Label(frame, text=glyph, bg=BG, fg=FG, font=ICONS)
        icon.pack()
        label = tk.Label(frame, text=caption, bg=BG, fg=DIM_FG, font=CAPTION)
        label.pack()
        self.clickable(frame, action)
        return icon, label

    def small_button(self, parent, glyph, action, side="left", caption=None):
        frame = tk.Frame(parent, bg=BG, padx=6, pady=4, cursor="hand2")
        frame.pack(side=side, padx=2)
        tk.Label(frame, text=glyph, bg=BG, fg=FG, font=("Segoe MDL2 Assets", 11)).pack(side="left")
        label = None
        if caption:
            label = tk.Label(frame, text=caption, bg=BG, fg=FG, font=CAPTION)
            label.pack(side="left", padx=(6, 0))
        self.clickable(frame, action)
        return label

    def clickable(self, frame, action):
        widgets = [frame, *frame.winfo_children()]
        paint = lambda color: [w.configure(bg=color) for w in widgets]
        for w in widgets:
            w.bind("<Button-1>", lambda e: (action(), self.refresh()))
            w.bind("<Enter>", lambda e: paint(HOVER))
            w.bind("<Leave>", lambda e: paint(BG))

    # --- behavior ---

    def toggle(self):
        if self.win.winfo_viewable():
            self.hide()
        elif time.monotonic() - self.hidden_at > 0.3:  # the tray click that just closed it by focus loss
            self.show()

    def show(self):
        state = speak.load_state()
        self.speed.configure(text=f"{state['speed']:.1f}×")
        self.show_glow(state.get("glow", True))
        self.refresh()
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        px, py = self.win.winfo_pointerxy()
        left, top, right, bottom = work_area()
        x = min(max(px - w // 2, left + 8), right - w - 8)
        y = bottom - h - 8 if py > (top + bottom) / 2 else top + 8  # taskbar at the bottom or top
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self._tick()

    def hide(self):
        if self.win.winfo_viewable():
            self.win.withdraw()
            self.hidden_at = time.monotonic()

    def refresh(self):
        state, queued, source = self.ui.player.status()
        self.status.configure(text=STATUS[state] + (f" · {source}" if source else "")
                              + (f" · {queued} queued" if queued else ""))
        speaking = state == "speaking"
        self.play[0].configure(text=PAUSE if speaking else PLAY)
        self.play[1].configure(text="Pause" if speaking else "Play")
        self.led.itemconfigure(self.dot, fill=hex_color(self.ui.tray.color))

    def _tick(self):
        if self.win.winfo_viewable():
            self.refresh()
            self.win.after(80, self._tick)

    def change_speed(self, delta):
        state = speak.load_state()
        state["speed"] = round(min(max(state["speed"] + delta, 0.5), 2.0), 2)
        speak.save_state(state)
        self.speed.configure(text=f"{state['speed']:.1f}×")

    def toggle_glow(self):
        state = speak.load_state()
        state["glow"] = not state.get("glow", True)
        speak.save_state(state)
        self.show_glow(state["glow"])

    def show_glow(self, on):
        self.glow_label.configure(text="Glow on" if on else "Glow off", fg=FG if on else DIM_FG)

    def read_clipboard(self):
        try:
            text = clean(self.win.clipboard_get())
        except tk.TclError:  # empty, or not text
            text = ""
        reply = self.ui.player.say(text, {"label": "Clipboard"}) if text else None
        if not reply:
            self.status.configure(text="Clipboard has no text")
            return
        self.hide()
        Reader(self.ui, reply)

    def quit(self):
        self.hide()
        self.ui.on_quit()


class Reader:
    """Transient window that follows along: spoken chunks grey out, the current one is highlighted."""

    def __init__(self, ui, reply):
        self.ui, self.reply = ui, reply
        self.win = win = tk.Toplevel(ui.root, bg=BG)
        win.title("Speak: clipboard, waiting its turn")  # _tick switches it once this reply plays
        k = win.winfo_fpixels("1i") / 96
        left, top, right, bottom = work_area()
        w, h = round(640 * k), round(460 * k)
        win.geometry(f"{w}x{h}+{(left + right - w) // 2}+{(top + bottom - h) // 2}")
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())

        self.text = text = tk.Text(win, wrap="word", bg=BG, fg=FG, font=("Segoe UI", 12), relief="flat",
                                   padx=24, pady=18, spacing1=2, spacing2=4, highlightthickness=0,
                                   insertwidth=0, cursor="arrow")
        text.pack(fill="both", expand=True)  # no scrollbar (native one is light); wheel scrolls, view follows
        text.tag_configure("done", foreground=DIM_FG)
        text.tag_configure("current", background=CURRENT_BG, foreground="#ffffff")

        # Character span of each chunk in the displayed text
        self.spans, offset = [], 0
        for p, para in enumerate(reply.paragraphs):
            cursor = 0
            for chunk_para, chunk in reply.chunks:
                if chunk_para != p:
                    continue
                at = para.find(chunk, cursor)
                self.spans.append((offset + at, offset + at + len(chunk)) if at >= 0 else None)
                cursor = at + len(chunk) if at >= 0 else cursor
            offset += len(para) + 2
        text.insert("1.0", "\n\n".join(reply.paragraphs))
        text.configure(state="disabled")
        self.shown = None
        win.lift()
        win.focus_force()
        self._tick()

    def _tick(self):
        if not self.win.winfo_exists():
            return
        player, reply = self.ui.player, self.reply
        if reply.cancelled and player.current is not reply:  # finished, skipped or stopped
            self._mark(len(self.spans))
            self.win.after(2000, self.win.destroy)
            return
        title = "Speak: reading clipboard" if player.current is reply else "Speak: clipboard, waiting its turn"
        if self.win.title() != title:
            self.win.title(title)
        if player.current is reply and reply.index != self.shown:
            self._mark(reply.index)
        self.win.after(100, self._tick)

    def _mark(self, index):
        self.shown = index
        text = self.text
        text.tag_remove("done", "1.0", "end")
        text.tag_remove("current", "1.0", "end")
        pos = lambda n: f"1.0 + {n} chars"
        done_end = next((s[0] for s in self.spans[index:] if s), None)
        text.tag_add("done", "1.0", pos(done_end) if done_end is not None else "end")
        span = self.spans[index] if index < len(self.spans) else None
        if span:
            text.tag_add("current", pos(span[0]), pos(span[1]))
            text.see(pos(span[1]))
            text.see(pos(span[0]))

    def close(self):
        self.ui.player.cancel(self.reply)
        self.win.destroy()
