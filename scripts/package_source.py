"""Explicit release whitelist. Local identities and runtime caches never enter it."""
from pathlib import Path
import io
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
files=[ROOT/name for name in ('README.md','AGENTS.md','requirements.txt','pyproject.toml','LICENSE','Start-Windows.cmd','Start-Mac.command','runtime_host.py','trustedhost.py')]
files += sorted((ROOT/'crossdesk').rglob('*.py'))
files += sorted((ROOT/'tests').rglob('*.py'))
files += [ROOT/'scripts'/name for name in ('build_windows.ps1','build_macos.sh','package_source.py','package_update.py','package_windows.py')]
files += [ROOT/'scripts/package_installer.py', ROOT/'tests/test_installer.ps1']
files += sorted(path for path in (ROOT/'installer').rglob('*') if path.is_file())
for path in files:
    parts=path.relative_to(ROOT).parts
    assert not any(p.startswith('.') or p=='__pycache__' for p in parts)
    assert path.is_file(), path
(ROOT/'dist').mkdir(exist_ok=True)
with tarfile.open(ROOT/'dist/CrossDesk-source.tar.gz','w:gz') as archive, zipfile.ZipFile(ROOT/'dist/CrossDesk-source.zip','w',zipfile.ZIP_DEFLATED) as z:
    for path in files:
        relative=path.relative_to(ROOT).as_posix()
        data=path.read_bytes()
        if path.suffix in ('.sh','.command'):
            data=data.replace(b'\r\n',b'\n')
        info=tarfile.TarInfo(relative)
        info.size=len(data)
        info.mtime=int(path.stat().st_mtime)
        info.mode=0o755 if path.suffix in ('.sh','.command') else 0o644
        archive.addfile(info,io.BytesIO(data))
        z.writestr(relative,data)
print(f'Packaged {len(files)} source files; private state and SSH files excluded.')
