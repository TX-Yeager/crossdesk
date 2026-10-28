"""Native clipboard ABI/lifetime behavior with fake OS calls, never real data."""

from contextlib import nullcontext
import ctypes as C
from io import BytesIO
from pathlib import Path
import struct
import threading
import unittest
from unittest import mock

from crossdesk import clipboard_native as native

try:
    from PIL import Image
except ImportError:
    Image = None


class FakeKernel:
    def __init__(self):
        self.buffers = {}
        self.freed = []
        self.unlocked = []
        self.next_handle = 1
        self.fail_lock = False

    def put(self, data):
        handle = self.next_handle
        self.next_handle += 1
        self.buffers[handle] = C.create_string_buffer(data, len(data))
        return handle

    def GlobalSize(self, handle):
        return C.sizeof(self.buffers[handle])

    def GlobalLock(self, handle):
        return 0 if self.fail_lock else C.addressof(self.buffers[handle])

    def GlobalUnlock(self, handle):
        self.unlocked.append(handle)
        return 1

    def GlobalAlloc(self, flag, length):
        if flag != 2:
            raise AssertionError("Clipboard allocation must be moveable")
        return self.put(b"\0" * length)

    def GlobalFree(self, handle):
        self.freed.append(handle)
        return 0


class FakeUser32:
    def __init__(self, kernel):
        self.kernel = kernel
        self.formats = {}
        self.open_ok = True
        self.opened, self.closed, self.emptied = 0, 0, 0
        self.sets = []
        self.fail_set = None

    def add(self, format_id, data):
        self.formats[format_id] = self.kernel.put(data)

    def OpenClipboard(self, owner):
        if owner != 91:
            raise AssertionError("Must use the application's valid HWND")
        self.opened += 1
        return self.open_ok

    def CloseClipboard(self):
        self.closed += 1
        return 1

    def IsClipboardFormatAvailable(self, format_id):
        return format_id in self.formats

    def GetClipboardData(self, format_id):
        return self.formats.get(format_id, 0)

    def EmptyClipboard(self):
        self.emptied += 1
        self.formats.clear()
        return 1

    def SetClipboardData(self, format_id, handle):
        if format_id == self.fail_set:
            return 0
        self.sets.append((format_id, handle))
        self.formats[format_id] = handle
        return handle

    def GetClipboardSequenceNumber(self):
        return 0xFFFFFFFF


class FakeShell:
    def __init__(self):
        self.paths = [r"C:\copied\测试.txt", r"C:\copied\folder"]

    def DragQueryFileW(self, _handle, index, buffer, _maximum):
        if index == 0xFFFFFFFF:
            return len(self.paths)
        value = self.paths[index]
        if buffer is not None:
            buffer.value = value
        return len(value)


def windows():
    clipboard = native.WindowsClipboard.__new__(native.WindowsClipboard)
    clipboard.kernel32 = FakeKernel()
    clipboard.user32 = FakeUser32(clipboard.kernel32)
    clipboard.shell32 = FakeShell()
    clipboard.window_handle, clipboard.png_format = 91, 500
    return clipboard


class FakeMac(native.MacClipboard):
    def __init__(self):
        self._types = {name: name for name in ("text", "png", "tiff", "file")}
        self.items = []
        self.values = {}
        self.calls = []
        self.urls = {}
        self.write_ok = True

    def _pool(self):
        return nullcontext()

    def _pasteboard(self):
        return "board"

    def _class(self, name):
        return name

    def _nsstring(self, value):
        return value

    def _string(self, value, limit=native.MAX_TEXT_BYTES):
        if len(value.encode("utf-8")) > limit:
            raise native.ClipboardError("剪贴板文本数据超过限制。")
        return value

    def _data(self, value, limit):
        if len(value) > limit:
            raise native.ClipboardError("剪贴板图片数据超过限制。")
        return value

    def _send(self, receiver, name, _result=None, _arguments=(), *values):
        self.calls.append((name, values))
        if name == "changeCount":
            return 17
        if name == "pasteboardItems":
            return self.items
        if name == "count":
            return len(receiver)
        if name == "objectAtIndex:":
            return receiver[values[0]]
        if name in ("stringForType:", "dataForType:"):
            return (self.values if receiver == "board" else receiver).get(values[0])
        if name == "clearContents":
            return 18
        if name in ("setString:forType:", "setData:forType:"):
            self.written = values
            return self.write_ok
        if name == "dataWithBytes:length:":
            return values[0]
        if name == "fileURLWithPath:":
            token = 100 + len(self.urls)
            self.urls[token] = values[0]
            return token
        if name == "arrayWithObjects:count:":
            return [self.urls[value] for value in values[0]]
        if name == "writeObjects:":
            self.written = list(values[0])
            return self.write_ok
        raise AssertionError("Unexpected native selector")


