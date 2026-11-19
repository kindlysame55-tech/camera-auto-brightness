"""Tests for cross-platform autostart logic, UI toggles, tray menu, and CLI."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from camab import autostart
from camab.__main__ import main
from camab.config import AppConfig
from camab.tray import TrayApp
from camab.ui_theme import FIELD, PRIMARY


class _FakeWinReg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    class _Key:
        def __init__(self, store: dict, path: str) -> None:
            self.store = store
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def OpenKey(self, _root, subkey, _res=0, _access=0):
        if not any(k[0] == subkey for k in self.values):
            raise FileNotFoundError(subkey)
        return self._Key(self.values, subkey)

    def CreateKeyEx(self, _root, subkey, _res=0, _access=0):
        return self._Key(self.values, subkey)

    def QueryValueEx(self, key, name):
        pair = (key.path, name)
        if pair not in key.store:
            raise FileNotFoundError(name)
        return key.store[pair], self.REG_SZ

    def SetValueEx(self, key, name, _res, _typ, val):
        key.store[(key.path, name)] = val

    def DeleteValue(self, key, name):
        pair = (key.path, name)
        if pair not in key.store:
            raise FileNotFoundError(name)
        del key.store[pair]


def test_windows_autostart_roundtrip_and_shortcut_cleanup(tmp_path, monkeypatch):
    fake_reg = _FakeWinReg()
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake_reg)
    appdata = tmp_path / "roaming"
    shortcut = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "摄像头自动亮度.lnk"
    shortcut.parent.mkdir(parents=True)
    shortcut.write_text("legacy", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(autostart, "resolve_launch_argv", lambda: [str(tmp_path / "start-camab.exe")])

    # Legacy Startup shortcut alone counts as enabled and gets removed on disable.
    assert autostart.is_enabled() is True
    assert autostart.set_enabled(False) is False
    assert not shortcut.exists()
    assert autostart.is_enabled() is False

    assert autostart.set_enabled(True) is True
    assert autostart.is_enabled() is True
    assert fake_reg.values[(autostart.WIN_RUN_KEY, autostart.WIN_VALUE_NAME)] == f'"{tmp_path / "start-camab.exe"}"'

    assert autostart.set_enabled(False) is False
    assert autostart.is_enabled() is False
    assert (autostart.WIN_RUN_KEY, autostart.WIN_VALUE_NAME) not in fake_reg.values


def test_resolve_launch_argv_prefers_launcher_for_frozen_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    root = tmp_path / "app"
    dist_camab = root / "dist" / "Camab"
    dist_camab.mkdir(parents=True)
    tray_exe = dist_camab / "CamabTray.exe"
    tray_exe.write_bytes(b"")
    launcher = root / "start-camab.exe"
    launcher.write_bytes(b"")

    assert autostart.resolve_launch_argv(str(tray_exe), frozen=True) == [str(launcher)]

    launcher.unlink()
    cli_exe = dist_camab / "Camab.exe"
    cli_exe.write_bytes(b"")
    assert autostart.resolve_launch_argv(str(cli_exe), frozen=True) == [str(tray_exe)]


def test_linux_and_macos_autostart_files(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "resolve_launch_argv", lambda: ["/opt/camab/CamabTray"])

    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert autostart.is_enabled() is False
    assert autostart.set_enabled(True) is True
    desktop = tmp_path / ".config" / "autostart" / "camera-auto-brightness.desktop"
    assert desktop.is_file()
    assert "Exec=/opt/camab/CamabTray" in desktop.read_text(encoding="utf-8")
    assert autostart.set_enabled(False) is False
    assert not desktop.exists()

    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    assert autostart.is_enabled() is False
    assert autostart.set_enabled(True) is True
    plist = tmp_path / "Library" / "LaunchAgents" / f"{autostart.MAC_LABEL}.plist"
    assert plist.is_file()
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == ["/opt/camab/CamabTray"]
    assert data["RunAtLoad"] is True
    assert autostart.set_enabled(False) is False
    assert not plist.exists()


def test_main_button_tray_menu_and_settings_stay_synced(tk_window, monkeypatch):
    state = {"enabled": False}
    monkeypatch.setattr(autostart, "is_supported", lambda: True)
    monkeypatch.setattr(autostart, "is_enabled", lambda: state["enabled"])
    monkeypatch.setattr(autostart, "set_enabled", lambda v: state.__setitem__("enabled", bool(v)) or state["enabled"])

    app = TrayApp(AppConfig())
    app._win = tk_window
    app._build_ui()
    try:
        assert "已关闭" in app.btn_autostart.cget("text")
        assert app.btn_autostart.cget("bg") == FIELD

        app.toggle_autostart()
        assert state["enabled"] is True
        assert "已开启" in app.btn_autostart.cget("text")
        assert app.btn_autostart.cget("bg") == PRIMARY

        panel_win = app.open_settings()
        assert panel_win is not None
        panel = app._settings_panel
        assert panel.var_autostart.get() is True

        panel.var_autostart.set(False)
        panel.save()
        assert state["enabled"] is False
        assert "已关闭" in app.btn_autostart.cget("text")
    finally:
        app._shutdown()


def test_settings_autostart_failure_keeps_dialog_open(tk_window, monkeypatch):
    monkeypatch.setattr(autostart, "is_supported", lambda: True)
    monkeypatch.setattr(autostart, "is_enabled", lambda: False)

    def fail(_val):
        raise OSError("access denied")

    monkeypatch.setattr(autostart, "set_enabled", fail)

    app = TrayApp(AppConfig())
    app._win = tk_window
    app._build_ui()
    try:
        app.open_settings()
        panel = app._settings_panel
        panel.var_autostart.set(True)
        panel.save()
        assert panel.win.winfo_exists()
        assert "开机自启失败" in panel.error.cget("text")
    finally:
        app._shutdown()


def test_tray_menu_includes_autostart_item(tk_window, monkeypatch):
    from pystray._base import Icon

    state = {"enabled": True}
    monkeypatch.setattr(autostart, "is_supported", lambda: True)
    monkeypatch.setattr(autostart, "is_enabled", lambda: state["enabled"])

    import pystray

    class Backend(Icon):
        def __del__(self):
            pass

        def run_detached(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pystray, "Icon", Backend)
    app = TrayApp(AppConfig())
    app._win = tk_window
    app._build_ui()
    try:
        app._start_tray()
        item = next(i for i in app._icon.menu if i.text == "开机自启动")
        assert item.checked is True
        state["enabled"] = False
        assert item.checked is False
    finally:
        app._shutdown()


def test_cli_autostart_command(monkeypatch, capsys):
    state = {"enabled": False}
    monkeypatch.setattr(autostart, "is_supported", lambda: True)
    monkeypatch.setattr(autostart, "is_enabled", lambda: state["enabled"])
    monkeypatch.setattr(autostart, "set_enabled", lambda v: state.__setitem__("enabled", bool(v)) or state["enabled"])
    monkeypatch.setattr(autostart, "describe_target", lambda: '"C:\\app\\start-camab.exe"')

    assert main(["autostart", "status"]) == 0
    assert "未开启" in capsys.readouterr().out

    assert main(["autostart", "on"]) == 0
    out = capsys.readouterr().out
    assert "已开启" in out and state["enabled"] is True

    assert main(["autostart", "off"]) == 0
    out = capsys.readouterr().out
    assert "已关闭" in out and state["enabled"] is False

def test_linux_autostart_quotes_installation_path(monkeypatch, tmp_path):
    from camab import autostart

    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(autostart, "resolve_launch_argv",
                        lambda: ["/opt/Camera Tools 100%/CamabTray"])
    assert autostart.set_enabled(True)
    entry = tmp_path / "autostart" / "camera-auto-brightness.desktop"
    assert 'Exec="/opt/Camera Tools 100%%/CamabTray"' in entry.read_text(encoding="utf-8").splitlines()
