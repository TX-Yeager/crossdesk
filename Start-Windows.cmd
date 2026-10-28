@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "CROSSDESK_PYTHON=%~dp0.venv\Scripts\python.exe"

if exist "%CROSSDESK_PYTHON%" goto check_runtime
where py >nul 2>&1
if errorlevel 1 goto try_python
py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>&1
if errorlevel 1 goto missing_python
py -3 -m venv ".venv"
if errorlevel 1 goto failed
goto check_runtime

:try_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
python -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>&1
if errorlevel 1 goto missing_python
python -m venv ".venv"
if errorlevel 1 goto failed

:check_runtime
"%CROSSDESK_PYTHON%" -c "import sys, tkinter; assert sys.version_info >= (3, 10)" >nul 2>&1
if errorlevel 1 goto missing_python
"%CROSSDESK_PYTHON%" -c "import cryptography, PIL; assert 44 <= int(cryptography.__version__.split('.')[0]) < 51; assert 12 <= int(PIL.__version__.split('.')[0]) < 14" >nul 2>&1
if not errorlevel 1 goto run
"%CROSSDESK_PYTHON%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 goto failed

:run
"%CROSSDESK_PYTHON%" "%~dp0runtime_host.py" %*
if errorlevel 1 goto failed
exit /b 0

:missing_python
echo 请安装含 Tcl/Tk 的 Python 3.10 或更新版本：
echo https://www.python.org/downloads/windows/
echo 安装时保留 Tcl/Tk 与 Python Launcher，然后重新打开本程序。
echo 如已有旧的 .venv，请先将它重命名，再重新运行。
pause
exit /b 1

:failed
echo CrossDesk 启动失败。请检查上面的错误和网络连接。
echo 程序只在本文件夹的 .venv 中安装依赖。
pause
exit /b 1
