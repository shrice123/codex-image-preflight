"""Prepare image pastes and Explorer drops for the Windows Codex app."""

import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from ctypes import POINTER, Structure, WINFUNCTYPE, WinError, cast, get_last_error, set_last_error, wintypes
from dataclasses import dataclass
from threading import Event
from time import monotonic
from tkinter import messagebox

from PIL import ImageGrab

from preflight import clipboard_sources, prepare
from windows_clipboard import CF_HDROP, clipboard_owner, sequence, user as clipboard_api
from windows_drag import ExplorerDropLayer
from windows_paste import PASTE_KEYS, PASTE_MARKER, VK_CONTROL, core, foreground_codex, last_input
from windows_paste import paste_files, press_paste, user


READY = r"Local\CodexImagePrep.SmartPaste.Ready"
STOP = r"Local\CodexImagePrep.SmartPaste.Stop"
ERROR_ALREADY_EXISTS = 183

WH_KEYBOARD_LL = 13
VK_V = ord("V")
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x10


class KeyboardEvent(Structure):
    _fields_ = [
        ("key", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("extra", wintypes.WPARAM),
    ]


HookProc = WINFUNCTYPE(wintypes.LPARAM, wintypes.INT, wintypes.WPARAM, wintypes.LPARAM)
user.SetWindowsHookExW.argtypes = [wintypes.INT, HookProc, wintypes.HINSTANCE, wintypes.DWORD]
user.SetWindowsHookExW.restype = wintypes.HANDLE
user.CallNextHookEx.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.WPARAM, wintypes.LPARAM]
user.CallNextHookEx.restype = wintypes.LPARAM
user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
clipboard_api.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
core.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
core.GetModuleHandleW.restype = wintypes.HMODULE
core.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
core.CreateEventW.restype = wintypes.HANDLE
core.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
core.OpenEventW.restype = wintypes.HANDLE
core.SetEvent.argtypes = [wintypes.HANDLE]
core.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
core.WaitForSingleObject.restype = wintypes.DWORD


@dataclass(frozen=True)
class PasteRequest:
    window: int
    clipboard: int
    accepted: float


def key_down(key: int) -> bool:
    return bool(user.GetAsyncKeyState(key) & 0x8000)


def image_clipboard(png_format: int) -> bool:
    available = clipboard_api.IsClipboardFormatAvailable
    if available(CF_HDROP):
        return True
    # Preserve normal text and rich-text paste even if another format includes a bitmap.
    return not available(13) and any(available(fmt) for fmt in (png_format, 8, 17, 2) if fmt)


def handle_paste(owner_window: int, request: PasteRequest, gesture: int, cancelled: Event):
    def current():
        return not cancelled.is_set() and user.GetForegroundWindow() == request.window and last_input() == gesture

    if not current() or sequence() != request.clipboard:
        return
    original_owner = clipboard_owner()
    contents = ImageGrab.grabclipboard()
    sources = clipboard_sources(contents)
    clipboard = sequence()
    if not current() or clipboard_owner() != original_owner:
        return
    if not sources:
        # A file clipboard can also contain ordinary documents. Replay their paste unchanged.
        if not isinstance(contents, list):
            raise ValueError("无法读取剪贴板图片。可重新复制图片后再试。")
        if current() and sequence() == clipboard and not any(map(key_down, PASTE_KEYS)):
            press_paste()
        return
    batch = prepare(sources)
    paste_files(owner_window, request.window, gesture, clipboard, batch.files, cancelled)


