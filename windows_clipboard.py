"""Copy actual file references on Windows, using the Explorer clipboard format."""

from contextlib import contextmanager
from ctypes import WinDLL, WinError, create_unicode_buffer, get_last_error, memmove, set_last_error
from ctypes import wintypes
from pathlib import Path
from struct import pack
from time import sleep


CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
CF_BITMAP = 2
CF_PALETTE = 9
MEMORY_FORMATS = {1, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 16, 17}
REGISTERED_FORMAT_START = 0xC000
OLE_OWNER_FORMATS = {"DataObject", "Ole Private Data"}
VIRTUAL_FILE_FORMATS = {"FileContents", "FileGroupDescriptor", "FileGroupDescriptorW"}

user = WinDLL("user32", use_last_error=True)
core = WinDLL("kernel32", use_last_error=True)
ole = WinDLL("ole32", use_last_error=True)
gdi = WinDLL("gdi32", use_last_error=True)
user.OpenClipboard.argtypes = [wintypes.HWND]
user.GetClipboardOwner.restype = wintypes.HWND
user.EnumClipboardFormats.argtypes = [wintypes.UINT]
user.EnumClipboardFormats.restype = wintypes.UINT
user.GetClipboardSequenceNumber.restype = wintypes.DWORD
user.GetClipboardData.argtypes = [wintypes.UINT]
user.GetClipboardData.restype = wintypes.HANDLE
user.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.INT]
user.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user.SetClipboardData.restype = wintypes.HANDLE
user.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user.RegisterClipboardFormatW.restype = wintypes.UINT
core.GlobalAlloc.argtypes = [wintypes.UINT, wintypes.WPARAM]
core.GlobalAlloc.restype = wintypes.HGLOBAL
core.GlobalLock.argtypes = [wintypes.HGLOBAL]
core.GlobalLock.restype = wintypes.LPVOID
core.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
core.GlobalFree.argtypes = [wintypes.HGLOBAL]
core.GlobalFree.restype = wintypes.HGLOBAL
ole.OleDuplicateData.argtypes = [wintypes.HANDLE, wintypes.WORD, wintypes.UINT]
ole.OleDuplicateData.restype = wintypes.HANDLE
gdi.DeleteObject.argtypes = [wintypes.HANDLE]


def sequence() -> int:
    return user.GetClipboardSequenceNumber()


def clipboard_owner() -> int | None:
    return user.GetClipboardOwner()


def global_bytes(payload: bytes) -> int:
    memory = core.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not memory:
        raise WinError(get_last_error())
    try:
        address = core.GlobalLock(memory)
        if not address:
            raise WinError(get_last_error())
        try:
            memmove(address, payload, len(payload))
        finally:
            core.GlobalUnlock(memory)
    except BaseException:
        core.GlobalFree(memory)
        raise
    return memory


def file_drop(files: tuple[Path, ...]) -> int:
    if not files or any(not file.is_file() for file in files):
        raise ValueError("附件文件已不存在，请重新生成。")
    # DROPFILES header followed by a double-NUL-terminated list of UTF-16 paths.
    header = pack("<IiiII", 20, 0, 0, 0, 1)
    paths = ("\0".join(str(file) for file in files) + "\0\0").encode("utf-16-le")
    return global_bytes(header + paths)


@contextmanager
def opened(owner: int):
    for _ in range(20):
        if user.OpenClipboard(owner):
            break
        sleep(0.025)
    else:
        raise WinError(get_last_error())
    try:
        yield
    finally:
        user.CloseClipboard()


def release(data: dict[int, int]) -> None:
    for fmt, handle in data.items():
        if fmt in {CF_BITMAP, CF_PALETTE}:
            gdi.DeleteObject(handle)
        else:
            core.GlobalFree(handle)


def write_formats(data: dict[int, int]) -> None:
    if not user.EmptyClipboard():
        raise WinError(get_last_error())
    for fmt, handle in tuple(data.items()):
        if not user.SetClipboardData(fmt, handle):
            raise WinError(get_last_error())
        del data[fmt]  # Windows owns the handle after SetClipboardData succeeds.


def copy_files(owner: int, files: tuple[Path, ...]) -> None:
    data = {CF_HDROP: file_drop(files)}
    try:
        with opened(owner):
            write_formats(data)
    finally:
        release(data)


def snapshot(data: dict[int, int]) -> None:
    has_file_drop = user.IsClipboardFormatAvailable(CF_HDROP)
    name = create_unicode_buffer(256)
    fmt = 0
    while True:
        set_last_error(0)
        fmt = user.EnumClipboardFormats(fmt)
        if not fmt:
            if get_last_error():
                raise WinError(get_last_error())
            if has_file_drop and CF_HDROP not in data:
                raise ValueError("无法备份剪贴板中的原文件路径。")
            return
        if fmt >= REGISTERED_FORMAT_START:
            if user.GetClipboardFormatNameW(fmt, name, len(name)):
                # OLE's owner references cannot be reused by a different clipboard owner.
                if name.value in OLE_OWNER_FORMATS:
                    continue
                # For actual files, CF_HDROP preserves the originals without a virtual file stream.
                if has_file_drop and name.value in VIRTUAL_FILE_FORMATS:
                    continue
        elif fmt not in MEMORY_FORMATS and fmt not in {CF_BITMAP, CF_PALETTE}:
            raise ValueError("剪贴板包含无法安全恢复的格式。")
        source = user.GetClipboardData(fmt)
        handle = ole.OleDuplicateData(source, fmt, GMEM_MOVEABLE) if source else None
        if not handle:
            raise ValueError("无法备份当前剪贴板。")
        data[fmt] = handle


@contextmanager
def temporary_files(owner: int, files: tuple[Path, ...], expected_sequence: int):
    """Expose compressed file references briefly and preserve the source clipboard."""
    original = {}
    temporary = {CF_HDROP: file_drop(files)}
    installed = None
    try:
        exclude = user.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
        if exclude:
            temporary[exclude] = global_bytes(pack("<I", 1))
        with opened(owner):
            if sequence() != expected_sequence:
                raise ValueError("图片压缩期间剪贴板已更新。")
            snapshot(original)
            try:
                write_formats(temporary)
            except OSError:
                write_formats(original)
                raise
            installed = sequence()
        yield
    finally:
        try:
            if installed is not None:
                with opened(owner):
                    if sequence() == installed:
                        write_formats(original)
        finally:
            release(temporary)
            release(original)
