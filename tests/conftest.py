"""pytest 共享设置：路径注入 + Tk 资源固定。

## 为什么需要固定 Tcl 库路径

整套测试里 GUI 用例较多，每个用例都 ``tk.Tk()`` + ``destroy()``。
反复创建/销毁 Tcl 解释器后，偶尔会炸出：

    _tkinter.TclError: Can't find a usable init.tcl in the following directories:
        ...\\tcl\\tcl8.6

这是 Tcl 在被卸载后重新加载时找不到库目录（实测整套跑会偶发，
单跑某个文件不复现）。两道保险：

1. 显式把 ``TCL_LIBRARY`` 指到 Python 自带的 tcl8.6 目录；
2. 提供 :func:`tk_root` 会话级 fixture —— GUI 用例**共用一个根窗口**，
   每个用例只建自己的 ``Toplevel`` 并销毁，不再反复创建解释器。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _pin_tcl_library() -> str:
    """把 TCL_LIBRARY 指向实际存在的 tcl8.6 目录（找不到就返回空串）。"""
    if os.environ.get("TCL_LIBRARY"):
        return os.environ["TCL_LIBRARY"]
    bases = [getattr(sys, "base_prefix", ""), getattr(sys, "prefix", "")]
    for base in bases:
        if not base:
            continue
        for rel in (Path("tcl") / "tcl8.6", Path("lib") / "tcl8.6", Path("tcl") / "tcl8"):
            cand = Path(base) / rel
            if (cand / "init.tcl").exists():
                os.environ["TCL_LIBRARY"] = str(cand)
                return str(cand)
    return ""


TCL_LIBRARY = _pin_tcl_library()


def pytest_configure(config):
    # A fresh checkout has no ignored tmp/ directory. Pytest requires the
    # configured basetemp's parent to exist before tmp_path_factory is used.
    if config.option.basetemp:
        Path(config.option.basetemp).resolve().parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _isolate_logging():
    """Do not retain a previous test's captured stdout or open log files."""
    import logging
    from camab import status
    root = logging.getLogger()
    previous, level, configured = list(root.handlers), root.level, status._CONFIGURED
    yield
    for handler in list(root.handlers):
        if handler not in previous and (type(handler) is logging.StreamHandler or isinstance(handler, logging.FileHandler)):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    status._CONFIGURED = configured


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path_factory, monkeypatch):
    """**让测试永远碰不到用户的真实配置**。

    实测踩到过：GUI 用例里调了 ``cfg.save()``（自定义速度、采样速度、设置面板
    「保存」按钮都会触发），而它默认写 ``%LOCALAPPDATA%\\camera-auto-brightness\\config.json``
    —— 于是测试把用户的运行配置改成了「干跑 + 模拟后端 + 2.2 秒采样」，
    重启托盘后自动亮度直接失灵。这类污染极难排查，必须从根上堵死。

    做法：把所有测试的 ``CAMAB_CONFIG`` 指到临时目录；用例自己想指定路径时
    照常用 ``monkeypatch.setenv`` 覆盖即可。
    """
    tmp = tmp_path_factory.mktemp("camab-config") / "config.json"
    monkeypatch.setenv("CAMAB_CONFIG", str(tmp))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp.parent))
    return tmp


def has_display() -> bool:
    """能不能建 Tk 根窗口（无桌面会话时跳过 GUI 用例）。"""
    try:
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        r.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def tk_root():
    """会话级根窗口：GUI 用例共用，避免反复创建/销毁 Tcl 解释器。

    **要重试**：实测 ``tk.Tk()`` 会偶发失败（整套跑 3 次里约 1 次，
    失败时所有 GUI 用例被静默跳过，看起来像"测试少了"）。重试几次就稳了。
    """
    import time

    tk = pytest.importorskip("tkinter")
    root = None
    last = None
    for attempt in range(5):
        try:
            root = tk.Tk()
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6 * (attempt + 1))
    if root is None:
        pytest.skip(f"没有可用桌面会话：{last}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def tk_window(tk_root):
    """每个用例一个独立子窗口（用完销毁，但不动根窗口）。"""
    import tkinter as tk

    win = tk.Toplevel(tk_root)
    win.withdraw()
    yield win
    try:
        win.destroy()
    except Exception:  # noqa: BLE001
        pass


class _DialogRecorder:
    """替身 messagebox：记录调用并直接返回，**不弹窗**。

    ``askyesno`` 默认返回 True（= 用户点「是」），可以用 ``answer`` 改。
    """

    answer = True
    calls: list = []

    @classmethod
    def _record(cls, kind, args):
        cls.calls.append((kind, args))
        if kind == "askyesno":
            return cls.answer
        return "ok"

    @classmethod
    def showinfo(cls, *a, **k):
        return cls._record("showinfo", a)

    @classmethod
    def showwarning(cls, *a, **k):
        return cls._record("showwarning", a)

    @classmethod
    def showerror(cls, *a, **k):
        return cls._record("showerror", a)

    @classmethod
    def askyesno(cls, *a, **k):
        return cls._record("askyesno", a)


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """**测试里绝不允许弹出模态对话框**。

    模态框会一直等用户点「确定」——自动化里没有用户，于是整个 pytest
    永久卡住（实测踩到两次：设置面板的「保存」按钮、校准向导弹的完成提示）。
    这里把所有会弹窗的模块统一换成记录器。

    要断言"确实弹了某个提示"，用 ``camab.tray.messagebox.calls`` 或
    ``camab.calib_dialog.messagebox.calls``。
    """
    import camab.calib_dialog as calib
    import camab.tray as tray_mod

    rec = type("_Rec", (_DialogRecorder,), {"calls": [], "answer": True})
    monkeypatch.setattr(tray_mod, "messagebox", rec, raising=False)
    monkeypatch.setattr(calib, "messagebox", rec, raising=False)
    return rec
