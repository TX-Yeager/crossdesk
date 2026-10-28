"""Bounded native clipboard adapters. No network, polling, Tk or input injection.

Construct after the GUI has initialized (especially Tk's NSApplication on Mac).
Native access runs on the GUI main thread. ``read_raw`` only copies native bytes;
``prepare_snapshot`` and ``prepare_write`` are pure background codec operations;
``write_prepared`` only writes already-prepared OS formats. Legacy read/write
combine these operations. Creating an adapter does not inspect clipboard contents.
The caller gates access on the user's switch and authenticated active session.

Snapshots are one of ``{kind: text, text: str}``, ``{kind: image, data: PNGbytes}``
or ``{kind: files, paths: list[Path]}``. Files take priority over image and text
representations; files are never opened, traversed or transferred by this module.
Pillow validates and normalizes images before writing any native format.

References:
https://learn.microsoft.com/windows/win32/dataxchg/using-the-clipboard
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-openclipboard
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setclipboarddata
https://developer.apple.com/documentation/appkit/nspasteboard
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
import struct
import sys
import threading
from urllib.parse import unquote, urlsplit
import warnings


MAX_TEXT_BYTES = 1024 * 1024
MAX_NATIVE_TEXT_BYTES = MAX_TEXT_BYTES * 4 + 2  # UTF-16 plus Windows CR/LF expansion.
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_DIB_BYTES = MAX_IMAGE_PIXELS * 4 + 4096
MAX_FILE_PATHS = 64
MAX_PATH_CHARS = 32767
MAX_PATH_BYTES = 1024 * 1024
CF_UNICODETEXT, CF_HDROP, CF_DIB, CF_DIBV5 = 13, 15, 8, 17
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ClipboardError(RuntimeError):
    """A fixed diagnostic message, never copied content or file names."""


def _main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise ClipboardError("剪贴板原生接口必须由 GUI 主线程调用。")


def _text(value: object) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise ClipboardError("剪贴板文本格式无效。")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ClipboardError("剪贴板文本编码无效。") from None
    if size > MAX_TEXT_BYTES:
        raise ClipboardError("剪贴板文本超过 1 MiB 限制。")
    return value


def _paths(value: object, platform: str) -> list[Path]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_FILE_PATHS:
        raise ClipboardError("剪贴板文件数量无效或超过 64 个限制。")
    result, total = [], 0
    for item in value:
        if not isinstance(item, (str, Path)):
            raise ClipboardError("剪贴板文件路径无效。")
        text = item.as_posix() if isinstance(item, Path) and platform == "darwin" else str(item)
        pure = PureWindowsPath(text) if platform == "win32" else PurePosixPath(text)
        if not text or "\0" in text or len(text) > MAX_PATH_CHARS or not pure.is_absolute():
            raise ClipboardError("剪贴板文件路径必须为有效绝对路径。")
        try:
            total += len(text.encode("utf-8"))
        except UnicodeError:
            raise ClipboardError("剪贴板文件路径编码无效。") from None
        if total > MAX_PATH_BYTES:
            raise ClipboardError("剪贴板文件路径数据超过限制。")
        result.append(Path(text))
    return result


def _loaded_image(data: object, *, dib: bool = False, native_image: bool = False):
    if not isinstance(data, bytes) or not data or len(data) > (MAX_DIB_BYTES if dib or native_image else MAX_IMAGE_BYTES):
        raise ClipboardError("剪贴板图片数据无效或超过 16 MiB 限制。")
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(_dib_to_bmp(data) if dib else data))
            try:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ClipboardError("剪贴板图片超过 1600 万像素限制。")
                image.load()
                result = image.convert("RGBA")
                result.info.clear()
                return result
            finally:
                image.close()
    except ClipboardError:
        raise
    except Exception:
        raise ClipboardError("剪贴板图片无法解码。") from None


def normalize_png(data: bytes, *, dib: bool = False, native_image: bool = False) -> bytes:
    """Validate size/pixels and return a metadata-free PNG representation."""
    with _loaded_image(data, dib=dib, native_image=native_image) as image:
        output = BytesIO()
        image.save(output, format="PNG")
        value = output.getvalue()
    if len(value) > MAX_IMAGE_BYTES:
        raise ClipboardError("剪贴板图片超过 16 MiB 限制。")
    return value


def _dib_to_bmp(data: bytes) -> bytes:
    if len(data) < 12:
        raise ClipboardError("剪贴板位图头无效。")
    header = struct.unpack_from("<I", data)[0]
    if header == 12:
        _width, _height, planes, bits = struct.unpack_from("<HHHH", data, 4)
        palette = (1 << bits) * 3 if bits <= 8 else 0
        offset = 14 + header + palette
    elif header in (40, 52, 56, 108, 124) and len(data) >= header:
        planes, bits, compression = struct.unpack_from("<HHI", data, 12)
        used = struct.unpack_from("<I", data, 32)[0]
        palette = (used or (1 << bits if bits <= 8 else 0)) * 4
        masks = (16 if compression == 6 else 12) if header == 40 and compression in (3, 6) else 0
        offset = 14 + header + masks + palette
    else:
        raise ClipboardError("剪贴板位图头不受支持。")
    if planes != 1 or bits not in (1, 4, 8, 16, 24, 32) or offset >= len(data) + 14:
        raise ClipboardError("剪贴板位图布局无效。")
    return struct.pack("<2sIHHI", b"BM", len(data) + 14, 0, 0, offset) + data


def _windows_image_formats(png: bytes) -> tuple[bytes, bytes, bytes]:
    png = normalize_png(png)
    with _loaded_image(png) as image:
        width, height = image.size
        pixels = image.tobytes("raw", "BGRA")
        header = bytearray(124)
        struct.pack_into("<IiiHHIIiiII", header, 0, 124, width, -height, 1, 32, 3,
                         len(pixels), 0, 0, 0, 0)
        struct.pack_into("<IIIII", header, 40, 0x00FF0000, 0x0000FF00,
                         0x000000FF, 0xFF000000, 0x73524742)
        struct.pack_into("<I", header, 108, 4)  # LCS_GM_IMAGES.
        dibv5 = bytes(header) + pixels
        from PIL import Image
        with Image.new("RGB", image.size, "white") as opaque:
            opaque.paste(image, mask=image.getchannel("A"))
            output = BytesIO()
            opaque.save(output, format="BMP")
            dib = output.getvalue()[14:]
    return png, dibv5, dib


def _dropfiles(paths: list[Path]) -> bytes:
    names = "\0".join(str(path) for path in paths) + "\0\0"
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + names.encode("utf-16-le")


@dataclass(frozen=True)
class PreparedClipboard:
    """Local-only codec result, never a wire message or unvalidated OS format."""

    snapshot: dict
    platform: str
    formats: tuple[tuple[object, bytes], ...] = ()


def prepare_snapshot(raw: dict | None, *, platform: str | None = None) -> dict | None:
    """Pure codec/validation work; safe on a background worker, no native calls."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ClipboardError("剪贴板快照格式无效。")
    kind = raw.get("kind")
    if kind == "text":
        return {"kind": "text", "text": _text(raw.get("text"))}
    if kind == "files":
        return {"kind": "files", "paths": _paths(raw.get("paths"), platform or sys.platform)}
    if kind == "image":
        encoding = raw.get("encoding", "png")
        if encoding not in ("png", "tiff", "dib"):
            raise ClipboardError("剪贴板图片编码不受支持。")
        return {"kind": "image", "data": normalize_png(raw.get("data"), dib=encoding == "dib",
                                                         native_image=encoding == "tiff")}
    raise ClipboardError("剪贴板快照类型不受支持。")


