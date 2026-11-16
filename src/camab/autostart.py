"""跨平台开机自启动管理（仅动当前用户，无需管理员权限）。

* Windows: 写入 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 的 ``CamabTray`` 项，
  与 ``packaging/install_autostart.ps1`` 保持完全兼容。
* Linux: 写入 ``$XDG_CONFIG_HOME/autostart/camera-auto-brightness.desktop``。
* macOS: 写入 ``~/Library/LaunchAgents/tech.kindlysame55.camera-auto-brightness.plist``。
"""

from __future__ import annotations

import os
import plistlib
import shlex
import sys
from pathlib import Path
from typing import Optional

APP_ID = "camera-auto-brightness"
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_VALUE_NAME = "CamabTray"
MAC_LABEL = "tech.kindlysame55.camera-auto-brightness"


def _windows_startup_shortcut() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "摄像头自动亮度.lnk"


def _linux_desktop_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "autostart" / f"{APP_ID}.desktop"


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def resolve_launch_argv(executable: Optional[str] = None,
                        frozen: Optional[bool] = None) -> list[str]:
    """解析当前运行环境对应的托盘启动命令参数列表。"""
    exe = Path(executable or sys.executable).resolve()
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)

    if sys.platform == "win32":
        if is_frozen:
            for candidate in (
                exe.parent / "start-camab.exe",
                exe.parent.parent / "start-camab.exe",
                exe.parent.parent.parent / "start-camab.exe",
                exe.parent / "CamabTray.exe",
                exe,
            ):
                if candidate.is_file():
                    return [str(candidate)]
            return [str(exe)]
        from .winutil import python_launcher

        pyw = python_launcher() if executable is None else str(exe)
        return [pyw, "-m", "camab", "tray"]

    if is_frozen:
        if sys.platform == "darwin":
            # Inside Camera Auto Brightness.app/Contents/MacOS/CamabTray
            tray = exe.parent / "CamabTray"
            return [str(tray if tray.is_file() else exe)]
        tray = exe.parent / "CamabTray"
        return [str(tray if tray.is_file() else exe)]

    return [str(exe), "-m", "camab", "tray"]


def format_windows_command(argv: list[str]) -> str:
    """把命令行参数格式化为 Windows Run 注册表字符串。"""
    parts = [f'"{argv[0]}"']
    for arg in argv[1:]:
        if any(ch in arg for ch in (' ', '\t', '"')) or not arg:
            escaped = arg.replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def is_supported() -> bool:
    return sys.platform == "win32" or sys.platform.startswith("linux") or sys.platform == "darwin"


def is_enabled() -> bool:
    """检查当前用户是否已配置开机自启动。"""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, WIN_VALUE_NAME)
                if str(val).strip():
                    return True
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return _windows_startup_shortcut().is_file()

    if sys.platform.startswith("linux"):
        return _linux_desktop_path().is_file()

    if sys.platform == "darwin":
        return _macos_plist_path().is_file()

    return False


def describe_target() -> str:
    """返回当前已注册或将要注册的自启目标描述。"""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, WIN_VALUE_NAME)
                if str(val).strip():
                    return str(val)
        except OSError:
            pass
        lnk = _windows_startup_shortcut()
        if lnk.is_file():
            return str(lnk)
        return format_windows_command(resolve_launch_argv())

    if sys.platform.startswith("linux"):
        return str(_linux_desktop_path())

    if sys.platform == "darwin":
        return str(_macos_plist_path())

    return ""


def set_enabled(enabled: bool) -> bool:
    """开启或关闭当前用户的开机自启动，返回操作后的状态。"""
    want = bool(enabled)
    if sys.platform == "win32":
        import winreg

        if want:
            cmd = format_windows_command(resolve_launch_argv())
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, WIN_VALUE_NAME, 0, winreg.REG_SZ, cmd)
            return True

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, WIN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        lnk = _windows_startup_shortcut()
        if lnk.is_file():
            lnk.unlink()
        return False

    if sys.platform.startswith("linux"):
        desktop = _linux_desktop_path()
        if want:
            desktop.parent.mkdir(parents=True, exist_ok=True)
            exec_line = " ".join(shlex.quote(a) for a in resolve_launch_argv())
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=摄像头自动亮度\n"
                "Name[en]=Camera Auto Brightness\n"
                f"Exec={exec_line}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            desktop.write_text(content, encoding="utf-8")
            return True
        if desktop.is_file():
            desktop.unlink()
        return False

    if sys.platform == "darwin":
        plist = _macos_plist_path()
        if want:
            plist.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "Label": MAC_LABEL,
                "ProgramArguments": resolve_launch_argv(),
                "RunAtLoad": True,
                "KeepAlive": False,
            }
            with plist.open("wb") as fh:
                plistlib.dump(payload, fh)
            return True
        if plist.is_file():
            plist.unlink()
        return False

    raise OSError(f"当前平台 {sys.platform} 暂不支持开机自启动")
