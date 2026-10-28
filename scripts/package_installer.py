"""Wrap a verified release in an NSIS installer; do not replace a live host.

Requires the official NSIS 3.13 portable compiler. Build example:
  .venv/Scripts/python scripts/package_installer.py --makensis <path>/makensis.exe
Outputs are additive, in dist/releases/<version>/installers-<revision>/.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def nsis(value):
    return str(value).replace('$', '$$').replace('"', '$\\"')


def build(args):
    tree = ast.parse((ROOT / 'crossdesk/__init__.py').read_text(encoding='utf-8'))
    version = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '__version__' for t in n.targets))
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-rc\d+)?', version):
        raise ValueError('Invalid version')
    if not re.fullmatch(r'[0-9]{8}(?:-[a-z0-9]+)?', args.revision):
        raise ValueError('Invalid installer revision')
    release = ROOT / 'dist/releases' / version
    receipt = json.loads((release / 'release.json').read_text(encoding='utf-8'))
    assert receipt['version'] == version and receipt['frozen_sources_match_signed_payload']
    for name, info in receipt['artifacts'].items():
        assert Path(name).name == name
        assert digest(release / name) == info['sha256'], name
    stage = ROOT / 'dist' / ('v' + version) / ('installer-' + args.revision)
    output = release / ('installers-' + args.revision)
    if output.exists() or stage.exists():
        raise RuntimeError('Preserve previous builds: use a new --revision')
    stage.mkdir(parents=True)
    output.mkdir(parents=True)
    payload = stage / 'payload'
    payload.mkdir()
    original_hashes = {}
    forbidden = {'.local', '.venv', '.runtime', 'identity', 'network.json', 'settings.json',
                 'status.json', 'known_hosts', 'transport-key.pem', 'transport-secret.bin',
                 'update-release-ed25519.pem', 'mac_link_ed25519', 'mac_link_ed25519.pub', 'authorized_keys'}
    with zipfile.ZipFile(release / 'CrossDesk-Windows.zip') as archive:
        assert archive.testzip() is None
        assert archive.read('CrossDesk/VERSION').decode().strip() == version
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if path.parts[0] != 'CrossDesk' or '..' in path.parts or forbidden.intersection(path.parts):
                raise RuntimeError('Unsafe or private archive entry: ' + info.filename)
            if info.is_dir():
                continue
            relative = Path(*path.parts[1:])
            assert relative.parts and not relative.is_absolute() and ':' not in relative.as_posix()
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
            original_hashes[relative.as_posix()] = digest(target)
    host_hash = digest(payload / 'CrossDesk.exe')
    shutil.copy2(ROOT / 'installer/INSTALL.html', payload / 'INSTALL.html')
    for name in ('Configure-Firewall.ps1', 'Repair-Network.ps1'):
        source = (ROOT / 'installer/windows' / name).read_text(encoding='utf-8-sig')
        (payload / name).write_text(source.replace('@HOST_SHA256@', host_hash), encoding='utf-8-sig')
    files = sorted(p for p in payload.rglob('*') if p.is_file())
    install, uninstall, directories = [], [], set()
    previous = None
    for path in files:
        relative = path.relative_to(payload)
        parent = relative.parent
        if parent != previous:
            install.append('SetOutPath "$INSTDIR' + ('\\' + nsis(parent) if str(parent) != '.' else '') + '"')
            previous = parent
        install.append(f'File "{nsis(path)}"')
        uninstall.append(f'Delete "$INSTDIR\\{nsis(relative)}"')
        directories.update(p for p in relative.parents if str(p) != '.')
    install.append('SetOutPath "$INSTDIR"')
    uninstall += [f'RMDir "$INSTDIR\\{nsis(p)}"' for p in sorted(directories, key=lambda p: (-len(p.parts), str(p)))]
    for name, lines in (('files-install.nsh', install), ('files-uninstall.nsh', uninstall)):
        (stage / name).write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    setup = output / f'CrossDesk-{version}-Windows-x64-Setup.exe'
    numeric = version.split('-')[0] + '.0'
    (stage / 'config.nsh').write_text(f'!define VERSION "{version}"\n!define NUMERIC_VERSION "{numeric}"\n!define OUTPUT "{nsis(setup)}"\n', encoding='utf-8-sig')
    subprocess.run([str(Path(args.makensis).resolve()), '/V2', '/DSTAGE=' + str(stage),
                    str(ROOT / 'installer/windows/CrossDesk.nsi')], check=True)
    assert setup.stat().st_size > 1_000_000
    for name in receipt['artifacts']:
        shutil.copy2(release / name, output / name)
    shutil.copy2(release / 'release.json', output / 'application-release.json')
    shutil.copy2(ROOT / 'installer/INSTALL.html', output / 'INSTALL.html')
    source_files = sorted(p for p in (ROOT / 'installer').rglob('*') if p.is_file())
    source_files += [ROOT / 'scripts/package_installer.py', ROOT / 'tests/test_installer.ps1']
    with zipfile.ZipFile(output / 'CrossDesk-installer-source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in source_files:
            archive.write(path, path.relative_to(ROOT).as_posix())
    manifest = {'version': version, 'installer_revision': args.revision,
                'application_unchanged': True, 'preview': True,
                'windows_host_sha256': host_hash, 'payload_hashes': original_hashes,
                'firewall': {'program': '<install-dir>/CrossDesk.exe', 'profile': 'Private',
                             'remote_address': 'LocalSubnet', 'inbound': ['TCP 48193', 'UDP 48194']},
                'compiler': 'NSIS 3.13', 'unsigned_installer': True,
                'artifacts': {p.name: {'sha256': digest(p), 'bytes': p.stat().st_size}
                              for p in sorted(output.iterdir()) if p.is_file()}}
    (output / 'installer-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    checksums = [f'{digest(p)}  {p.name}' for p in sorted(output.iterdir()) if p.is_file()]
    (output / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output), 'setup_bytes': setup.stat().st_size,
                      'setup_sha256': digest(setup), 'application_files': len(original_hashes)}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--makensis', required=True)
    parser.add_argument('--revision', default='20261028')
    build(parser.parse_args())
