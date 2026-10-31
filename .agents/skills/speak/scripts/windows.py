"""Find the desktop window an agent session lives in, and read where it is on screen.

Windows Terminal runs all its windows in one process, so the process tree alone can't tell them
apart. Each tab's hidden pseudo-console window is owned by the terminal window currently hosting
it, so the hook attaches to the agent's console and asks for that owner. Classic consoles are
their own visible window; anything else falls back to the nearest ancestor with a window.
"""
import os
import ctypes
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
user32.GetWindow.restype = w.HWND
user32.GetForegroundWindow.restype = w.HWND
kernel32.GetConsoleWindow.restype = w.HWND

GW_OWNER = 4
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14


class _Process(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_char * 260)]


def _parents():
    """{pid: parent pid} for every running process."""
    snap = kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    entry, parents = _Process(), {}
    entry.dwSize = ctypes.sizeof(_Process)
    ok = kernel32.Process32First(snap, ctypes.byref(entry))
    while ok:
        parents[entry.th32ProcessID] = entry.th32ParentProcessID
        ok = kernel32.Process32Next(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return parents


def _ancestors():
    parents, seen = _parents(), set()
    pid = parents.get(os.getpid())
    while pid and pid not in seen:
        seen.add(pid)
        yield pid
        pid = parents.get(pid)


def _console_window():
    """Visible window hosting the current console, if any."""
    console = kernel32.GetConsoleWindow()
    if not console:
        return None
    owner = user32.GetWindow(console, GW_OWNER)  # Windows Terminal's (zero-size) pseudo-console
    if owner:
        return owner if user32.IsWindowVisible(owner) else None
    return console if user32.IsWindowVisible(console) else None  # classic console host


def _process_window(pid):
    """A visible top-level window of pid, preferring the foreground one."""
    found = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def visit(hwnd, _):
        owner = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER) \
                and user32.GetWindowTextLengthW(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    foreground = user32.GetForegroundWindow()
    return foreground if foreground in found else (found[0] if found else None)


def session_window():
    """Handle of the window this (hook) process's agent session is shown in, or None."""
    try:
        hwnd = _console_window()
        if hwnd:
            return hwnd
        ancestors = list(_ancestors())
        for pid in ancestors:  # hooks usually run without a console: borrow the agent's
            kernel32.FreeConsole()
            if kernel32.AttachConsole(pid):
                hwnd = _console_window()
                kernel32.FreeConsole()
                if hwnd:
                    return hwnd
        for pid in ancestors:
            hwnd = _process_window(pid)
            if hwnd:
                return hwnd
    except Exception:
        pass
    return None


def frame_rect(hwnd):
    """Visible bounds (left, top, right, bottom) without the invisible resize border; None if the
    window is gone, minimized, hidden or on another virtual desktop."""
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    cloaked = w.DWORD()
    dwmapi.DwmGetWindowAttribute(w.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    if cloaked.value:
        return None
    rect = w.RECT()
    if dwmapi.DwmGetWindowAttribute(w.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                                    ctypes.byref(rect), ctypes.sizeof(rect)):
        user32.GetWindowRect(w.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom
