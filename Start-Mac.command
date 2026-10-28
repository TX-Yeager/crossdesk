#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

fail() {
    printf '%s\n' "$1" >&2
    read -r -p "按回车关闭窗口……" _ || true
    exit 1
}

venv_python="$PWD/.venv/bin/python3"
if [[ ! -x "$venv_python" ]]; then
    command -v python3 >/dev/null 2>&1 || fail "请先安装 python.org 官方 Python 3.10+：https://www.python.org/downloads/macos/"
    python3 -c 'import sys, tkinter; assert sys.version_info >= (3, 10)' >/dev/null 2>&1 || fail "当前 python3 缺少 tkinter 或版本太旧。请安装含 Tcl/Tk 的 python.org 官方 Python，不要修改系统 Python。"
    python3 -m venv "$PWD/.venv" || fail "无法在当前文件夹创建 .venv，请检查文件夹是否可写。"
fi

"$venv_python" -c 'import sys, tkinter; assert sys.version_info >= (3, 10)' >/dev/null 2>&1 || fail "本地 .venv 缺少 tkinter 或 Python 版本不符。安装 python.org 官方 Python 后，将本文件夹的 .venv 重命名并重新启动。"
if ! "$venv_python" -c 'import cryptography, PIL; assert 44 <= int(cryptography.__version__.split(".")[0]) < 51; assert 12 <= int(PIL.__version__.split(".")[0]) < 14' >/dev/null 2>&1; then
    "$venv_python" -m pip install --disable-pip-version-check -r "$PWD/requirements.txt" || fail "依赖安装失败，请检查网络连接。只会在本文件夹的 .venv 中安装。"
fi

"$venv_python" "$PWD/runtime_host.py" "$@" || fail "CrossDesk 退出时发生错误，请查看上面的提示。"
