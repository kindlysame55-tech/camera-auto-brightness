"""托盘 GUI 测试。

两个层次：
* 纯函数：图标生成（不需要显示器）
* 界面自检：建窗口渲染几帧再关掉（无显示器时自动跳过）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab.config import AppConfig  # noqa: E402
from camab.tray import TrayApp, make_icon_image  # noqa: E402


def _has_display() -> bool:
    if os.name != "nt":
        return bool(os.environ.get("DISPLAY"))
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


HAS_DISPLAY = _has_display()


class TestGracefulStop:
    """优雅退出：外部写停止信号 → 程序自己收图标退出，不留幽灵图标。

    背景：以前自动化里用 Stop-Process -Force 反复强杀托盘程序，
    每次都在 Windows 通知区域留下一个"幽灵图标"（注册信息还在、进程没了），
    最后把用户的任务栏通知区域搞乱。所以必须有正经退出通道。
    """

    def test_stop_flag_path_next_to_config(self, tmp_path, monkeypatch):
        from camab.config import default_config_path, stop_flag_path

        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "config.json"))
        assert stop_flag_path().parent == default_config_path().parent
        assert stop_flag_path().name == "stop.flag"

    def test_check_stop_flag_quits_and_consumes_flag(self, tmp_path, monkeypatch):
        from camab import tray as tray_mod

        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "config.json"))
        cfg = AppConfig()
        app = tray_mod.TrayApp(cfg)

        quit_called = []
        app._on_quit = lambda: quit_called.append(True)          # type: ignore[assignment]
        assert app._check_stop_flag() is False, "没有信号时不该退出"
        assert quit_called == []

        from camab.config import stop_flag_path

        flag = stop_flag_path()
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("stop", encoding="utf-8")
        assert app._check_stop_flag() is True
        assert quit_called == [True], "收到信号后应调用退出"
        assert not flag.exists(), "信号文件要消费掉，否则下次启动会被立刻劝退"

    def test_cmd_stop_without_process_clears_stale_flag(self, tmp_path, monkeypatch, capsys):
        """没有进程在跑时：不写信号，并清掉上次残留的信号。

        实测踩到：残留的 stop.flag 让新启动的进程一起来就自己退出了。
        """
        from camab import cli_run
        from camab.config import stop_flag_path

        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "config.json"))
        flag = stop_flag_path()
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("stop", encoding="utf-8")
        monkeypatch.setattr(cli_run, "_tray_process_count", lambda: 0)

        class _A:
            timeout = 1.0

        assert cli_run.cmd_stop(_A()) == 0
        out = capsys.readouterr().out
        assert "没在运行" in out
        assert not flag.exists(), "残留信号必须被清掉"


class TestIcon:
    def test_icon_size_and_mode(self):
        img = make_icon_image(50)
        assert img.size == (64, 64)
        assert img.mode == "RGBA"

    def test_brighter_level_is_brighter(self):
        import numpy as np

        def total(level: int) -> int:
            arr = np.asarray(make_icon_image(level).convert("RGB"), dtype=np.int64)
            return int(arr.sum())

        assert total(100) > total(10)

    def test_paused_uses_grey(self):
        img = make_icon_image(100, paused=True).convert("RGB")
        r, g, b = img.getpixel((32, 32))
        assert r == g == b, "暂停态应该是灰色"

    def test_extremes_do_not_crash(self):
        for lv in (-10, 0, 50, 100, 999):
            assert make_icon_image(lv).size == (64, 64)


@pytest.mark.skipif(not HAS_DISPLAY, reason="没有可用显示器")
class TestTrayUI:
    def test_startup_without_camera_degrades_gracefully(self, tmp_path: Path, monkeypatch):
        """没有摄像头时：界面要能起来并显示错误，而不是崩掉。"""
        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "c.json"))
        cfg = AppConfig()
        cfg.engine.dry_run = True
        cfg.engine.backend = "dryrun"
        cfg.camera.index = 8          # 几乎肯定不存在的设备
        cfg.camera.open_timeout_s = 3.0
        app = TrayApp(cfg)
        code = app.start(selftest=True)
        assert code == 0
        assert app.engine.status.state in ("error", "idle", "warming", "running")
