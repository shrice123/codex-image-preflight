"""Prepare local images before Codex's native Windows file dialog opens them."""

import re
from ctypes import POINTER, WINFUNCTYPE, Structure, WinDLL, byref, c_void_p, cast, sizeof, wintypes
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import pythoncom
import win32api
import win32con
import win32gui
from win32com.shell import shell, shellcon

from preflight import image_file_extensions
from windows_drag import compress_drop
from windows_paste import is_codex_window, user


FILE_NAME = 1148
ADDRESS_BAR = 1001
SHELL_VIEW = 1121
OPEN_TITLES = {"选择文件", "选取文件", "選擇檔案", "select file", "select files", "open"}
USER_FOLDERS = (
    shell.FOLDERID_Desktop, shell.FOLDERID_Downloads,
    shell.FOLDERID_Documents, shell.FOLDERID_Pictures, shell.FOLDERID_Profile,
)
MESSAGE_TIMEOUT_MS = 150
QUOTED_NAMES = re.compile(r'(?:\s*"[^"]+")+\s*')


class GUIThreadInfo(Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("active", wintypes.HWND), ("focus", wintypes.HWND),
        ("capture", wintypes.HWND), ("menu_owner", wintypes.HWND),
        ("move_size", wintypes.HWND), ("caret", wintypes.HWND),
        ("caret_rectangle", wintypes.RECT),
    ]


class AccessibleChild(Structure):
    _fields_ = [
        ("type", wintypes.WORD), ("reserved", wintypes.WORD * 3),
        ("index", wintypes.LONG), ("padding", wintypes.LONG * 3),
    ]


oleacc = WinDLL("oleacc")
oleacc.AccessibleObjectFromPoint.argtypes = [
    wintypes.POINT, POINTER(c_void_p), POINTER(AccessibleChild),
]
user.GetGUIThreadInfo.argtypes = [wintypes.DWORD, wintypes.LPVOID]


def focus_window():
    info = GUIThreadInfo(sizeof(GUIThreadInfo))
    return info.focus if user.GetGUIThreadInfo(0, byref(info)) else None


def contains(parent, child):
    return child and (child == parent or win32gui.IsChild(parent, child))


def image_at(point, name):
    pointer = c_void_p()
    child = AccessibleChild()
    if oleacc.AccessibleObjectFromPoint(wintypes.POINT(*point), byref(pointer), byref(child)) < 0 or not pointer:
        return False
    release = WINFUNCTYPE(wintypes.ULONG, c_void_p)(cast(pointer, POINTER(POINTER(c_void_p)))[0][2])
    try:
        if child.type != pythoncom.VT_I4:
            return False
        accessible = pythoncom.ObjectFromAddress(pointer.value, pythoncom.IID_IDispatch)
        # IAccessible dispids for accRole and accName.
        role = accessible.Invoke(-5006, 0, pythoncom.DISPATCH_PROPERTYGET, True, child.index)
        if role != 0x22:  # ROLE_SYSTEM_LISTITEM
            return False
        label = accessible.Invoke(-5003, 0, pythoncom.DISPATCH_PROPERTYGET, True, child.index)
        file = Path(name)
        return isinstance(label, str) and label.casefold() in {file.name.casefold(), file.stem.casefold()}
    except (pythoncom.com_error, ValueError):
        return False
    finally:
        release(pointer)