def prepare_write(snapshot: dict, *, platform: str | None = None) -> PreparedClipboard:
    """Pure preprocessing for a later short, GUI-only write_prepared call."""
    platform = platform or sys.platform
    if platform not in ("win32", "darwin"):
        raise ClipboardError("剪贴板平台不受支持。")
    if not isinstance(snapshot, dict):
        raise ClipboardError("剪贴板快照格式无效。")
    kind = snapshot.get("kind")
    formats = ()
    if kind == "text":
        text = _text(snapshot.get("text"))
        canonical = {"kind": "text", "text": text}
        if platform == "win32":
            formats = ((CF_UNICODETEXT, (text.replace("\n", "\r\n") + "\0").encode("utf-16-le")),)
    elif kind == "image":
        if platform == "win32":
            png, dibv5, dib = _windows_image_formats(snapshot.get("data"))
            formats = (("png", png), (CF_DIBV5, dibv5), (CF_DIB, dib))
        else:
            png = normalize_png(snapshot.get("data"))
        canonical = {"kind": "image", "data": png}
    elif kind == "files":
        paths = _paths(snapshot.get("paths"), platform)
        canonical = {"kind": "files", "paths": paths}
        if platform == "win32":
            formats = ((CF_HDROP, _dropfiles(paths)),)
    else:
        raise ClipboardError("剪贴板快照类型不受支持。")
    return PreparedClipboard(canonical, platform, formats)


