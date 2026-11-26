"""Receive Explorer file drops over Codex and replay them through its native drop target."""

import struct
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from ctypes import WinDLL, wintypes
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import pythoncom
import win32api
import win32con
import win32gui
from win32com.server.util import wrap
from win32com.shell import shell, shellcon

from preflight import Source, image_file_extensions, prepare
from windows_paste import is_codex_window, user, window_executable


CF_HDROP = (win32con.CF_HDROP, None, pythoncom.DVASPECT_CONTENT, -1, pythoncom.TYMED_HGLOBAL)
WINDOW_STYLE = 0x08000000 | win32con.WS_EX_LAYERED | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TOPMOST
DRAG_DROP = 0x00040100
DRAG_CANCEL = 0x00040101
DRAG_DEFAULT_CURSOR = 0x00040102
POINTER_TOLERANCE = 4
DROP_ACCEPT_TIMEOUT = 0.8
ole = WinDLL("ole32")
user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, *([wintypes.INT] * 4), wintypes.UINT]
user.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def window_at(point: tuple[int, int]) -> int:
    child = win32gui.WindowFromPoint(point)
    return win32gui.GetAncestor(child, win32con.GA_ROOT) if child else 0


def client_rectangle(window: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = win32gui.GetClientRect(window)
    x, y = win32gui.ClientToScreen(window, (left, top))
    return x, y, right - left, bottom - top


def dropped_paths(data) -> tuple[Path, ...]:
    raw = data.GetData(CF_HDROP).data
    if len(raw) < 20:
        raise ValueError("拖入的数据没有有效的文件列表。")
    offset, _, _, _, wide = struct.unpack_from("<IiiII", raw)
    if not 20 <= offset < len(raw):
        raise ValueError("拖入的数据没有有效的文件列表。")
    names = raw[offset:].decode("utf-16-le" if wide else "mbcs").split("\0\0", 1)[0].split("\0")
    return tuple(Path(name) for name in names if name)


def compress_drop(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    extensions = image_file_extensions()
    image_indexes = [
        index for index, path in enumerate(paths)
        if path.suffix.lower() in extensions and path.is_file()
    ]
    if not image_indexes:
        return paths
    sources = tuple(Source(paths[index].name, paths[index]) for index in image_indexes)
    files = list(paths)
    for index, attachment in zip(image_indexes, prepare(sources).files, strict=True):
        files[index] = attachment
    return tuple(files)


def shell_file_data(paths: tuple[Path, ...]):
    desktop = shell.SHGetSpecialFolderLocation(0, shellcon.CSIDL_DESKTOP)
    identifiers = [shell.SHParseDisplayName(str(path), 0)[0] for path in paths]
    return shell.SHCreateDataObject(desktop, identifiers, None, pythoncom.IID_IDataObject)


def wake_drag_loop(thread: int, point: tuple[int, int]) -> bool:
    x, y = point
    coordinates = (x & 0xffff) | ((y & 0xffff) << 16)
    # Wake this thread's OLE modal loop even when the physical mouse stays still.
    return bool(user.PostThreadMessageW(thread, win32con.WM_MOUSEMOVE, 0, coordinates))


def pump_drag_loop(done: Event, thread: int, point: tuple[int, int]):
    while not done.wait(0.025):
        wake_drag_loop(thread, point)


@dataclass(frozen=True)
class DropLocation:
    window: int
    point: tuple[int, int]
    foreground: int


class NativeDropSource:
    _public_methods_ = ["QueryContinueDrag", "GiveFeedback"]
    _com_interfaces_ = [pythoncom.IID_IDropSource]

    def __init__(self, location: DropLocation):
        self.location = location
        self.accepted = False
        self.deadline = monotonic() + DROP_ACCEPT_TIMEOUT

    def QueryContinueDrag(self, escaped, keys):
        point = win32gui.GetCursorPos()
        x, y = self.location.point
        if escaped or max(abs(point[0] - x), abs(point[1] - y)) > POINTER_TOLERANCE:
            return DRAG_CANCEL
        if window_at(point) != self.location.window:
            return DRAG_CANCEL
        if self.accepted:
            return DRAG_DROP
        return DRAG_CANCEL if monotonic() >= self.deadline else 0

    def GiveFeedback(self, effect):
        self.accepted = bool(effect & shellcon.DROPEFFECT_COPY)
        return DRAG_DEFAULT_CURSOR


def replay_files(location: DropLocation, paths: tuple[Path, ...]) -> bool:
    if win32gui.GetForegroundWindow() not in {location.foreground, location.window}:
        return False
    if user.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000 or window_at(location.point) != location.window:
        return False
    data = shell_file_data(paths)
    source = wrap(NativeDropSource(location), pythoncom.IID_IDropSource)
    pointer = win32gui.GetCursorPos()
    if pointer != location.point:
        win32api.SetCursorPos(location.point)
    done = Event()
    thread = win32api.GetCurrentThreadId()
    pump = Thread(target=pump_drag_loop, args=(done, thread, location.point), daemon=True)
    try:
        if window_at(location.point) != location.window or not wake_drag_loop(thread, location.point):
            return False
        pump.start()
        return pythoncom.DoDragDrop(data, source, shellcon.DROPEFFECT_COPY) == shellcon.DROPEFFECT_COPY
    finally:
        done.set()
        if pointer != location.point and win32gui.GetCursorPos() == location.point:
            win32api.SetCursorPos(pointer)


class CaptureTarget:
    _public_methods_ = ["DragEnter", "DragOver", "DragLeave", "Drop"]
    _com_interfaces_ = [pythoncom.IID_IDropTarget]

    def __init__(self, layer):
        self.layer = layer
        self.accepts_files = False

    def DragEnter(self, data, keys, point, effects):
        try:
            data.QueryGetData(CF_HDROP)
            self.accepts_files = True
        except pythoncom.com_error:
            self.accepts_files = False
            self.layer.skip_drag = True
            self.layer.hide()
        return self.DragOver(keys, point, effects)

    def DragOver(self, keys, point, effects):
        return effects & shellcon.DROPEFFECT_COPY if self.accepts_files and self.layer.target else shellcon.DROPEFFECT_NONE

    def DragLeave(self):
        self.accepts_files = False

    def Drop(self, data, keys, point, effects):
        if self.DragOver(keys, point, effects):
            try:
                paths = dropped_paths(data)
                if paths and self.layer.receive(paths, point):
                    return shellcon.DROPEFFECT_COPY
            except (ValueError, OSError, pythoncom.com_error, win32gui.error) as error:
                self.layer.record_error(str(error))
            finally:
                self.layer.hide()
        return shellcon.DROPEFFECT_NONE


class ExplorerDropLayer:
    def __init__(self, report_error):
        self.report_error = report_error
        self.error = None
        self.target = None
        self.rectangle = None
        self.press_start = None
        self.button_down = False
        self.released_at = None
        self.skip_drag = False
        self.jobs = deque()
        self.replaying = False
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.instance = win32api.GetModuleHandle(None)
        self.window = None
        self.registered = False
        self.class_name = "CodexImagePrepExplorerDrop"
        self.class_atom = None

        pythoncom.OleInitialize()
        try:
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = self.instance
            window_class.lpszClassName = self.class_name
            window_class.lpfnWndProc = win32gui.DefWindowProc
            window_class.hbrBackground = win32con.COLOR_WINDOW + 1
            self.class_atom = win32gui.RegisterClass(window_class)
            self.window = win32gui.CreateWindowEx(
                WINDOW_STYLE, self.class_atom, "", win32con.WS_POPUP, 0, 0, 1, 1,
                0, 0, self.instance, None
            )
            win32gui.SetLayeredWindowAttributes(self.window, 0, 1, win32con.LWA_ALPHA)
            self.drop_target = wrap(CaptureTarget(self), pythoncom.IID_IDropTarget)
            pythoncom.RegisterDragDrop(self.window, self.drop_target)
            self.registered = True
        except BaseException:
            self.close()
            raise

    def hide(self):
        if self.target:
            win32gui.ShowWindow(self.window, win32con.SW_HIDE)
            self.target = None
            self.rectangle = None

    def show(self, target: int):
        rectangle = client_rectangle(target)
        if self.target == target and self.rectangle == rectangle:
            return
        previous = win32gui.GetWindow(target, win32con.GW_HWNDPREV)
        if previous == self.window:
            previous = win32gui.GetWindow(self.window, win32con.GW_HWNDPREV)
        if not previous:
            topmost = win32gui.GetWindowLong(target, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST
            previous = win32con.HWND_TOPMOST if topmost else win32con.HWND_TOP
        if user.SetWindowPos(self.window, previous, *rectangle, win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW):
            self.target = target
            self.rectangle = rectangle

    def record_error(self, error: str):
        self.error = error

    def receive(self, paths: tuple[Path, ...], point: tuple[int, int]) -> bool:
        expected = self.target
        self.hide()
        if not expected or window_at(point) != expected:
            return False
        location = DropLocation(expected, point, win32gui.GetForegroundWindow())
        self.jobs.append((location, self.executor.submit(compress_drop, paths)))
        self.skip_drag = True
        return True

    def tick(self):
        if self.replaying:
            return
        pressed = bool(user.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
        self.track_gesture(pressed)
        if self.jobs and not pressed and not self.target and self.jobs[0][1].done():
            location, completed = self.jobs.popleft()
            try:
                self.replaying = True
                if not replay_files(location, completed.result()):
                    self.record_error("原落点已不可用，请重新拖入图片。")
            except (OSError, ValueError, RuntimeError, pythoncom.com_error, win32gui.error) as error:
                self.record_error(str(error))
            finally:
                self.replaying = False
        if self.error:
            message, self.error = self.error, None
            self.report_error(message)

    def track_gesture(self, pressed: bool):
        if not pressed:
            if self.button_down:
                self.released_at = monotonic()
            self.button_down = False
            self.press_start = None
            if self.target and self.released_at is not None and monotonic() - self.released_at >= 0.15:
                self.hide()
            return

        point = win32gui.GetCursorPos()
        active = window_at(point)
        if not self.button_down:
            self.button_down = True
            self.skip_drag = False
            self.released_at = None
            self.hide()
            self.press_start = point if window_executable(active) == "explorer.exe" else None
            return
        if self.skip_drag or self.press_start is None:
            return
        if max(abs(point[0] - self.press_start[0]), abs(point[1] - self.press_start[1])) < 8:
            return
        target = self.target if active == self.window else active
        if target and is_codex_window(target):
            try:
                self.show(target)
            except win32gui.error:
                self.hide()
        else:
            self.hide()

    def close(self):
        self.hide()
        if self.registered:
            pythoncom.RevokeDragDrop(self.window)
        if self.window:
            win32gui.DestroyWindow(self.window)
        if self.class_atom:
            win32gui.UnregisterClass(self.class_atom, self.instance)
        self.executor.shutdown(wait=True, cancel_futures=True)
        ole.OleUninitialize()
