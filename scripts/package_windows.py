"""Package the versioned Windows build, never the running installation."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT=Path(__file__).resolve().parents[1]
tree=ast.parse((ROOT/'crossdesk/__init__.py').read_text(encoding='utf-8'))
VERSION=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
             and any(isinstance(target,ast.Name) and target.id=='__version__' for target in node.targets))
BUNDLE=ROOT/'dist'/('v'+VERSION)/'CrossDesk'
RELEASE=ROOT/'dist/releases'/VERSION
if not (BUNDLE/'CrossDesk.exe').is_file():raise RuntimeError('Build the versioned Windows bundle first')
RELEASE.mkdir(parents=True,exist_ok=True)
for name in ('README.md','LICENSE'):
    shutil.copy2(ROOT/name,BUNDLE/name)
(BUNDLE/'VERSION').write_text(VERSION+'\n',encoding='ascii')
files=sorted(path for path in BUNDLE.rglob('*') if path.is_file())
for path in files:
    parts=path.relative_to(BUNDLE).parts
    if any(part in ('.local','.venv','.runtime','identity','network.json','settings.json','status.json',
                    'known_hosts','transport-key.pem','transport-secret.bin','update-release-ed25519.pem',
                    'mac_link_ed25519','mac_link_ed25519.pub','authorized_keys') for part in parts):
        raise RuntimeError('Private state in Windows build; refusing to package')
destination=RELEASE/'CrossDesk-Windows.zip'
temporary=destination.with_suffix('.tmp.zip')
with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
    for path in files:archive.write(path,(Path('CrossDesk')/path.relative_to(BUNDLE)).as_posix())
with zipfile.ZipFile(temporary) as archive:
    if archive.testzip() is not None:raise RuntimeError('Windows ZIP CRC check failed')
    if archive.read('CrossDesk/VERSION').decode().strip()!=VERSION:raise RuntimeError('Version mismatch')
temporary.replace(destination)
shutil.copy2(destination,ROOT/'dist/CrossDesk-Windows.zip')
for name in ('CrossDesk-source.zip','CrossDesk-source.tar.gz'):
    source=ROOT/'dist'/name
    if source.exists():shutil.copy2(source,RELEASE/name)
print(json.dumps({'version':VERSION,'archive':str(destination),'files':len(files),
                  'bytes':destination.stat().st_size,'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
                  'crc_verified':True,'private_files':0}))
