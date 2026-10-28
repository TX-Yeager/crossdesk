param(
    [string]$Python = '',
    [string]$TclLibrary = '',
    [switch]$SkipInstall
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$VenvPath = Join-Path $ProjectRoot '.venv'
$BuildPath = Join-Path $ProjectRoot 'build'
$DistPath = Join-Path $ProjectRoot 'dist'
$EntryPoint = Join-Path $ProjectRoot 'runtime_host.py'

# Native Tcl may not be able to read the host's bundled runtime path inside a
# restricted environment. Use a caller-provided library or the project-local
# build runtime; neither option changes the user's global environment.
if (-not $TclLibrary) {
    $LocalTclLibrary = Join-Path $ProjectRoot '.runtime\tcl\tcl8.6'
    if (Test-Path -LiteralPath (Join-Path $LocalTclLibrary 'init.tcl') -PathType Leaf) {
        $TclLibrary = $LocalTclLibrary
    }
}
if ($TclLibrary) {
    $TclLibrary = (Resolve-Path -LiteralPath $TclLibrary).Path
    $TkLibrary = Join-Path (Split-Path -Parent $TclLibrary) 'tk8.6'
    if (-not (Test-Path -LiteralPath (Join-Path $TclLibrary 'init.tcl') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $TkLibrary 'tk.tcl') -PathType Leaf)) {
        throw 'TclLibrary must point to tcl8.6 with a sibling tk8.6 directory.'
    }
    $env:TCL_LIBRARY = $TclLibrary
    $env:TK_LIBRARY = $TkLibrary
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    if ($Python) {
        & $Python -c 'import sys, tkinter; assert sys.version_info >= (3, 10)'
        if ($LASTEXITCODE -ne 0) { throw 'Python 需要 3.10+ 和 tkinter。' }
        & $Python -m venv $VenvPath
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -c 'import sys, tkinter; assert sys.version_info >= (3, 10)'
        if ($LASTEXITCODE -ne 0) { throw '请安装含 Tcl/Tk 的 python.org 官方 Python 3.10+。' }
        & py -3 -m venv $VenvPath
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -c 'import sys, tkinter; assert sys.version_info >= (3, 10)'
        if ($LASTEXITCODE -ne 0) { throw '请安装含 Tcl/Tk 的 python.org 官方 Python 3.10+。' }
        & python -m venv $VenvPath
    } else {
        throw '未找到 Python。请安装 https://www.python.org/downloads/windows/ 中含 Tcl/Tk 的 Python 3.10+。'
    }
    if ($LASTEXITCODE -ne 0) { throw '创建项目 .venv 失败。' }
}

& $VenvPython -c 'import sys, tkinter; assert sys.version_info >= (3, 10)'
if ($LASTEXITCODE -ne 0) { throw '项目 .venv 需要含 tkinter 的 Python 3.10+。' }
if (-not $SkipInstall) {
    & $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot 'requirements.txt') 'pyinstaller>=6,<7'
    if ($LASTEXITCODE -ne 0) { throw '项目 .venv 依赖安装失败。' }
}
if (-not (Test-Path -LiteralPath $EntryPoint -PathType Leaf)) { throw '找不到 crossdesk/__main__.py 入口。' }
$Version = & $VenvPython -c 'from crossdesk import __version__; print(__version__)'
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+(-rc\d+)?$') { throw 'Invalid source version.' }
$StagePath = Join-Path $DistPath "v$Version"
New-Item -ItemType Directory -Path $BuildPath -Force | Out-Null

# Only the application entry and imported modules are collected. Never add the
# repository root as data: .local contains private SSH/testing material.
$BuildArguments = @(
    '-m', 'PyInstaller', '--noconfirm', '--windowed', '--onedir',
    '--name', 'CrossDesk', '--paths', $ProjectRoot,
    '--distpath', $StagePath, '--workpath', $BuildPath, '--specpath', $BuildPath,
    '--exclude-module', 'tests', '--exclude-module', 'pytest',
    '--exclude-module', 'tkinter.test',
    '--collect-submodules', 'crossdesk', '--hidden-import', 'PIL.Image',
    '--hidden-import', 'PIL.PngImagePlugin', '--hidden-import', 'PIL.BmpImagePlugin',
    '--hidden-import', 'PIL.TiffImagePlugin', '--hidden-import', 'PIL.JpegImagePlugin', $EntryPoint
)
& $VenvPython @BuildArguments
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }

$OutputPath = Join-Path $StagePath 'CrossDesk'
$UnexpectedPrivateFiles = Get-ChildItem -LiteralPath $OutputPath -Force -Recurse |
    Where-Object { $_.Name -eq '.local' -or $_.Name -in @('transport-key.pem', 'transport-secret.bin', 'update-release-ed25519.pem') } |
    Select-Object -First 1
if ($UnexpectedPrivateFiles) { throw '构建产物包含私有测试/配对文件，禁止分发。' }
Write-Host "构建完成：$(Join-Path $OutputPath 'CrossDesk.exe')"
Write-Host '请分发整个版本目录内的 CrossDesk 文件夹，保留 _internal 目录；不会替换运行中的安装。'