@dataclass(frozen=True)
class Dialog:
    window: int
    button: int
    editor: int
    address: int
    view: int
    shortcut: int | None

    @classmethod
    def find(cls, window):
        if not window or win32gui.GetClassName(window) != "#32770":
            return None
        if win32gui.GetWindowText(window).strip().casefold() not in OPEN_TITLES:
            return None
        owner = win32gui.GetWindow(window, win32con.GW_OWNER)
        if not is_codex_window(owner):
            return None
        button = win32gui.GetDlgItem(window, win32con.IDOK)
        if not button or win32gui.GetClassName(button) != "Button":
            return None
        controls = {}

        def collect(child, unused):
            controls[win32gui.GetDlgCtrlID(child), win32gui.GetClassName(child)] = child

        win32gui.EnumChildWindows(window, collect, None)
        editor = controls.get((FILE_NAME, "Edit"))
        address = controls.get((ADDRESS_BAR, "ToolbarWindow32"))
        view = controls.get((SHELL_VIEW, "SHELLDLL_DefView"))
        if editor and address and view:
            match = re.search(r"&([A-Z])", win32gui.GetWindowText(button), re.I)
            shortcut = ord(match[1].upper()) if match else None
            return cls(window, button, editor, address, view, shortcut)
        return None

    def selection(self):
        buffer = bytearray(65536)
        _, length = win32gui.SendMessageTimeout(
            self.editor, win32con.WM_GETTEXT, len(buffer) // 2, buffer,
            win32con.SMTO_ABORTIFHUNG, MESSAGE_TIMEOUT_MS,
        )
        return bytes(buffer[:2 * length]).decode("utf-16-le"), win32gui.GetWindowText(self.address)

    def confirm(self, files=None):
        if files:
            value = " ".join(f'"{file}"' for file in files)
            _, written = win32gui.SendMessageTimeout(
                self.editor, win32con.WM_SETTEXT, 0, value,
                win32con.SMTO_ABORTIFHUNG, MESSAGE_TIMEOUT_MS,
            )
            if not written or self.selection()[0] != value:
                raise RuntimeError("无法将压缩副本填入文件选择窗口。")
        win32gui.PostMessage(self.button, win32con.BM_CLICK, 0, 0)


def selected_names(value):
    value = value.strip()
    if '"' not in value:
        return (value,) if value else ()
    if QUOTED_NAMES.fullmatch(value):
        return tuple(re.findall(r'"([^"]+)"', value))
    return ()


def folder_path(address):
    label = address.partition(": ")[2] or address
    directory = Path(label)
    if directory.is_absolute():
        return directory
    pythoncom.CoInitialize()
    try:
        for folder_id in USER_FOLDERS:
            try:
                path = Path(shell.SHGetKnownFolderPath(folder_id, 0, None))
                _, data = shell.SHGetFileInfo(str(path), 0, shellcon.SHGFI_DISPLAYNAME)
            except (OSError, pythoncom.com_error):
                continue
            if label.casefold() in {path.name.casefold(), data[3].casefold()}:
                return path
    finally:
        pythoncom.CoUninitialize()


def compress_selection(names, address):
    directory = folder_path(address) if any(not Path(name).is_absolute() for name in names) else None
    paths = []
    for name in names:
        path = Path(name)
        if not path.is_absolute():
            if not directory:
                raise ValueError("请在图片所在的本地文件夹中选择文件。")
            path = directory / path
        paths.append(path)
    extensions = image_file_extensions()
    if any(path.suffix.casefold() in extensions and not path.exists() for path in paths):
        raise ValueError("选中的图片已不可用，请重新选择。")
    outputs = compress_drop(tuple(paths))
    return outputs if outputs != tuple(paths) else None


