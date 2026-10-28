"""Signed offline payloads and stable-host loading; no GUI or native input."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import trustedhost as host
from scripts.package_update import build_update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="crossdesk-update-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.manager = host.UpdateManager(self.root / "state", _platform="win32", _public_key=self.public)
        self.sources = {"crossdesk/__init__.py": b"__version__ = '1.2.3'\n",
                        "crossdesk/__main__.py": b"def main():\n    return 0\n",
                        "crossdesk/feature.py": b"value = 'new payload'\n"}

    def archive(self, *, sources=None, changes=None, key=None, extras=(), raw_manifest=None):
        sources = self.sources if sources is None else sources
        manifest = {"format": 1, "host_api": host.HOST_API, "version": "1.2.3",
                    "platforms": ["win32", "darwin"],
                    "files": {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}}
        manifest.update(changes or {})
        raw = host.canonical_json(manifest) if raw_manifest is None else raw_manifest
        memory = io.BytesIO()
        with warnings.catch_warnings(), zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("manifest.json", raw)
            archive.writestr("signature.ed25519", (key or self.key).sign(host.SIGNING_CONTEXT + raw))
            for name, data in sources.items():
                archive.writestr(name, data)
            for name, data in extras:
                archive.writestr(name, data)
        return memory.getvalue()

    def file(self, data=None, name="release.crossdesk-update"):
        path = self.root / name
        path.write_bytes(self.archive() if data is None else data)
        return path

    def verify(self, data):
        return host.verify_archive(data, platform="win32", public_key=self.public)

    def test_inspection_verifies_signed_content_without_installing(self):
        metadata = self.manager.inspect(self.file())
        self.assertEqual(metadata["version"], "1.2.3")
        self.assertEqual(metadata["files_count"], 3)
        self.assertFalse(self.manager.root.exists())

    def test_wrong_publisher_key_rejected_without_creating_state(self):
        path = self.file(self.archive(key=Ed25519PrivateKey.generate()))
        with self.assertRaisesRegex(host.UpdateError, "签名验证失败"):
            self.manager.stage(path)
        self.assertFalse(self.manager.root.exists())

    def test_modified_source_fails_digest_even_with_valid_manifest_signature(self):
        data = self.archive()
        with zipfile.ZipFile(io.BytesIO(data)) as original:
            content = {name: original.read(name) for name in original.namelist()}
        content["crossdesk/feature.py"] = b"raise RuntimeError('must never execute')\n"
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w") as archive:
            for name, value in content.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(host.UpdateError, "校验失败"):
            self.verify(memory.getvalue())

    def test_incompatible_host_platform_and_version_are_rejected(self):
        for change in ({"host_api": host.HOST_API + 1}, {"host_api": True},
                       {"platforms": ["darwin"]}, {"platforms": ["win32", "win32"]},
                       {"platforms": [[]]}, {"version": "../arbitrary"}):
            with self.subTest(change=change), self.assertRaises(host.UpdateError):
                self.verify(self.archive(changes=change))

    def test_traversal_host_modules_native_files_and_extra_members_are_rejected(self):
        for member in ("../trustedhost.py", "trustedhost.py", "crossdesk/trustedhost.py",
                       "crossdesk/runtime_host.py", "crossdesk/plugin.dylib", "crossdesk\\feature.py"):
            with self.subTest(member=member), self.assertRaises(host.UpdateError):
                self.verify(self.archive(sources={**self.sources, member: b"value = 1\n"}))
        with self.assertRaises(host.UpdateError):
            self.verify(self.archive(extras=[(".local/private-key.pem", b"private-marker")]))

    def test_symlink_duplicate_and_case_colliding_members_are_rejected(self):
        link = zipfile.ZipInfo("crossdesk/link.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extras in ([(link, b"/etc/host")], [("manifest.json", b"{}")],
                       [("CROSSDESK/feature.py", b"value=2\n")]):
            with self.subTest(extras=extras), self.assertRaises(host.UpdateError):
                self.verify(self.archive(extras=extras))

    def test_missing_package_parent_and_invalid_python_are_rejected(self):
        for sources in ({"crossdesk/__init__.py": self.sources["crossdesk/__init__.py"]},
                        {**self.sources, "crossdesk/sub/feature.py": b"value=1\n"},
                        {**self.sources, "crossdesk/feature.py": b"def broken(:\n"}):
            with self.subTest(sources=sources), self.assertRaises(host.UpdateError):
                self.verify(self.archive(sources=sources))

    def test_duplicate_json_fields_and_noncanonical_json_are_rejected(self):
        for raw in (b'{"version":"1.2.3","version":"9.9.9"}', b"{}", b"[1,2,3]"):
            with self.subTest(raw=raw), self.assertRaises(host.UpdateError):
                self.verify(self.archive(raw_manifest=raw))
        canonical = host.canonical_json({"format": 1})
        self.assertIsInstance(canonical, bytes)

    def test_size_limits_are_enforced_before_code_can_run(self):
        data = self.archive()
        for name, limit in (("MAX_ARCHIVE", len(data)-1), ("MAX_CONTENT", 10), ("MAX_FILE", 8), ("MAX_FILES", 1)):
            with self.subTest(name=name), patch.object(host, name, limit), self.assertRaises(host.UpdateError):
                self.verify(data)

    def test_staging_is_atomic_private_and_does_not_extract_sources(self):
        metadata = self.manager.stage(self.file())
        state = self.manager._state()
        self.assertEqual(state["current"], metadata["digest"])
        self.assertTrue(state["pending"])
        self.assertFalse(state["attempted"])
        self.assertFalse((self.manager.root / "crossdesk").exists())
        self.assertEqual(list(self.manager.root.rglob("*.tmp")), [])
        if os.name != "nt":
            self.assertEqual(self.manager.pointer.stat().st_mode & 0o077, 0)
            self.assertEqual(self.manager.versions.stat().st_mode & 0o077, 0)

    def test_atomic_pointer_failure_keeps_previous_selection(self):
        original = self.manager.stage(self.file())
        replacement = self.file(self.archive(changes={"version": "1.2.4"}), "next.crossdesk-update")
        replace = host.os.replace
        def fail_pointer(source, destination):
            if Path(destination) == self.manager.pointer:
                raise OSError("isolated simulated write failure")
            return replace(source, destination)
        with patch.object(host.os, "replace", side_effect=fail_pointer), self.assertRaises(OSError):
            self.manager.stage(replacement)
        self.assertEqual(self.manager._state()["current"], original["digest"])
        self.assertEqual(list(self.manager.root.rglob("*.tmp")), [])

    def confirm(self, digest):
        with patch.object(host, "_HOST_ACTIVE", True), patch.object(host, "_RUNNING_DIGEST", digest):
            self.manager.confirm_started()

    def test_confirmed_release_survives_restarts_and_next_failed_release_rolls_back(self):
        first = self.manager.stage(self.file())
        self.assertEqual(self.manager.begin_boot().metadata, {key: value for key, value in first.items() if key != "requires_restart"})
        self.confirm(first["digest"])
        self.assertFalse(self.manager._state()["pending"])
        self.assertEqual(self.manager.begin_boot().metadata["version"], "1.2.3")
        self.manager.stage(self.file(self.archive(changes={"version": "1.2.4"}), "next.crossdesk-update"))
        self.assertEqual(self.manager.begin_boot().metadata["version"], "1.2.4")
        # No confirmation: import/startup failed or the process crashed.
        self.assertEqual(self.manager.begin_boot().metadata["version"], "1.2.3")
        self.assertFalse(self.manager._state()["pending"])

    def test_old_running_app_cannot_confirm_newly_staged_payload(self):
        staged = self.manager.stage(self.file())
        self.manager.begin_boot()
        self.confirm("0" * 64)
        self.assertTrue(self.manager._state()["pending"])
        self.confirm(staged["digest"])
        self.assertFalse(self.manager._state()["pending"])

    def test_unconfirmed_first_release_and_explicit_rollback_use_baseline(self):
        self.manager.stage(self.file())
        self.assertIsNotNone(self.manager.begin_boot())
        self.assertIsNone(self.manager.begin_boot())
        self.assertIsNone(self.manager._state()["current"])
        self.manager.stage(self.file())
        self.assertEqual(self.manager.rollback(), {"version": None, "requires_restart": True})
        self.assertIsNone(self.manager.begin_boot())

    def test_corrupt_installed_release_or_pointer_never_imports_unverified_code(self):
        metadata = self.manager.stage(self.file())
        installed = self.manager.versions / (metadata["digest"] + ".crossdesk-update")
        installed.write_bytes(b"bad bytes")
        self.assertIsNone(self.manager.begin_boot())
        self.manager.pointer.write_text('{"current":"../../outside"}')
        self.assertIsNone(self.manager.begin_boot())

    def test_corrupt_pointer_can_be_repaired_by_an_explicit_signed_import(self):
        package = self.file()
        self.manager.stage(package)
        self.manager.pointer.write_text("invalid metadata")
        metadata = self.manager.stage(package)
        self.assertEqual(self.manager._state()["current"], metadata["digest"])
        self.assertIsNotNone(self.manager.begin_boot())

    def test_baseline_confirmation_does_not_parse_untrusted_selection(self):
        self.manager.stage(self.file())
        self.manager.pointer.write_text("invalid metadata")
        with patch.object(host, "_HOST_ACTIVE", True), patch.object(host, "_RUNNING_DIGEST", None):
            self.manager.confirm_started()

    def test_payload_is_not_loaded_if_attempt_marker_cannot_be_written(self):
        self.manager.stage(self.file())
        with patch.object(self.manager, "_write_state", side_effect=OSError("read-only state")):
            self.assertIsNone(self.manager.begin_boot())

    def test_reimporting_same_unconfirmed_release_repairs_content_and_restarts_attempt(self):
        package = self.file()
        metadata = self.manager.stage(package)
        self.manager.begin_boot()
        (self.manager.versions / (metadata["digest"] + ".crossdesk-update")).write_bytes(b"bad")
        self.manager.stage(package)
        self.assertFalse(self.manager._state()["attempted"])
        self.assertIsNotNone(self.manager.begin_boot())

    def test_corrupt_previous_after_failed_boot_falls_back_to_baseline(self):
        first = self.manager.stage(self.file())
        self.manager.begin_boot()
        self.confirm(first["digest"])
        self.manager.stage(self.file(self.archive(changes={"version": "1.2.4"}), "next.crossdesk-update"))
        self.manager.begin_boot()
        (self.manager.versions / (first["digest"] + ".crossdesk-update")).write_bytes(b"bad")
        self.assertIsNone(self.manager.begin_boot())

    def test_verified_in_memory_loader_overrides_baseline_and_forbids_module_mixing(self):
        sources = {**self.sources, "crossdesk/__main__.py": b"import trustedhost\nfrom .feature import value\ndef main():\n    trustedhost.UpdateManager(trustedhost.state_directory()).confirm_started()\n    print(value)\n    try:\n        import crossdesk.geometry\n    except ModuleNotFoundError:\n        print('no baseline mixing')\n    return 7\n"}
        self.manager.stage(self.file(self.archive(sources=sources)))
        script = ("import sys, trustedhost as h; "
                  f"h.TRUSTED_PUBLIC_KEY=bytes.fromhex({self.public.hex()!r}); "
                  f"sys.argv=['runtime_host','--state-dir',{str(self.manager.state_dir)!r}]; "
                  "raise SystemExit(h.main())")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(host.__file__).parent,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["new payload", "no baseline mixing"])
        self.assertFalse(self.manager._state()["pending"])

    def test_loader_uses_verified_memory_even_if_stored_archive_changes(self):
        metadata = self.manager.stage(self.file())
        payload = self.manager.begin_boot()
        (self.manager.versions / (metadata["digest"] + ".crossdesk-update")).write_bytes(b"changed after verification")
        finder = host.PayloadFinder(payload)
        spec = finder.find_spec("crossdesk.feature")
        module = host.importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.value, "new payload")
        self.assertIsNone(finder.find_spec("trustedhost"))

    def test_restart_uses_same_frozen_executable_after_cleanup(self):
        with patch.object(host.sys, "frozen", True, create=True), patch.object(host.sys, "argv", ["CrossDesk", "--state-dir", "private state"]), patch.object(host.os, "execv") as execute, patch.dict(os.environ, {}, clear=False):
            self.manager.restart()
            execute.assert_called_once_with(sys.executable, [sys.executable, "--state-dir", "private state"])
            self.assertEqual(os.environ["PYINSTALLER_RESET_ENVIRONMENT"], "1")

    def test_runtime_info_only_uses_cached_host_state(self):
        with patch.object(host, "_HOST_ACTIVE", True), patch.object(host, "_RUNNING_DIGEST", "0" * 64), patch.object(Path, "open", side_effect=AssertionError("Unexpected disk read")):
            self.assertEqual(host.runtime_info(), {"host_api": host.HOST_API, "host_active": True, "updated": True})
        with patch.object(host, "_HOST_ACTIVE", False), patch.object(host, "_RUNNING_DIGEST", None):
            self.assertFalse(host.runtime_info()["updated"])

    def test_public_packager_contains_only_business_sources(self):
        source = self.root / "source"
        (source / "crossdesk").mkdir(parents=True)
        for name, data in self.sources.items():
            (source / name).write_bytes(data)
        (source / "trustedhost.py").write_text("private host must not enter payload\n")
        (source / ".local").mkdir()
        (source / ".local/key.pem").write_text("private marker\n")
        key_path = self.root / "test-signing-key.pem"
        key_path.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        output = self.root / "public.crossdesk-update"
        metadata = build_update(source, key_path, output)
        self.assertEqual(metadata["version"], "1.2.3")
        self.assertEqual(self.manager.inspect(output)["digest"], metadata["digest"])
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("trustedhost.py", archive.namelist())
            self.assertFalse(any(".local" in name for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