class SmartPaste:
    def __init__(self, root, stop_handle):
        self.root = root
        self.stop_handle = stop_handle
        self.owner = root.winfo_id()
        self.png_format = clipboard_api.RegisterClipboardFormatW("PNG")
        self.callback = HookProc(self.on_key)
        self.hook = None
        self.drag = None
        self.pending = None
        self.suppress_v = False
        self.job = None
        self.job_target = None
        self.cancelled = Event()
        self.executor = ThreadPoolExecutor(max_workers=1)

    def install(self):
        self.drag = ExplorerDropLayer(self.report_drop_error)
        self.hook = user.SetWindowsHookExW(WH_KEYBOARD_LL, self.callback, core.GetModuleHandleW(None), 0)
        if not self.hook:
            raise WinError(get_last_error())
        self.root.after(12, self.tick)

    def report_drop_error(self, detail):
        if foreground_codex() and not self.cancelled.is_set():
            messagebox.showwarning("Codex 图片拖放", f"这批文件未能自动添加。\n\n{detail}", parent=self.root)

    def on_key(self, code, message, address):
        if code == 0:
            event = cast(address, POINTER(KeyboardEvent)).contents
            if event.key == VK_V and not (event.flags & LLKHF_INJECTED and event.extra == PASTE_MARKER):
                if message in (WM_KEYUP, WM_SYSKEYUP) and self.suppress_v:
                    self.suppress_v = False
                    return 1
                if message == WM_KEYDOWN:
                    if self.suppress_v:
                        return 1
                    if self.try_capture():
                        self.suppress_v = True
                        return 1
        return user.CallNextHookEx(None, code, message, address)

    def try_capture(self):
        if self.cancelled.is_set() or not key_down(VK_CONTROL):
            return False
        if any(key_down(key) for key in (0x10, 0x12, 0x5B, 0x5C)) or not image_clipboard(self.png_format):
            return False
        window = foreground_codex()
        if not window:
            return False
        if self.pending is None and self.job is None:
            self.pending = PasteRequest(window, sequence(), monotonic())
        return True

    def tick(self):
        if core.WaitForSingleObject(self.stop_handle, 0) == 0:
            self.cancelled.set()
            self.pending = None
            if self.job is None or self.job.done():
                self.root.quit()
                return

        if self.job is not None and self.job.done():
            job, self.job = self.job, None
            try:
                job.result()
            except (OSError, ValueError, RuntimeError) as error:
                if foreground_codex() == self.job_target and not self.cancelled.is_set():
                    messagebox.showwarning("Codex 图片粘贴", f"这批图片未能自动粘贴。\n\n{error}", parent=self.root)

        if self.pending is not None:
            if monotonic() - self.pending.accepted > 4 or user.GetForegroundWindow() != self.pending.window:
                self.pending = None
            elif not self.suppress_v and not any(map(key_down, PASTE_KEYS)):
                request, self.pending = self.pending, None
                self.job_target = request.window
                self.job = self.executor.submit(handle_paste, self.owner, request, last_input(), self.cancelled)

        if self.drag is not None and not self.cancelled.is_set():
            self.drag.tick()
        self.root.after(12, self.tick)

    def close(self):
        self.cancelled.set()
        if self.hook:
            user.UnhookWindowsHookEx(self.hook)
        if self.drag is not None:
            self.drag.close()
        self.executor.shutdown(wait=True, cancel_futures=True)


def listen(announce: bool = False, *, startup_check: bool = False):
    set_last_error(0)
    ready_handle = core.CreateEventW(None, True, False, None if startup_check else READY)
    if not ready_handle:
        raise WinError(get_last_error())
    if get_last_error() == ERROR_ALREADY_EXISTS:
        core.CloseHandle(ready_handle)
        return 0
    stop_handle = None
    root = None
    listener = None
    try:
        stop_handle = core.CreateEventW(None, True, False, None if startup_check else STOP)
        if not stop_handle:
            raise WinError(get_last_error())
        user.SetProcessDpiAwarenessContext(-4)
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        listener = SmartPaste(root, stop_handle)
        listener.install()
        if not core.SetEvent(ready_handle):
            raise WinError(get_last_error())
        if announce:
            root.after_idle(lambda: messagebox.showinfo(
                "Codex 图片压缩",
                "自动压缩已启动。\n\n在 Codex 输入框按 Ctrl+V，或从资源管理器直接拖入图片。"
                "\n\n再次打开本程序可以停止自动压缩。",
                parent=root,
            ))
        if startup_check:
            root.after(100, root.quit)
        root.mainloop()
        return 0
    finally:
        if listener is not None:
            listener.close()
        if root is not None:
            root.destroy()
        if stop_handle:
            core.CloseHandle(stop_handle)
        core.CloseHandle(ready_handle)


def control(stop: bool) -> int:
    if stop:
        handle = core.OpenEventW(0x0002, False, STOP)
        if handle:
            try:
                if not core.SetEvent(handle):
                    raise WinError(get_last_error())
                print("Stop requested.")
                return 0
            finally:
                core.CloseHandle(handle)
    else:
        handle = core.OpenEventW(0x00100000, False, READY)
        if handle:
            try:
                if core.WaitForSingleObject(handle, 0) == 0:
                    print("Smart Paste is running.")
                    return 0
            finally:
                core.CloseHandle(handle)
    print("Smart Paste is not running.")
    return 1


def launch_portable() -> int:
    if control(stop=False) != 0:
        return listen(announce=True)
    if messagebox.askyesno(
        "Codex 图片压缩", "自动压缩正在运行。是否停止？", default=messagebox.NO
    ):
        return control(stop=True)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--self-test"]:
        sys.exit(listen(startup_check=True))
    if args in (["--status"], ["--stop"]):
        sys.exit(control(stop=args == ["--stop"]))
    if args:
        sys.exit("Usage: smart_paste.py [--status | --stop]")
    if getattr(sys, "frozen", False):
        sys.exit(launch_portable())
    sys.exit(listen())