class ClipboardValidationTests(unittest.TestCase):
    def test_prepared_write_requires_matching_local_preparation_object(self):
        win, mac = windows(), FakeMac()
        wrong = native.prepare_write({"kind": "text", "text": "fake"}, platform="darwin")
        with self.assertRaises(native.ClipboardError):
            win.write_prepared(wrong)
        with self.assertRaises(native.ClipboardError):
            mac.write_prepared({"kind": "text", "text": "fake"})
        self.assertEqual(win.user32.opened, 0)
        self.assertEqual(mac.calls, [])

    def test_text_is_unicode_and_has_canonical_newlines(self):
        self.assertEqual(native._text("中\r\n文\r🙂"), "中\n文\n🙂")
        self.assertEqual(native._text(""), "")

    def test_text_rejects_nul_invalid_unicode_and_utf8_size_before_native_write(self):
        for text in ("secret\0marker", "\ud800", "中" * (native.MAX_TEXT_BYTES // 3 + 1)):
            clipboard = windows()
            with self.assertRaises(native.ClipboardError) as error:
                clipboard.write({"kind": "text", "text": text})
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(clipboard.user32.opened, 0)
            self.assertEqual(clipboard.kernel32.buffers, {})

    def test_path_validation_is_platform_specific_and_never_walks(self):
        self.assertEqual(native._paths([r"C:\copied\folder"], "win32"), [Path(r"C:\copied\folder")])
        self.assertEqual(native._paths(["/Users/fake/copied"], "darwin"), [Path("/Users/fake/copied")])
        for values, platform in ((["relative"], "win32"), (["relative"], "darwin"),
                                 (["/bad\0path"], "darwin"), ([], "darwin"),
                                 (["/a"] * 65, "darwin")):
            with self.assertRaises(native.ClipboardError):
                native._paths(values, platform)

    def test_dropfiles_uses_wide_double_terminated_absolute_names(self):
        data = native._dropfiles([Path(r"C:\fake\一.txt"), Path(r"C:\fake\two.txt")])
        self.assertEqual(struct.unpack_from("<IiiII", data), (20, 0, 0, 0, 1))
        self.assertEqual(data[20:].decode("utf-16-le"), "C:\\fake\\一.txt\0C:\\fake\\two.txt\0\0")

    def test_factory_does_not_read_clipboard_and_requires_supported_platform(self):
        with mock.patch.object(native.sys, "platform", "win32"), mock.patch.object(native, "WindowsClipboard") as cls:
            self.assertIs(native.create_clipboard(91), cls.return_value)
            cls.assert_called_once_with(91)
        with mock.patch.object(native.sys, "platform", "linux"):
            with self.assertRaises(OSError):
                native.create_clipboard()

    def test_all_native_operations_reject_background_threads(self):
        win, mac = windows(), FakeMac()
        errors = []
        def run():
            for clipboard in (win, mac):
                for operation in (clipboard.get_change_token, clipboard.read,
                                  lambda: clipboard.write({"kind": "text", "text": "fake"})):
                    try:
                        operation()
                    except native.ClipboardError:
                        errors.append(True)
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 6)
        self.assertEqual(win.user32.opened, 0)
        self.assertEqual(mac.calls, [])


class WindowsClipboardTests(unittest.TestCase):
    def test_read_unicode_scans_only_aligned_utf16_terminators_and_closes(self):
        clipboard = windows()
        clipboard.user32.add(native.CF_UNICODETEXT, "中🙂\r\nA\0".encode("utf-16-le"))
        self.assertEqual(clipboard.read(), {"kind": "text", "text": "中🙂\nA"})
        self.assertEqual(clipboard.user32.closed, 1)
        self.assertEqual(len(clipboard.kernel32.unlocked), 1)

    def test_read_missing_terminator_fails_without_unbounded_read(self):
        clipboard = windows()
        clipboard.user32.add(native.CF_UNICODETEXT, "no terminator".encode("utf-16-le"))
        with self.assertRaises(native.ClipboardError):
            clipboard.read()
        self.assertEqual(clipboard.user32.closed, 1)

    def test_read_oversized_data_fails_before_global_lock(self):
        clipboard = windows()
        clipboard.user32.add(native.CF_UNICODETEXT, b"x" * (native.MAX_NATIVE_TEXT_BYTES + 2))
        with self.assertRaises(native.ClipboardError):
            clipboard.read()
        self.assertEqual(clipboard.kernel32.unlocked, [])
        self.assertEqual(clipboard.user32.closed, 1)

    def test_busy_clipboard_is_not_closed_or_emptied(self):
        clipboard = windows()
        clipboard.user32.open_ok = False
        with self.assertRaises(native.ClipboardError):
            clipboard.read()
        self.assertEqual(clipboard.user32.closed, 0)
        self.assertEqual(clipboard.user32.emptied, 0)

    def test_files_are_read_as_paths_and_take_priority_over_text(self):
        clipboard = windows()
        clipboard.user32.add(native.CF_HDROP, b"fake")
        clipboard.user32.add(native.CF_UNICODETEXT, "fallback\0".encode("utf-16-le"))
        self.assertEqual(clipboard.read(), {"kind": "files", "paths": [Path(p) for p in clipboard.shell32.paths]})

    def test_empty_or_too_many_file_items_do_not_allocate_paths(self):
        for paths in ([], ["fake"] * 65):
            clipboard = windows()
            clipboard.user32.add(native.CF_HDROP, b"fake")
            clipboard.shell32.paths = paths
            with self.assertRaises(native.ClipboardError):
                clipboard.read()
            self.assertEqual(clipboard.user32.closed, 1)

    def test_write_transfers_memory_to_windows_and_preserves_crlf(self):
        clipboard = windows()
        clipboard.write({"kind": "text", "text": "中🙂\nline"})
        handle = clipboard.user32.formats[native.CF_UNICODETEXT]
        self.assertEqual(bytes(clipboard.kernel32.buffers[handle]).decode("utf-16-le"), "中🙂\r\nline\0")
        self.assertEqual(clipboard.kernel32.freed, [])
        self.assertEqual(clipboard.user32.closed, 1)

    def test_crlf_expansion_does_not_reject_text_within_normalized_limit(self):
        clipboard = windows()
        clipboard.write({"kind": "text", "text": "\n" * native.MAX_TEXT_BYTES})
        self.assertEqual(clipboard.read(), {"kind": "text", "text": "\n" * native.MAX_TEXT_BYTES})

    def test_failed_write_or_open_frees_only_untransferred_memory(self):
        for fail_set in (True, False):
            clipboard = windows()
            clipboard.user32.fail_set = native.CF_UNICODETEXT if fail_set else None
            clipboard.user32.open_ok = fail_set
            with self.assertRaises(native.ClipboardError):
                clipboard.write({"kind": "text", "text": "fake"})
            self.assertEqual(clipboard.kernel32.freed, [1])
            self.assertEqual(clipboard.user32.closed, 1 if fail_set else 0)

    def test_failed_global_lock_frees_allocation_before_clipboard_is_opened(self):
        clipboard = windows()
        clipboard.kernel32.fail_lock = True
        with self.assertRaises(native.ClipboardError):
            clipboard.write({"kind": "text", "text": "fake"})
        self.assertEqual(clipboard.kernel32.freed, [1])
        self.assertEqual(clipboard.user32.opened, 0)

    def test_native_pointer_and_size_signatures_are_not_truncated_on_64bit(self):
        clipboard = native.WindowsClipboard.__new__(native.WindowsClipboard)
        clipboard.user32, clipboard.kernel32, clipboard.shell32 = mock.Mock(), mock.Mock(), mock.Mock()
        clipboard._bind()
        self.assertIs(clipboard.user32.GetClipboardData.restype, C.c_void_p)
        self.assertEqual(clipboard.user32.SetClipboardData.argtypes, [C.c_uint32, C.c_void_p])
        self.assertIs(clipboard.kernel32.GlobalSize.restype, C.c_size_t)
        self.assertIs(clipboard.kernel32.GlobalLock.restype, C.c_void_p)
        self.assertEqual(clipboard.kernel32.GlobalAlloc.argtypes, [C.c_uint32, C.c_size_t])
        self.assertEqual(clipboard.shell32.DragQueryFileW.argtypes, [C.c_void_p, C.c_uint32, C.c_wchar_p, C.c_uint32])

    def test_token_is_unsigned_and_no_content_is_requested(self):
        clipboard = windows()
        self.assertEqual(clipboard.get_change_token(), 0xFFFFFFFF)
        self.assertEqual(clipboard.user32.opened, 0)


class MacClipboardTests(unittest.TestCase):
    def test_objc_message_bridge_keeps_64bit_pointers_and_nsuint_arguments(self):
        clipboard = native.MacClipboard.__new__(native.MacClipboard)
        clipboard._messages, clipboard._selectors = {}, {}
        clipboard.objc = mock.Mock()
        clipboard.objc.sel_registerName.return_value = 77
        observed = []
        signature = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p, C.c_ulong)
        @signature
        def message(receiver, selector, pointer, length):
            observed.append((receiver, selector, pointer, length))
            return 0x123456789ABC
        clipboard._message_address = C.cast(message, C.c_void_p).value
        result = clipboard._send(0x111122223333, "fakeBytes:length:", C.c_void_p,
                                 (C.c_void_p, C.c_ulong), 0xAAAABBBBCCCC, 123)
        self.assertEqual(result, 0x123456789ABC)
        self.assertEqual(observed, [(0x111122223333, 77, 0xAAAABBBBCCCC, 123)])

    def test_token_only_queries_change_count(self):
        clipboard = FakeMac()
        self.assertEqual(clipboard.get_change_token(), 17)
        self.assertEqual(clipboard.calls, [("changeCount", ())])

    def test_text_read_and_write_need_no_tk_or_nsapplication_selector(self):
        clipboard = FakeMac()
        clipboard.values["text"] = "中\r\n🙂"
        self.assertEqual(clipboard.read(), {"kind": "text", "text": "中\n🙂"})
        clipboard.write({"kind": "text", "text": "fake"})
        self.assertEqual(clipboard.written, ("fake", "text"))
        self.assertFalse(any(name in ("sharedApplication", "activate", "init") for name, _values in clipboard.calls))

    def test_file_urls_are_decoded_without_opening_files(self):
        clipboard = FakeMac()
        clipboard.items = [{"file": "file:///Users/fake/a%20b.txt"}, {"file": "file://localhost/tmp/%E4%B8%AD.txt"}]
        clipboard.values["text"] = "fallback"
        self.assertEqual(clipboard.read(), {"kind": "files", "paths": [Path("/Users/fake/a b.txt"), Path("/tmp/中.txt")]})

    def test_remote_or_malformed_file_urls_are_rejected_with_fixed_error(self):
        for url in ("https://fake.invalid/a", "file://remote/a", "file:///tmp/a?x=1", "file:///tmp/a%00b"):
            clipboard = FakeMac()
            clipboard.items = [{"file": url}]
            with self.assertRaises(native.ClipboardError) as error:
                clipboard.read()
            self.assertNotIn(url, str(error.exception))

    def test_file_write_constructs_urls_before_clearing_contents(self):
        clipboard = FakeMac()
        clipboard.write({"kind": "files", "paths": ["/Users/fake/a", "/Users/fake/b"]})
        self.assertEqual(len(clipboard.written), 2)
        names = [name for name, _values in clipboard.calls]
        self.assertLess(names.index("arrayWithObjects:count:"), names.index("clearContents"))
        self.assertEqual(names.count("clearContents"), 1)

    def test_invalid_snapshot_never_clears_native_contents(self):
        clipboard = FakeMac()
        with self.assertRaises(native.ClipboardError):
            clipboard.write({"kind": "text", "text": "bad\0private"})
        self.assertEqual(clipboard.calls, [])

    def test_failed_native_write_reports_no_content(self):
        clipboard = FakeMac()
        clipboard.write_ok = False
        with self.assertRaises(native.ClipboardError) as error:
            clipboard.write({"kind": "text", "text": "private-native-test-marker"})
        self.assertNotIn("private-native-test-marker", str(error.exception))


