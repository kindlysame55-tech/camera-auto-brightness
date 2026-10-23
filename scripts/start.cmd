@echo off
rem 清掉上次可能残留的停止信号，否则新进程一起来就会自己退出
del /q "%LOCALAPPDATA%\camera-auto-brightness\stop.flag" 2>nul

rem ============================================================
rem  camera-auto-brightness 一键启动
rem
rem  双击：默认以托盘方式常驻运行（右下角托盘图标，右键可退出）
rem  用法：start.cmd 加模式名，模式可选 tray check run dry probe devices help
rem
rem  启动顺序（哪个有就用哪个）：
rem    1. 打包好的 exe：dist\CamabTray.exe / dist\Camab.exe
rem    2. 项目内虚拟环境：.venv\Scripts\python.exe -m camab
rem    3. PATH 里的 python
rem  所以「给不懂 Python 的人用」时，先跑 packaging\build_exe.ps1 打包一次。
rem
rem  【两条硬规矩，改这个文件前务必看完】
rem  1) 本文件用 GBK/ANSI 保存。不要存成 UTF-8、不要加 BOM、不要在里面 chcp，
rem     否则中文会变乱码（cmd 按控制台代码页解释这些字节）。
rem  2) 本文件**刻意一个 goto 都不用**，只用 if / else 顺序执行。
rem     实测：文件里只要有中文（多字节字符），goto :标签 就会跳到行中间，
rem     把注释后半截当命令执行（报 'heck' is not recognized 之类）。
rem     所以这里禁止 goto，也禁止 call :标签。
rem ============================================================
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem 项目根 = 本脚本所在目录的上一级（路径含全角字符，必须全程加引号）
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

rem ---- 找启动器（有 exe 用 exe，没有才退回源码）-----------------------
set "EXE_TRAY="
set "EXE_CLI="
if exist "%ROOT%\dist\CamabTray.exe"            set "EXE_TRAY=%ROOT%\dist\CamabTray.exe"
if exist "%ROOT%\dist\CamabTray\CamabTray.exe"  set "EXE_TRAY=%ROOT%\dist\CamabTray\CamabTray.exe"
if exist "%ROOT%\dist\Camab.exe"                set "EXE_CLI=%ROOT%\dist\Camab.exe"
if exist "%ROOT%\dist\Camab\Camab.exe"          set "EXE_CLI=%ROOT%\dist\Camab\Camab.exe"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem ---- 模式翻译成真正要跑的命令行（纯 ASCII 参数）---------------------
set "MODE=%~1"
if "%MODE%"=="" set "MODE=tray"
set "RUN="
set "RC=0"
if /i "%MODE%"=="tray"    set "RUN=tray"
if /i "%MODE%"=="check"   set "RUN=preflight"
if /i "%MODE%"=="run"     set "RUN=run"
if /i "%MODE%"=="dry"     set "RUN=--dry-run run"
if /i "%MODE%"=="probe"   set "RUN=probe --seconds 30"
if /i "%MODE%"=="devices" set "RUN=devices"
if /i "%MODE%"=="help"    set "RUN=help"
if /i "%MODE%"=="-h"      set "RUN=help"
if /i "%MODE%"=="--help"  set "RUN=help"

if not defined RUN (
  echo.
  echo 未知模式：%MODE%
  echo 可用模式：tray / check / run / dry / probe / devices / help
  set "RUN=help"
  set "RC=2"
)

if /i "%RUN%"=="tray" (
  if defined EXE_TRAY (
    echo 启动托盘版：%EXE_TRAY%
    start "" "%EXE_TRAY%"
  ) else (
    echo 没找到 dist\CamabTray.exe，改用 Python 源码方式启动。
    echo 这个黑窗口就是程序本体：关掉窗口等于退出程序，想看日志就别关。
    echo.
    "%PY%" -m camab tray
    if errorlevel 1 set "RC=1"
  )
) else if /i "%RUN%"=="help" (
  echo.
  echo camera-auto-brightness 一键启动
  echo.
  echo   双击本文件         托盘常驻，右下角出现图标
  echo   start.cmd check    环境自检：摄像头 + 屏幕亮度后端
  echo   start.cmd run      前台运行，看得到日志
  echo   start.cmd dry      干跑：只记录不改屏幕亮度
  echo   start.cmd probe    连续测光 30 秒
  echo   start.cmd devices  列出摄像头设备并给诊断结论
  echo   start.cmd help     这份帮助
  echo.
  echo 全部命令行参数：dist\Camab.exe --help
  echo 开机自启：packaging\install_autostart.ps1 加 -Remove 可取消
  echo.
  pause
) else if defined EXE_CLI (
  "%EXE_CLI%" %RUN%
  if errorlevel 1 set "RC=1"
  echo.
  pause
) else (
  echo 没找到 dist\Camab.exe，改用 Python 源码方式（.venv）。
  echo.
  "%PY%" -m camab %RUN%
  if errorlevel 1 set "RC=1"
  echo.
  pause
)

if not "%RC%"=="0" (
  echo.
  echo [退出码 %RC%] 出错了，把上面的内容截图发给开发者。
  pause
)
endlocal & exit /b %RC%
