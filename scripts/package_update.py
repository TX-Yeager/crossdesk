"""Build a publisher-signed pure-Python update; private key stays in .local.

An existing stable host trusts one embedded public key. Rotating that key or
changing host/native dependencies requires a new host installation.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from trustedhost import HOST_API, SIGNING_CONTEXT, TRUSTED_PUBLIC_KEY, canonical_json, verify_archive


def build_update(source_root, key_path, destination, *, version=None, platforms=("win32", "darwin")):
    source_root, key_path, destination = Path(source_root), Path(key_path), Path(destination)
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Release key must be Ed25519")
    public_key = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sources = {}
    package = source_root / "crossdesk"
    for path in sorted(package.rglob("*.py")):
        if path.is_symlink() or any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(source_root).parts):
            raise ValueError("Payload source must not contain private or linked files")
        sources[path.relative_to(source_root).as_posix()] = path.read_bytes()
    if version is None:
        import ast
        tree = ast.parse(sources["crossdesk/__init__.py"])
        version = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                       and target.id == "__version__" for target in node.targets))
    manifest = {"format": 1, "host_api": HOST_API, "version": version, "platforms": list(platforms),
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}}
    raw = canonical_json(manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    import io
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", raw)
        archive.writestr("signature.ed25519", key.sign(SIGNING_CONTEXT + raw))
        for name, data in sources.items():
            archive.writestr(name, data)
    result = memory.getvalue()
    verify_archive(result, platform=platforms[0], public_key=public_key)
    destination.write_bytes(result)
    return {"version": version, "files_count": len(sources), "digest": hashlib.sha256(result).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description="Create an offline signed CrossDesk business update")
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--key", type=Path, default=ROOT / ".local/update-release-ed25519.pem")
    parser.add_argument("--version")
    parser.add_argument("--platform", choices=("win32", "darwin"), action="append")
    arguments = parser.parse_args()
    # CLI release keys may only come from this project's ignored private area.
    arguments.key.resolve().relative_to((ROOT / ".local").resolve())
    key = serialization.load_pem_private_key(arguments.key.read_bytes(), password=None)
    if key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) != TRUSTED_PUBLIC_KEY:
        raise ValueError("Release key does not match the permanent host public key")
    metadata = build_update(arguments.source, arguments.key, arguments.output, version=arguments.version,
                            platforms=tuple(arguments.platform or ("win32", "darwin")))
    print(f"Signed CrossDesk {metadata['version']}: {metadata['files_count']} Python modules; no host/native/private files.")


if __name__ == "__main__":
    main()
