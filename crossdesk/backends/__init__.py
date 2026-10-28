import sys


def native_backend():
    if sys.platform == "win32":
        from .windows import WindowsBackend
        return WindowsBackend()
    if sys.platform == "darwin":
        from .macos import MacOSBackend
        return MacOSBackend()
    raise RuntimeError("当前版本支持 Windows 10/11 和 macOS。")
