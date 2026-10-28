"""Stable, frozen update trust boundary. Never include this file in a payload.

Only signed pure Python ``crossdesk`` modules can replace the bundled baseline.
The executable, this verifier, native libraries and public key remain in the
installed host. This is signature verification, not a sandbox for signed code.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.abc
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import uuid
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

HOST_API = 1
TRUSTED_PUBLIC_KEY = bytes.fromhex("a5ca89f9bd48fe3d921f3355a987300580ffd9d436f9b5cc6eda32d7ef66a772")
SIGNING_CONTEXT = b"CrossDesk signed update v1\0"
MAX_ARCHIVE = 24 * 1024 * 1024
MAX_CONTENT = 16 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_FILES = 256
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z")
_MEMBER = re.compile(r"crossdesk/(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*\.py\Z")
_LOCK = threading.RLock()
_RUNNING_DIGEST: str | None = None
_HOST_ACTIVE = False


class UpdateError(ValueError):
    """A fixed, input-free failure suitable for the local update dialog."""


def _baseline_state():
    return {"format": 1, "current": None, "previous": None, "pending": False, "attempted": False}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("更新元数据含重复字段。")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError("更新元数据无效。") from None


def _private_directory(path):
    if path.is_symlink():
        raise UpdateError("更新目录不能是符号链接。")
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _atomic(path, data):
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _read_bounded(path, limit):
    with Path(path).open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise UpdateError("更新文件超过大小限制。")
    return value


@dataclass(frozen=True)
class VerifiedPayload:
    metadata: dict
    sources: dict[str, bytes]
    archive: bytes


def verify_archive(data, *, platform=None, public_key=None):
    """Check every member before returning any executable source bytes."""
    platform = platform or sys.platform
    if len(data) > MAX_ARCHIVE:
        raise UpdateError("更新文件超过大小限制。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not 3 <= len(entries) <= MAX_FILES + 2:
                raise UpdateError("更新文件数量无效。")
            names = [entry.filename for entry in entries]
            if len({name.casefold() for name in names}) != len(names):
                raise UpdateError("更新包含重复文件。")
            if "manifest.json" not in names or "signature.ed25519" not in names:
                raise UpdateError("更新包缺少发布签名。")
            total = 0
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.flag_bits & 1 or entry.is_dir() or stat.S_ISLNK(mode)
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise UpdateError("更新包包含不支持的文件类型。")
                limit = 65536 if entry.filename == "manifest.json" else 64 if entry.filename == "signature.ed25519" else MAX_FILE
                if not 0 < entry.file_size <= limit:
                    raise UpdateError("更新包成员大小无效。")
                total += entry.file_size
            if total > MAX_CONTENT:
                raise UpdateError("更新内容超过大小限制。")
            raw_manifest = archive.read("manifest.json")
            manifest = _json(raw_manifest)
            if (not isinstance(manifest, dict) or set(manifest) != {"format", "host_api", "version", "platforms", "files"}
                    or type(manifest["format"]) is not int or manifest["format"] != 1
                    or type(manifest["host_api"]) is not int or manifest["host_api"] != HOST_API
                    or not isinstance(manifest["version"], str) or len(manifest["version"]) > 64
                    or not _VERSION.fullmatch(manifest["version"])
                    or not isinstance(manifest["platforms"], list)
                    or not manifest["platforms"] or len(set(manifest["platforms"])) != len(manifest["platforms"])
                    or any(item not in ("win32", "darwin") for item in manifest["platforms"])
                    or platform not in manifest["platforms"]
                    or not isinstance(manifest["files"], dict)):
                raise UpdateError("更新版本、系统或宿主接口不兼容。")
            if canonical_json(manifest) != raw_manifest:
                raise UpdateError("更新元数据不是规范格式。")
            signature = archive.read("signature.ed25519")
            if len(signature) != 64:
                raise UpdateError("更新签名无效。")
            try:
                Ed25519PublicKey.from_public_bytes(public_key or TRUSTED_PUBLIC_KEY).verify(
                    signature, SIGNING_CONTEXT + raw_manifest)
            except (InvalidSignature, ValueError):
                raise UpdateError("更新签名验证失败；未安装任何代码。") from None
            files = manifest["files"]
            if (set(names) != set(files) | {"manifest.json", "signature.ed25519"}
                    or not {"crossdesk/__init__.py", "crossdesk/__main__.py"} <= set(files)):
                raise UpdateError("更新文件清单不完整。")
            sources = {}
            for name, digest in files.items():
                if (not _MEMBER.fullmatch(name) or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                        or any(part.casefold() in ("trustedhost.py", "runtime_host.py") for part in name.split("/"))):
                    raise UpdateError("更新包不能替换宿主、密钥或原生依赖。")
                source = archive.read(name)
                if hashlib.sha256(source).hexdigest() != digest:
                    raise UpdateError("更新文件校验失败；未安装任何代码。")
                try:
                    compile(source, name, "exec", dont_inherit=True)
                except (SyntaxError, ValueError, OverflowError, RecursionError):
                    raise UpdateError("更新代码不兼容当前 Python 宿主。") from None
                sources[name] = source
            # Every package parent must be present: never mix a partial payload
            # with frozen baseline modules or namespace packages.
            for name in sources:
                parent = Path(name).parent
                while parent.as_posix() != ".":
                    if parent.as_posix() + "/__init__.py" not in sources:
                        raise UpdateError("更新包缺少 Python 包入口。")
                    parent = parent.parent
    except UpdateError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, zipfile.BadZipFile, EOFError):
        raise UpdateError("更新包损坏或格式无效。") from None
    metadata = {"version": manifest["version"], "host_api": HOST_API,
                "platforms": list(manifest["platforms"]), "files_count": len(sources),
                "digest": hashlib.sha256(data).hexdigest()}
    return VerifiedPayload(metadata, sources, data)


class UpdateManager:
    def __init__(self, state_dir, *, _platform=None, _public_key=None):
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "updates"
        self.versions = self.root / "versions"
        self.pointer = self.root / "current.json"
        self.platform = _platform or sys.platform
        self._public_key = _public_key

    def _directories(self):
        _private_directory(self.root)
        _private_directory(self.versions)

    def _state(self):
        try:
            value = _json(_read_bounded(self.pointer, 4096))
        except FileNotFoundError:
            return _baseline_state()
        if (not isinstance(value, dict) or set(value) != {"format", "current", "previous", "pending", "attempted"}
                or type(value["format"]) is not int or value["format"] != 1 or not isinstance(value["pending"], bool)
                or not isinstance(value["attempted"], bool)
                or any(item is not None and (not isinstance(item, str) or not _DIGEST.fullmatch(item))
                       for item in (value["current"], value["previous"]))):
            raise UpdateError("更新选择记录无效；将使用内置版本。")
        return value

    def _write_state(self, value):
        self._directories()
        _atomic(self.pointer, canonical_json(value))

    def _payload(self, digest):
        data = _read_bounded(self.versions / (digest + ".crossdesk-update"), MAX_ARCHIVE)
        if hashlib.sha256(data).hexdigest() != digest:
            raise UpdateError("已安装更新的内容已变化。")
        return verify_archive(data, platform=self.platform, public_key=self._public_key)

    def inspect(self, path):
        return dict(verify_archive(_read_bounded(path, MAX_ARCHIVE), platform=self.platform,
                                   public_key=self._public_key).metadata)

    def stage(self, path):
        payload = verify_archive(_read_bounded(path, MAX_ARCHIVE), platform=self.platform,
                                 public_key=self._public_key)
        with _LOCK:
            self._directories()
            try:
                state = self._state()
            except UpdateError:
                state = _baseline_state()
            digest = payload.metadata["digest"]
            _atomic(self.versions / (digest + ".crossdesk-update"), payload.archive)
            if digest != state["current"] or state["pending"]:
                previous = state["previous"] if state["pending"] else state["current"]
                self._write_state({"format": 1, "current": digest, "previous": previous,
                                   "pending": True, "attempted": False})
        return {**payload.metadata, "requires_restart": True}

    def status(self):
        with _LOCK:
            state = self._state()
            current = self._payload(state["current"]).metadata if state["current"] else None
            previous = self._payload(state["previous"]).metadata if state["previous"] else None
        return {"host_api": HOST_API, "host_active": _HOST_ACTIVE,
                "current": current, "previous": previous, "pending": state["pending"],
                "running_digest": _RUNNING_DIGEST}

    def rollback(self):
        with _LOCK:
            try:
                state = self._state()
                previous = state["previous"]
                payload = self._payload(previous) if previous else None
            except (OSError, UpdateError):
                previous, payload = None, None
            self._write_state({"format": 1, "current": previous, "previous": None,
                               "pending": False, "attempted": False})
        return {"version": payload.metadata["version"] if payload else None, "requires_restart": True}

    def begin_boot(self):
        """Select verified bytes; a prior unconfirmed boot rolls back first."""
        with _LOCK:
            try:
                state = self._state()
            except (OSError, UpdateError):
                return None
            if state["pending"] and state["attempted"]:
                try:
                    self.rollback()
                except (OSError, UpdateError):
                    return None
                state = self._state()
            if not state["current"]:
                return None
            try:
                payload = self._payload(state["current"])
            except (OSError, UpdateError):
                # Never import corrupt code. Try the signed last known version;
                # if it too is invalid, the installed baseline remains available.
                try:
                    self.rollback()
                    state = self._state()
                    return self._payload(state["current"]) if state["current"] else None
                except (OSError, UpdateError):
                    return None
            if state["pending"]:
                try:
                    self._write_state({**state, "attempted": True})
                except (OSError, UpdateError):
                    return None  # Do not run a release without its rollback marker.
            return payload

    def confirm_started(self):
        if not _HOST_ACTIVE or _RUNNING_DIGEST is None:
            return
        with _LOCK:
            state = self._state()
            if (_HOST_ACTIVE and _RUNNING_DIGEST is not None and state["current"] == _RUNNING_DIGEST
                    and state["pending"] and state["attempted"]):
                self._write_state({**state, "pending": False, "attempted": False})

    def restart(self):
        # Call only after App.close() has released input and network resources.
        arguments = [sys.executable]
        if not getattr(sys, "frozen", False):
            arguments.append(str(Path(__file__).with_name("runtime_host.py")))
        # PyInstaller 6+ recommends a fresh bootloader environment for app
        # restarts, including when the permanent executable path is unchanged.
        os.environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        os.execv(sys.executable, [*arguments, *sys.argv[1:]])


class _PayloadLoader(importlib.abc.Loader):
    def __init__(self, source, origin):
        self.source, self.origin = source, origin

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(self.source, self.origin, "exec", dont_inherit=True), module.__dict__)


class PayloadFinder(importlib.abc.MetaPathFinder):
    """Load only the verified in-memory crossdesk tree before frozen finders."""
    def __init__(self, payload):
        self.sources = payload.sources
        self.origin = "crossdesk-update:" + payload.metadata["digest"]

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "crossdesk" and not fullname.startswith("crossdesk."):
            return None
        relative = fullname.replace(".", "/")
        package = relative + "/__init__.py"
        member = package if package in self.sources else relative + ".py"
        if member not in self.sources:
            raise ModuleNotFoundError("Module is not part of the verified CrossDesk payload", name=fullname)
        loader = _PayloadLoader(self.sources[member], self.origin + "/" + member)
        spec = importlib.util.spec_from_loader(fullname, loader, origin=loader.origin,
                                               is_package=member == package)
        spec.has_location = True
        return spec


def state_directory(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    for index, argument in enumerate(arguments):
        if argument.startswith("--state-dir="):
            return Path(argument.split("=", 1)[1])
        if argument == "--state-dir" and index + 1 < len(arguments):
            return Path(arguments[index + 1])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CrossDesk"
    return Path.home() / "Library/Application Support/CrossDesk"


def runtime_info():
    """Cached host identity for private diagnostics; no disk reads or hashes."""
    return {"host_api": HOST_API, "host_active": bool(_HOST_ACTIVE),
            "updated": bool(_HOST_ACTIVE and _RUNNING_DIGEST is not None)}


def main():
    global _HOST_ACTIVE, _RUNNING_DIGEST
    _HOST_ACTIVE = True
    manager = UpdateManager(state_directory())
    payload = manager.begin_boot()
    if payload:
        _RUNNING_DIGEST = payload.metadata["digest"]
        sys.meta_path.insert(0, PayloadFinder(payload))
    # PyInstaller builds must collect crossdesk submodules explicitly. Do not
    # import any baseline package before installing the verified finder above.
    try:
        from crossdesk.__main__ import main as application_main
        result = application_main()
    except BaseException:
        # Leave attempted=true. A crash or a failed import is rolled back on
        # the next launch without trying to continue a partly initialized GUI.
        raise
    return result