@unittest.skipIf(Image is None, "Pillow is not installed in the isolated test environment")
class ClipboardImageTests(unittest.TestCase):
    def image_data(self, format="PNG", mode="RGBA"):
        output = BytesIO()
        with Image.new(mode, (3, 2), (40, 70, 90, 100) if mode == "RGBA" else (40, 70, 90)) as image:
            image.save(output, format=format)
        return output.getvalue()

    def test_png_and_tiff_normalize_to_bounded_png_with_alpha(self):
        for format in ("PNG", "TIFF"):
            value = native.normalize_png(self.image_data(format))
            self.assertTrue(value.startswith(native.PNG_SIGNATURE))
            with Image.open(BytesIO(value)) as image:
                self.assertEqual(image.size, (3, 2))
                self.assertEqual(image.getpixel((0, 0)), (40, 70, 90, 100))

    def test_dib_and_dibv5_roundtrip_without_files_or_real_clipboard(self):
        png, dibv5, dib = native._windows_image_formats(self.image_data())
        for data in (dibv5, dib):
            result = native.normalize_png(data, dib=True)
            with Image.open(BytesIO(result)) as image:
                self.assertEqual(image.size, (3, 2))
        self.assertEqual(struct.unpack_from("<IiiHHI", dibv5), (124, 3, -2, 1, 32, 3))
        with Image.open(BytesIO(native.normalize_png(dibv5, dib=True))) as image:
            self.assertEqual(image.getpixel((0, 0))[3], 100)

    def test_image_bounds_and_bad_headers_are_rejected(self):
        for data, dib in ((b"not an image", False), (b"x" * (native.MAX_IMAGE_BYTES + 1), False),
                          (b"\0" * 8, True), (struct.pack("<I", 999) + b"\0" * 124, True)):
            with self.assertRaises(native.ClipboardError):
                native.normalize_png(data, dib=dib)
        with mock.patch.object(native, "MAX_IMAGE_PIXELS", 5):
            with self.assertRaises(native.ClipboardError):
                native.normalize_png(self.image_data())

    def test_windows_reads_dib_and_prefers_valid_png(self):
        clipboard = windows()
        png, _dibv5, dib = native._windows_image_formats(self.image_data())
        clipboard.user32.add(native.CF_DIB, dib)
        self.assertEqual(clipboard.read()["kind"], "image")
        clipboard.user32.add(clipboard.png_format, png)
        self.assertEqual(clipboard.read()["data"], png)

    def test_windows_image_write_publishes_three_formats_and_frees_only_failed(self):
        clipboard = windows()
        clipboard.user32.fail_set = native.CF_DIB
        with self.assertRaises(native.ClipboardError):
            clipboard.write({"kind": "image", "data": self.image_data()})
        self.assertEqual([format for format, _handle in clipboard.user32.sets], [clipboard.png_format, native.CF_DIBV5])
        self.assertEqual(clipboard.kernel32.freed, [3])
        self.assertEqual(clipboard.user32.closed, 1)

    def test_mac_reads_tiff_and_writes_png(self):
        clipboard = FakeMac()
        clipboard.values["tiff"] = self.image_data("TIFF")
        snapshot = clipboard.read()
        self.assertEqual(snapshot["kind"], "image")
        self.assertTrue(snapshot["data"].startswith(native.PNG_SIGNATURE))
        clipboard.write(snapshot)
        self.assertEqual(clipboard.written[1], "png")

    def test_gui_raw_read_and_prepared_write_do_not_run_image_codecs(self):
        data = self.image_data()
        win, mac = windows(), FakeMac()
        win.user32.add(win.png_format, data)
        mac.values["tiff"] = self.image_data("TIFF")
        win_prepared = win.prepare_write({"kind": "image", "data": data})
        mac_prepared = mac.prepare_write({"kind": "image", "data": data})
        with mock.patch.object(native, "normalize_png", side_effect=AssertionError("GUI codec")), \
                mock.patch.object(native, "_windows_image_formats", side_effect=AssertionError("GUI codec")):
            self.assertEqual(win.read_raw(), {"kind": "image", "data": data, "encoding": "png"})
            self.assertEqual(mac.read_raw()["encoding"], "tiff")
            win.write_prepared(win_prepared)
            mac.write_prepared(mac_prepared)
        self.assertEqual(len(win.user32.sets), 3)
        self.assertEqual(mac.written[1], "png")

    def test_preprocessing_runs_in_background_without_any_native_calls(self):
        data = self.image_data("TIFF")
        win, mac = windows(), FakeMac()
        results, errors = [], []
        def preprocess():
            try:
                canonical = mac.prepare_snapshot({"kind": "image", "data": data, "encoding": "tiff"})
                results.extend((win.prepare_write(canonical), mac.prepare_write(canonical)))
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=preprocess)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual([value.platform for value in results], ["win32", "darwin"])
        self.assertTrue(all(value.snapshot["data"].startswith(native.PNG_SIGNATURE) for value in results))
        self.assertEqual(win.user32.opened, 0)
        self.assertEqual(mac.calls, [])


if __name__ == "__main__":
    unittest.main()
