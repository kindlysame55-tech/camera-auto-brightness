@echo off
rem ============================================================
rem  camab 命令包装：先切到 UTF-8 代码页，避免中文乱码
rem  用法：scripts\camab.cmd preflight --no-camera
rem        scripts\camab.cmd devices
rem        scripts\camab.cmd --dry-run run
rem ============================================================
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0.."

set "EXE=%~dp0..\dist\Camab.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"

if exist "%EXE%" (
  "%EXE%" %*
) else if exist "%PY%" (
  "%PY%" -m camab %*
) else (
  python -m camab %*
)

endlocal & exit /b %errorlevel%
