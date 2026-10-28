#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
venv_python="$project_root/.venv/bin/python3"
base_python="${CROSSDESK_PYTHON:-python3}"

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ "$(uname -s)" == "Darwin" ]] || fail "macOS 应用必须在 Mac 上构建。"
if [[ ! -x "$venv_python" ]]; then
    command -v "$base_python" >/dev/null 2>&1 || fail "请安装 python.org 官方 Python 3.10+：https://www.python.org/downloads/macos/"
    "$base_python" -c 'import sys, tkinter; assert sys.version_info >= (3, 10)' || fail "Python 需要 3.10+ 和 tkinter。请使用 python.org 官方安装包，不要修改系统 Python。"
    "$base_python" -m venv "$project_root/.venv"
fi
"$venv_python" -c 'import sys, tkinter; assert sys.version_info >= (3, 10)' || fail "项目 .venv 缺少 tkinter 或 Python 版本过旧。"
"$venv_python" -m pip install --disable-pip-version-check -r "$project_root/requirements.txt" 'pyinstaller>=6,<7'
mkdir -p "$project_root/build"
version="$("$venv_python" -c 'from crossdesk import __version__; print(__version__)')"

# AppKit/CoreGraphics/Objective-C load from the target macOS system frameworks.
# Do not copy system frameworks or local state into the permanent host.
# No --add-data of the project root: .local contains SSH/testing secrets.
"$venv_python" -m PyInstaller.utils.cliutils.makespec --windowed --onedir \
    --name CrossDesk --osx-bundle-identifier app.crossdesk.local \
    --paths "$project_root" --specpath "$project_root/build" \
    --exclude-module tests --exclude-module pytest --exclude-module tkinter.test \
  --collect-submodules crossdesk --hidden-import PIL.Image \
  --hidden-import PIL.PngImagePlugin --hidden-import PIL.BmpImagePlugin \
  --hidden-import PIL.TiffImagePlugin --hidden-import PIL.JpegImagePlugin \
    "$project_root/runtime_host.py"
"$venv_python" - "$project_root/build/CrossDesk.spec" "$version" <<'PY'
import ast, pathlib, sys
path = pathlib.Path(sys.argv[1])
source = path.read_text()
needle = '    bundle_identifier='
if source.count(needle) != 1:
    raise RuntimeError('Unexpected PyInstaller bundle spec')
source = source.replace(needle, '    version=' + repr(sys.argv[2]) + ',\n' + needle, 1)
ast.parse(source)
path.write_text(source)
PY
"$venv_python" -m PyInstaller --noconfirm --distpath "$project_root/dist/v$version" \
    --workpath "$project_root/build" "$project_root/build/CrossDesk.spec"

bundle="$project_root/dist/v$version/CrossDesk.app"
[[ -d "$bundle" ]] || fail "未找到构建产物 CrossDesk.app。"
if find "$bundle" \( -name .local -o -name transport-key.pem -o -name transport-secret.bin -o -name update-release-ed25519.pem \) -print -quit | grep -q .; then
    fail "构建产物包含私有测试/配对文件，禁止分发。"
fi
printf '%s\n' "构建完成：$bundle" "仅分发 CrossDesk.app。请在目标 Mac 重新授予辅助功能和输入监控权限。"
