"""Background speech server: keeps Kokoro loaded and shows a tray LED with playback controls.

Started by `/speak on` or by the Stop hook when it isn't running; stopped by `/speak off`, the
tray panel's Quit, or after `idle_minutes` without speech (the next reply starts it again). Listens on localhost only. Protocol: one JSON line
per connection, e.g.
    {"cmd": "speak", "text": "...", "session": "...", "label": "...", "project": "...", "agent": "...",
     "window": 123} | {"cmd": "stop"} | {"cmd": "pause"} | {"cmd": "ping"} | {"cmd": "shutdown"}
Everything but "text" is optional (see player.SOURCE_KEYS). Every reply queues up behind what is already playing or
waiting, whichever session sent it (see Player.say).
"""
import os
import ctypes
import sys
import json
import socket
import tempfile
import threading
import subprocess
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import speak

HOST, PORT = "127.0.0.1", 47811
IDLE_CHECK = 30  # seconds between idle checks
LOG_FILE = os.path.join(speak.SKILL_DIR, "server.log")


class SpeechServer:
    def __init__(self, sock):
        from player import Player
        from tray import Tray
        from ui import UI
        self.sock = sock
        self.player = Player()
        self.tray = Tray(self.player, self.quit)
        self.tray.icon.on_click = UI(self.player, self.tray, self.quit).toggle_panel

    def quit(self):
        self.player.shutdown()
        self.tray.stop()

    def handle(self, conn):
        with conn:
            try:
                request = json.loads(conn.makefile("r", encoding="utf-8").readline())
            except ValueError:
                return
            cmd = request.get("cmd")
            if cmd == "speak" and request.get("text"):
                self.player.say(request["text"], request)
            elif cmd == "stop":
                self.player.stop()
            elif cmd == "pause":
                self.player.toggle_pause()
            conn.sendall(b"ok\n")
            if cmd == "shutdown":
                self.quit()

    def accept_loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def load_models(self, first):
        try:
            speak.load_kokoro()
        except Exception as e:
            print(f"Kokoro preload failed (other engines still work): {e}", flush=True)
        self.player.loaded.set()
        if first:
            self.player.say(first["text"], first)

    def idle_watch(self):
        """Quit after idle_minutes (state, default 20, 0 = never) with nothing playing, paused or
        queued, so the voice model doesn't hold RAM all day; the next reply restarts the server."""
        last_busy = time.monotonic()
        while True:
            time.sleep(IDLE_CHECK)
            state, queued, _ = self.player.status()
            now = time.monotonic()
            if state != "idle" or queued:
                last_busy = now
                continue
            limit = speak.load_state().get("idle_minutes", 20)
            if limit and now - last_busy > limit * 60:
                print(f"Idle for {limit} min, shutting down", flush=True)
                self.quit()
                return

    def serve(self, first=None):
        threading.Thread(target=self.accept_loop, daemon=True).start()
        threading.Thread(target=self.idle_watch, daemon=True).start()
        threading.Thread(target=self.load_models, args=(first,), daemon=True).start()
        self.tray.run()  # main thread; returns after Quit or a shutdown command
        self.sock.close()


def request(payload, timeout=1.0):
    """Send one command to the server; returns False if it isn't running."""
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as conn:
            conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            return conn.makefile("r").readline().strip() == "ok"
    except OSError:
        return False


def start_detached(text=None, source=None):
    """Launch the server as a hidden background process that outlives Claude Code.

    `text` is handed over in a temp file (as a speak request) and spoken once the voice model is loaded.
    """
    args = [os.path.join(os.path.dirname(sys.executable), "pythonw.exe"), os.path.abspath(__file__)]
    if text:
        fd, path = tempfile.mkstemp(prefix="speak_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"cmd": "speak", "text": text, **(source or {})}, f)
        args.append(path)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:  # parent job doesn't allow breakaway
        subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    # pythonw has no console; keep errors in a small log instead
    sys.stdout = sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    first = None
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            first = json.load(f)
        os.remove(sys.argv[1])
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, PORT))
    except OSError:  # already running (e.g. two replies raced to start it): hand the text over
        if first:
            request(first, timeout=5)
        return
    sock.listen()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp panel and icon on scaled displays
    except OSError:
        pass
    SpeechServer(sock).serve(first)


if __name__ == "__main__":
    main()
