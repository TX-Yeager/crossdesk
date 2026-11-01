"""Check a shipped package in empty directories without using developer runtimes.

Run on the matching OS. This is a GUI/identity/dependency check, not a replacement
for a fresh computer's OS permission, firewall, discovery and physical input test.
No installed app, firewall rule, account or user configuration is modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
import zipfile


PRIVATE = {'.local', '.venv', '.runtime', 'identity', 'network.json', 'settings.json',
           'status.json', 'transport-key.pem', 'transport-secret.bin',
           'update-release-ed25519.pem', 'mac_link_ed25519', 'known_hosts'}


def clean_environment():
    excluded = ('PYTHON', 'TCL', 'TK_', 'VIRTUAL_ENV', 'CONDA', 'DYLD_', '_PYI', 'PYINSTALLER')
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(excluded)}
    if sys.platform == 'win32':
        windows = Path(os.environ['SystemRoot'])
        env['PATH'] = os.pathsep.join(str(windows / part) for part in ('System32', '.', 'System32/Wbem'))
    else:
        env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
    return env


def extract_windows(package, work):
    with zipfile.ZipFile(package) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or '..' in path.parts or ':' in member.filename
                    or '\\' in member.filename or PRIVATE.intersection(path.parts)
                    or not path.parts or path.parts[0] != 'CrossDesk'
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unexpected package member: ' + member.filename)
        archive.extractall(work)
    return work / 'CrossDesk/CrossDesk.exe'


def extract_mac(package, work):
    mount = work / 'mounted-image'
    mount.mkdir()
    subprocess.run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse',
                    '-mountpoint', str(mount), str(package)], check=True, capture_output=True)
    app = work / 'CrossDesk.app'
    try:
        source = mount / 'CrossDesk.app'
        if not source.is_dir() or source.is_symlink():
            raise ValueError('DMG has no regular CrossDesk.app')
        subprocess.run(['/usr/bin/ditto', str(source), str(app)], check=True)
    finally:
        subprocess.run(['/usr/bin/hdiutil', 'detach', str(mount)], check=True, capture_output=True)
    for path in app.rglob('*'):
        if PRIVATE.intersection(path.relative_to(app).parts):
            raise ValueError('Private file in app: ' + str(path.relative_to(app)))
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)],
                   check=True, capture_output=True)
    return app / 'Contents/MacOS/CrossDesk'


def audit_mac_dependencies(app):
    """Reject absolute links to the builder's Python, Homebrew or home folder."""
    count = 0
    magic = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
             b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
    for path in app.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as stream:
            if stream.read(4) not in magic:
                continue
        result = subprocess.run(['/usr/bin/otool', '-L', str(path)],
                                capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines()[1:]:
            dependency = line.strip().split(' (', 1)[0]
            if dependency.startswith('/') and not dependency.startswith(('/usr/lib/', '/System/Library/')):
                raise ValueError('External library dependency: ' + dependency)
        count += 1
    if not count:
        raise ValueError('No Mach-O libraries found')
    return count


def smoke(executable, state, cwd, expected_version):
    if state.exists():
        raise ValueError('Smoke state must be fresh')
    options = {}
    if sys.platform == 'win32':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options['startupinfo'] = startup
    result = subprocess.run([str(executable), '--smoke-test', '--state-dir', str(state)],
                            cwd=cwd, env=clean_environment(), capture_output=True,
                            timeout=30, **options)
    if result.returncode:
        raise RuntimeError('Frozen GUI startup failed: ' + str(result.returncode))
    records = [json.loads(line) for line in (state / 'logs/crossdesk.jsonl').read_text(encoding='utf-8').splitlines()]
    started = [record for record in records if record.get('event') == 'app_started']
    if len(started) != 1 or started[0].get('version') != expected_version:
        raise ValueError('Frozen application version differs')
    if (state / 'network.json').exists() or (state / 'updates/current.json').exists():
        raise ValueError('Fresh smoke unexpectedly reused pairing or an installed update')
    settings = json.loads((state / 'settings.json').read_text(encoding='utf-8'))
    identity = state / 'identity'
    # These values stay in memory; never put private credentials into receipts.
    return (settings['machine_id'], (identity / 'transport-cert.pem').read_bytes(),
            (identity / 'transport-key.pem').read_bytes(), (identity / 'transport-secret.bin').read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path, help='Native Windows full ZIP or macOS DMG')
    parser.add_argument('--version', required=True)
    parser.add_argument('--work-dir', type=Path, required=True, help='New empty test directory; evidence is preserved')
    args = parser.parse_args()
    if sys.platform not in ('win32', 'darwin'):
        parser.error('Run on Windows or macOS')
    package = args.package.resolve(strict=True)
    work = args.work_dir.resolve()
    if work.exists():
        parser.error('Work directory already exists; choose a new directory')
    work.mkdir(parents=True)
    cwd = work / 'empty-working-directory'
    cwd.mkdir()
    receipt = {'version': args.version, 'platform': sys.platform, 'os_version': platform.release(),
               'architecture': platform.machine(), 'package': package.name,
               'package_sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
               'fresh_machine_test': False, 'os_permission_and_firewall_test': False,
               'native_input_test': False}
    try:
        executable = extract_windows(package, work) if sys.platform == 'win32' else extract_mac(package, work)
        if sys.platform == 'darwin':
            receipt['mach_o_files_checked'] = audit_mac_dependencies(work / 'CrossDesk.app')
            receipt['external_non_system_libraries'] = 0
        first = smoke(executable, work / '新用户 A', cwd, args.version)
        second = smoke(executable, work / 'New User B', cwd, args.version)
        if any(a == b for a, b in zip(first, second)):
            raise ValueError('Independent installations reused identity material')
        receipt.update(passed=True, clean_environment_gui_starts=2,
                       independent_machine_ids=True, independent_certificates=True,
                       independent_private_keys=True, independent_pairing_secrets=True,
                       existing_configuration_used=False, installed_app_unchanged=True)
    except Exception as exc:
        receipt.update(passed=False, failure_type=type(exc).__name__)
        raise
    finally:
        (work / 'validation.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