class _PreparedAdapter:
    platform: str

    def prepare_snapshot(self, raw: dict | None) -> dict | None:
        return prepare_snapshot(raw, platform=self.platform)

    def prepare_write(self, snapshot: dict) -> PreparedClipboard:
        return prepare_write(snapshot, platform=self.platform)

    def read(self) -> dict | None:
        # Compatibility API. The broker uses read_raw + background prepare.
        return self.prepare_snapshot(self.read_raw())

    def write(self, snapshot: dict) -> None:
        # Compatibility API. Never preprocess a GUI write on an input hook.
        _main_thread()
        self.write_prepared(self.prepare_write(snapshot))


class WindowsClipboard(_PreparedAdapter):
    """Win32 CF_UNICODETEXT, registered PNG/DIB and CF_HDROP adapter."""

    platform = "win32"

    def __init__(self, window_handle: int) -> None:
        _main_thread()
        if sys.platform != "win32":
            raise OSError("Windows clipboard is available only on Windows")
        if not isinstance(window_handle, int) or not window_handle:
            raise ClipboardError("Windows 剪贴板写入需要有效的应用窗口。")
        self.user32 = C.WinDLL("user32", use_last_error=True)
        self.kernel32 = C.WinDLL("kernel32", use_last_error=True)
        self.shell32 = C.WinDLL("shell32", use_last_error=True)
        self._bind()
        self.window_handle = window_handle
        if not self.user32.IsWindow(window_handle):
            raise ClipboardError("Windows 剪贴板窗口不可用。")
        self.png_format = self.user32.RegisterClipboardFormatW("PNG")
        if not self.png_format:
            raise ClipboardError("Windows PNG 剪贴板格式不可用。")

    def _bind(self) -> None:
        def bind(lib, name, result, *arguments):
            function = getattr(lib, name)
            function.restype, function.argtypes = result, list(arguments)
        ptr, uint, integer = C.c_void_p, C.c_uint32, C.c_int32
        bind(self.user32, "IsWindow", integer, ptr)
        bind(self.user32, "OpenClipboard", integer, ptr)
        bind(self.user32, "CloseClipboard", integer)
        bind(self.user32, "EmptyClipboard", integer)
        bind(self.user32, "IsClipboardFormatAvailable", integer, uint)
        bind(self.user32, "GetClipboardData", ptr, uint)
        bind(self.user32, "SetClipboardData", ptr, uint, ptr)
        bind(self.user32, "RegisterClipboardFormatW", uint, C.c_wchar_p)
        bind(self.user32, "GetClipboardSequenceNumber", uint)
        bind(self.kernel32, "GlobalSize", C.c_size_t, ptr)
        bind(self.kernel32, "GlobalLock", ptr, ptr)
        bind(self.kernel32, "GlobalUnlock", integer, ptr)
        bind(self.kernel32, "GlobalAlloc", ptr, uint, C.c_size_t)
        bind(self.kernel32, "GlobalFree", ptr, ptr)
        bind(self.shell32, "DragQueryFileW", uint, ptr, uint, C.c_wchar_p, uint)

    @contextmanager
    def _opened(self):
        if not self.user32.OpenClipboard(self.window_handle):
            raise ClipboardError("系统剪贴板正忙，稍后重试。")
        try:
            yield
        finally:
            self.user32.CloseClipboard()

    def get_change_token(self) -> int:
        _main_thread()
        return int(self.user32.GetClipboardSequenceNumber())

    def _data(self, format_id: int, limit: int) -> bytes | None:
        handle = self.user32.GetClipboardData(format_id)
        if not handle:
            return None
        size = self.kernel32.GlobalSize(handle)
        if not 0 < size <= limit:
            raise ClipboardError("剪贴板数据大小超过限制。")
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardError("无法读取系统剪贴板数据。")
        try:
            return C.string_at(pointer, size)
        finally:
            self.kernel32.GlobalUnlock(handle)

    def read_raw(self) -> dict | None:
        _main_thread()
        with self._opened():
            available = self.user32.IsClipboardFormatAvailable
            if available(CF_HDROP):
                handle = self.user32.GetClipboardData(CF_HDROP)
                if handle:
                    count = self.shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
                    if not 1 <= count <= MAX_FILE_PATHS:
                        raise ClipboardError("剪贴板文件数量超过 64 个限制。")
                    names = []
                    for index in range(count):
                        length = self.shell32.DragQueryFileW(handle, index, None, 0)
                        if not 0 < length <= MAX_PATH_CHARS:
                            raise ClipboardError("剪贴板文件路径长度无效。")
                        buffer = C.create_unicode_buffer(length + 1)
                        copied = self.shell32.DragQueryFileW(handle, index, buffer, length + 1)
                        if copied != length:
                            raise ClipboardError("无法读取剪贴板文件路径。")
                        names.append(buffer.value)
                    return {"kind": "files", "paths": _paths(names, "win32")}
            for format_id in (self.png_format, CF_DIBV5, CF_DIB):
                if available(format_id):
                    value = self._data(format_id, MAX_IMAGE_BYTES if format_id == self.png_format else MAX_DIB_BYTES)
                    if value:
                        return {"kind": "image", "data": value,
                                "encoding": "png" if format_id == self.png_format else "dib"}
            if available(CF_UNICODETEXT):
                data = self._data(CF_UNICODETEXT, MAX_NATIVE_TEXT_BYTES)
                if data is not None:
                    # Use the native byte search for large text, while ignoring
                    # false terminators spanning two adjacent UTF-16 code units.
                    end = data.find(b"\0\0")
                    while end >= 0 and end % 2:
                        end = data.find(b"\0\0", end + 1)
                    if end < 0:
                        raise ClipboardError("剪贴板 Unicode 文本缺少终止符。")
                    try:
                        return {"kind": "text", "text": _text(data[:end].decode("utf-16-le"))}
                    except UnicodeError:
                        raise ClipboardError("剪贴板文本编码无效。") from None
        return None

    def _allocate(self, data: bytes) -> int:
        handle = self.kernel32.GlobalAlloc(2, len(data))  # GMEM_MOVEABLE.
        if not handle:
            raise ClipboardError("无法分配剪贴板内存。")
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            self.kernel32.GlobalFree(handle)
            raise ClipboardError("无法锁定剪贴板内存。")
        try:
            C.memmove(pointer, data, len(data))
        finally:
            self.kernel32.GlobalUnlock(handle)
        return handle

    def write_prepared(self, prepared: PreparedClipboard) -> None:
        _main_thread()
        if not isinstance(prepared, PreparedClipboard) or prepared.platform != self.platform:
            raise ClipboardError("剪贴板预处理数据无效。")
        expected = {"text": (CF_UNICODETEXT,), "files": (CF_HDROP,), "image": ("png", CF_DIBV5, CF_DIB)}
        if tuple(item[0] for item in prepared.formats) != expected.get(prepared.snapshot.get("kind")):
            raise ClipboardError("剪贴板原生格式无效。")
        formats = []
        for format_id, data in prepared.formats:
            limit = (MAX_IMAGE_BYTES if format_id == "png" else MAX_DIB_BYTES if format_id in (CF_DIB, CF_DIBV5)
                     else MAX_NATIVE_TEXT_BYTES if format_id == CF_UNICODETEXT else MAX_PATH_BYTES * 2 + 256)
            if not isinstance(data, bytes) or not 0 < len(data) <= limit:
                raise ClipboardError("剪贴板原生数据大小无效。")
            formats.append((self.png_format if format_id == "png" else format_id, data))
        pending = []
        try:
            # Complete validation/allocation before replacing the old clipboard.
            for format_id, data in formats:
                pending.append([format_id, self._allocate(data)])
            with self._opened():
                if not self.user32.EmptyClipboard():
                    raise ClipboardError("无法更新系统剪贴板。")
                for item in pending:
                    if not self.user32.SetClipboardData(item[0], item[1]):
                        raise ClipboardError("无法写入系统剪贴板。")
                    item[1] = None  # Successful SetClipboardData transfers ownership.
        finally:
            for _format, handle in pending:
                if handle:
                    self.kernel32.GlobalFree(handle)