class FilePicker:
    def __init__(self, executor):
        self.executor = executor
        self.foreground = None
        self.dialog = None
        self.next_probe = 0
        self.pending = None
        self.job = None
        self.pressed_key = None
        self.pressed_mouse = None
        self.view_down = None
        self.last_click = None
        self.double_click_seconds = win32gui.GetDoubleClickTime() / 1000
        self.double_click_width = win32api.GetSystemMetrics(win32con.SM_CXDOUBLECLK)
        self.double_click_height = win32api.GetSystemMetrics(win32con.SM_CYDOUBLECLK)
        self.extensions = image_file_extensions()

    def active(self):
        dialog = self.dialog
        return dialog if dialog and win32gui.GetForegroundWindow() == dialog.window else None

    def key(self, message, key):
        if self.pressed_key and key == self.pressed_key[1]:
            if message in (win32con.WM_KEYUP, win32con.WM_SYSKEYUP):
                self.pending, _ = self.pressed_key
                self.pressed_key = None
            return True
        if message not in (win32con.WM_KEYDOWN, win32con.WM_SYSKEYDOWN):
            return False
        dialog = self.active()
        if not dialog:
            return False
        if key == win32con.VK_RETURN and message == win32con.WM_KEYDOWN:
            focus = focus_window()
            if focus not in {dialog.window, dialog.editor, dialog.button} and not contains(dialog.view, focus):
                return False
            if any(user.GetAsyncKeyState(k) & 0x8000 for k in (win32con.VK_CONTROL, win32con.VK_SHIFT)):
                return False
        elif key != dialog.shortcut or message != win32con.WM_SYSKEYDOWN:
            return False
        self.pressed_key = dialog, key
        return True

    def mouse(self, message, point):
        if message == win32con.WM_LBUTTONUP and self.pressed_mouse:
            dialog, require_button = self.pressed_mouse
            self.pressed_mouse = None
            if self.active() == dialog:
                hit = win32gui.WindowFromPoint(point)
                if (hit == dialog.button if require_button else contains(dialog.view, hit)):
                    self.pending = dialog
            return True
        dialog = self.active()
        if not dialog:
            return False
        hit = win32gui.WindowFromPoint(point)
        if message == win32con.WM_LBUTTONDOWN and hit == dialog.button:
            self.view_down = self.last_click = None
            self.pressed_mouse = dialog, True
            return True
        if not contains(dialog.view, hit):
            self.view_down = self.last_click = None
            return False
        if message == win32con.WM_LBUTTONUP:
            if self.view_down:
                started, prior = self.view_down
                self.last_click = (started, monotonic(), prior, None) if self.near(point, prior) else None
                self.view_down = None
        else:
            now = monotonic()
            previous, self.last_click = self.last_click, None
            if previous:
                first, _, prior, image = previous
                if (
                    image and now - first <= self.double_click_seconds and self.near(point, prior)
                    and not any(user.GetAsyncKeyState(k) & 0x8000 for k in (win32con.VK_CONTROL, win32con.VK_SHIFT))
                ):
                    self.pressed_mouse = dialog, False
                    return True
            self.view_down = now, point
        return False

    def near(self, first, second):
        return (
            abs(first[0] - second[0]) <= self.double_click_width // 2
            and abs(first[1] - second[1]) <= self.double_click_height // 2
        )

    def tick(self):
        window = win32gui.GetForegroundWindow()
        try:
            now = monotonic()
            if window != self.foreground:
                self.foreground = window
                self.dialog = None
                self.next_probe = 0
                self.view_down = self.last_click = None
            if not self.dialog or not win32gui.IsWindow(self.dialog.view):
                # The dialog may receive focus before the Shell creates its child controls.
                if now >= self.next_probe:
                    self.next_probe = now + 0.08
                    self.dialog = Dialog.find(window)
            if self.last_click and self.dialog:
                first, released, point, image = self.last_click
                if now - first > self.double_click_seconds:
                    self.last_click = None
                elif image is None and now - released >= 0.025:
                    names = selected_names(self.dialog.selection()[0])
                    image = (
                        len(names) == 1 and Path(names[0]).suffix.casefold() in self.extensions
                        and image_at(point, names[0])
                    )
                    self.last_click = first, released, point, image
            self.finish()
            dialog, self.pending = self.pending, None
            if dialog and dialog == self.active() and self.job is None:
                selection = dialog.selection()
                names = selected_names(selection[0])
                if any(Path(name).suffix.casefold() in self.extensions for name in names):
                    self.job = dialog, selection, self.executor.submit(compress_selection, names, selection[1])
                else:
                    dialog.confirm()
        except (OSError, ValueError, RuntimeError, win32gui.error) as error:
            if self.active():
                win32gui.MessageBox(
                    self.dialog.window, f"未能自动添加这批文件。\n\n{error}",
                    "Codex 文件选择", win32con.MB_ICONWARNING,
                )

    def finish(self):
        if self.job is None:
            return
        dialog, selection, future = self.job
        if future.done():
            self.job = None
            if dialog == self.active() and dialog.selection() == selection:
                dialog.confirm(future.result())
