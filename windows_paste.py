"""Paste compressed files into the foreground Codex window after an explicit hotkey."""

from ctypes import Structure, Union, WinDLL, WinError, byref, create_unicode_buffer, get_last_error, sizeof
from ctypes import wintypes
from pathlib import Path
from time import monotonic, sleep

from windows_clipboard import sequence, temporary_files


class LastInput(Structure):
    _fields_ = [("size", wintypes.UINT), ("tick", wintypes.DWORD)]


class KeyboardInput(Structure):
    _fields_ = [
        ("key", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("extra", wintypes.WPARAM),
    ]


class MouseInput(Structure):
    _fields_ = [
        ("x", wintypes.LONG), ("y", wintypes.LONG), ("data", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", wintypes.WPARAM),
    ]


class InputData(Union):
    _fields_ = [("keyboard", KeyboardInput), ("mouse", MouseInput)]


class Input(Structure):
    _fields_ = [("kind", wintypes.DWORD), ("data", InputData)]


user = WinDLL("user32", use_last_error=True)
core = WinDLL("kernel32", use_last_error=True)
user.GetForegroundWindow.restype = wintypes.HWND
user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
user.GetLastInputInfo.argtypes = [wintypes.LPVOID]
user.GetAsyncKeyState.argtypes = [wintypes.INT]
user.GetAsyncKeyState.restype = wintypes.SHORT
user.SendInput.argtypes = [wintypes.UINT, wintypes.LPVOID, wintypes.INT]
user.SendInput.restype = wintypes.UINT
core.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
core.OpenProcess.restype = wintypes.HANDLE
core.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPDWORD]
core.CloseHandle.argtypes = [wintypes.HANDLE]

VK_CONTROL = 0x11
SHORTCUT_KEYS = (0x10, VK_CONTROL, 0x12, 0x5B, 0x5C, ord("I"))
PASTE_KEYS = (0x10, VK_CONTROL, 0x12, 0x5B, 0x5C, ord("V"))
PASTE_MARKER = 0x43445049


def foreground_codex() -> int | None:
    window = user.GetForegroundWindow()
    return window if is_codex_window(window) else None


def is_codex_window(window: int | None) -> bool:
    return window_executable(window) in {"chatgpt.exe", "codex.exe"}


def window_executable(window: int | None) -> str:
    if not window:
        return ""
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(window, byref(pid))
    process = core.OpenProcess(0x1000, False, pid)
    if not process:
        return ""
    try:
        path = create_unicode_buffer(32768)
        capacity = wintypes.DWORD(len(path))
        if core.QueryFullProcessImageNameW(process, 0, path, byref(capacity)):
            return Path(path.value).name.lower()
    finally:
        core.CloseHandle(process)
    return ""


def wait_for_shortcut_release() -> bool:
    deadline = monotonic() + 2
    while monotonic() < deadline:
        if not shortcut_keys_down():
            return True
        sleep(0.02)
    return False


def last_input() -> int:
    state = LastInput(sizeof(LastInput))
    if not user.GetLastInputInfo(byref(state)):
        raise WinError(get_last_error())
    return state.tick


def shortcut_keys_down() -> bool:
    return any(user.GetAsyncKeyState(key) & 0x8000 for key in SHORTCUT_KEYS)


def press_paste(marker: int = PASTE_MARKER) -> None:
    key_up = 0x0002
    events = (Input * 4)(
        Input(1, InputData(keyboard=KeyboardInput(VK_CONTROL, 0, 0, 0, marker))),
        Input(1, InputData(keyboard=KeyboardInput(ord("V"), 0, 0, 0, marker))),
        Input(1, InputData(keyboard=KeyboardInput(ord("V"), 0, key_up, 0, marker))),
        Input(1, InputData(keyboard=KeyboardInput(VK_CONTROL, 0, key_up, 0, marker))),
    )
    if user.SendInput(len(events), events, sizeof(Input)) != len(events):
        raise OSError("Windows 未能将粘贴快捷键交给 Codex。")


def paste_files(
    owner: int, target: int, gesture: int, clipboard: int, files: tuple[Path, ...], cancelled=None
) -> bool:
    if cancelled is not None and cancelled.is_set():
        return False
    if user.GetForegroundWindow() != target or last_input() != gesture or sequence() != clipboard:
        return False
    with temporary_files(owner, files, clipboard):
        if cancelled is not None and cancelled.is_set():
            return False
        if user.GetForegroundWindow() != target or last_input() != gesture or any(
            user.GetAsyncKeyState(key) & 0x8000 for key in (*PASTE_KEYS, ord("I"))
        ):
            return False
        press_paste()
        # Chromium receives the keystroke asynchronously and then reads the file paths.
        sleep(0.85)
    return True