class MacClipboard(_PreparedAdapter):
    """Public NSPasteboard adapter, without creating NSApplication."""

    platform = "darwin"

    def __init__(self) -> None:
        _main_thread()
        if sys.platform != "darwin":
            raise OSError("Mac clipboard is available only on macOS")
        self.appkit = C.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        self.objc = C.CDLL("/usr/lib/libobjc.A.dylib")
        self.objc.objc_getClass.restype, self.objc.objc_getClass.argtypes = C.c_void_p, [C.c_char_p]
        self.objc.sel_registerName.restype, self.objc.sel_registerName.argtypes = C.c_void_p, [C.c_char_p]
        self._message_address = C.cast(self.objc.objc_msgSend, C.c_void_p).value
        self._messages, self._selectors = {}, {}
        self._types = {name: C.c_void_p.in_dll(self.appkit, symbol).value for name, symbol in
                       (("text", "NSPasteboardTypeString"), ("png", "NSPasteboardTypePNG"),
                        ("tiff", "NSPasteboardTypeTIFF"), ("file", "NSPasteboardTypeFileURL"))}

    def _class(self, name: str) -> int:
        value = self.objc.objc_getClass(name.encode("ascii"))
        if not value:
            raise ClipboardError("系统剪贴板类不可用。")
        return value

    def _send(self, receiver, name, result=None, arguments=(), *values):
        key = result, arguments
        if key not in self._messages:
            self._messages[key] = C.CFUNCTYPE(result, C.c_void_p, C.c_void_p, *arguments)(self._message_address)
        if name not in self._selectors:
            self._selectors[name] = self.objc.sel_registerName(name.encode("ascii"))
        return self._messages[key](receiver, self._selectors[name], *values)

    @contextmanager
    def _pool(self):
        pool = self._send(self._class("NSAutoreleasePool"), "new", C.c_void_p)
        try:
            yield
        finally:
            if pool:
                self._send(pool, "drain")

    def _pasteboard(self):
        board = self._send(self._class("NSPasteboard"), "generalPasteboard", C.c_void_p)
        if not board:
            raise ClipboardError("系统剪贴板不可用。")
        return board

    def _nsstring(self, text: str):
        data = text.encode("utf-8")
        value = self._send(self._class("NSString"), "alloc", C.c_void_p)
        value = self._send(value, "initWithBytes:length:encoding:", C.c_void_p,
                           (C.c_char_p, C.c_ulong, C.c_ulong), data, len(data), 4)
        if not value:
            raise ClipboardError("无法构造剪贴板文本。")
        return self._send(value, "autorelease", C.c_void_p)

    def _string(self, value, limit=MAX_TEXT_BYTES):
        if not value:
            return None
        length = self._send(value, "lengthOfBytesUsingEncoding:", C.c_ulong, (C.c_ulong,), 4)
        if length > limit:
            raise ClipboardError("剪贴板文本数据超过限制。")
        pointer = self._send(value, "UTF8String", C.c_void_p)
        if not pointer and length:
            raise ClipboardError("无法读取剪贴板文本。")
        try:
            return C.string_at(pointer, length).decode("utf-8") if length else ""
        except UnicodeError:
            raise ClipboardError("剪贴板文本编码无效。") from None

    def _data(self, value, limit):
        if not value:
            return None
        length = self._send(value, "length", C.c_ulong)
        if not 0 < length <= limit:
            raise ClipboardError("剪贴板图片数据超过限制。")
        pointer = self._send(value, "bytes", C.c_void_p)
        if not pointer:
            raise ClipboardError("无法读取剪贴板图片。")
        return C.string_at(pointer, length)

    def get_change_token(self) -> int:
        _main_thread()
        with self._pool():
            return int(self._send(self._pasteboard(), "changeCount", C.c_long))

    def read_raw(self) -> dict | None:
        _main_thread()
        with self._pool():
            board = self._pasteboard()
            items = self._send(board, "pasteboardItems", C.c_void_p)
            count = self._send(items, "count", C.c_ulong)
            if count > MAX_FILE_PATHS:
                raise ClipboardError("剪贴板项目超过 64 个限制。")
            files = []
            for index in range(count):
                item = self._send(items, "objectAtIndex:", C.c_void_p, (C.c_ulong,), index)
                value = self._send(item, "stringForType:", C.c_void_p, (C.c_void_p,), self._types["file"])
                if value:
                    text = self._string(value, MAX_PATH_CHARS * 12)
                    try:
                        url = urlsplit(text)
                        if url.scheme != "file" or url.netloc not in ("", "localhost") or url.query or url.fragment:
                            raise ValueError
                        files.append(unquote(url.path, errors="strict"))
                    except (ValueError, UnicodeError):
                        raise ClipboardError("剪贴板文件 URL 无效。") from None
            if files:
                return {"kind": "files", "paths": _paths(files, "darwin")}
            for kind in ("png", "tiff"):
                value = self._send(board, "dataForType:", C.c_void_p, (C.c_void_p,), self._types[kind])
                if value:
                    # Native TIFF may be uncompressed, like Windows DIB. The
                    # worker applies pixel/output limits before transmitting.
                    data = self._data(value, MAX_DIB_BYTES if kind == "tiff" else MAX_IMAGE_BYTES)
                    return {"kind": "image", "data": data, "encoding": kind}
            value = self._send(board, "stringForType:", C.c_void_p, (C.c_void_p,), self._types["text"])
            if value:
                return {"kind": "text", "text": _text(self._string(value, MAX_TEXT_BYTES * 2))}
        return None

    def write_prepared(self, prepared: PreparedClipboard) -> None:
        _main_thread()
        if not isinstance(prepared, PreparedClipboard) or prepared.platform != self.platform:
            raise ClipboardError("剪贴板预处理数据无效。")
        snapshot = prepared.snapshot
        kind = snapshot.get("kind")
        # No Pillow or decompression runs on this GUI-only path.
        if kind == "text":
            payload = _text(snapshot.get("text"))
        elif kind == "image":
            payload = snapshot.get("data")
            if not isinstance(payload, bytes) or not payload.startswith(PNG_SIGNATURE) or len(payload) > MAX_IMAGE_BYTES:
                raise ClipboardError("剪贴板预处理图片无效。")
        elif kind == "files":
            payload = _paths(snapshot.get("paths"), "darwin")
        else:
            raise ClipboardError("剪贴板快照类型不受支持。")
        with self._pool():
            board = self._pasteboard()
            if kind == "text":
                value = self._nsstring(payload)
                self._send(board, "clearContents", C.c_long)
                success = self._send(board, "setString:forType:", C.c_bool,
                                     (C.c_void_p, C.c_void_p), value, self._types["text"])
            elif kind == "image":
                value = self._send(self._class("NSData"), "dataWithBytes:length:", C.c_void_p,
                                   (C.c_char_p, C.c_ulong), payload, len(payload))
                if not value:
                    raise ClipboardError("无法构造剪贴板图片。")
                self._send(board, "clearContents", C.c_long)
                success = self._send(board, "setData:forType:", C.c_bool,
                                     (C.c_void_p, C.c_void_p), value, self._types["png"])
            else:
                values = []
                for path in payload:
                    url = self._send(self._class("NSURL"), "fileURLWithPath:", C.c_void_p,
                                     (C.c_void_p,), self._nsstring(path.as_posix()))
                    if not url:
                        raise ClipboardError("无法构造剪贴板文件 URL。")
                    values.append(url)
                pointers = (C.c_void_p * len(values))(*values)
                array = self._send(self._class("NSArray"), "arrayWithObjects:count:", C.c_void_p,
                                   (C.POINTER(C.c_void_p), C.c_ulong), pointers, len(values))
                if not array:
                    raise ClipboardError("无法构造剪贴板文件列表。")
                self._send(board, "clearContents", C.c_long)
                success = self._send(board, "writeObjects:", C.c_bool, (C.c_void_p,), array)
            if not success:
                raise ClipboardError("系统剪贴板写入失败，请稍后重试。")


def create_clipboard(window_handle: int | None = None):
    """Create only the native adapter; this does not read clipboard contents."""
    _main_thread()
    if sys.platform == "win32":
        return WindowsClipboard(window_handle)
    if sys.platform == "darwin":
        return MacClipboard()
    raise OSError("Clipboard sharing supports Windows and macOS")
