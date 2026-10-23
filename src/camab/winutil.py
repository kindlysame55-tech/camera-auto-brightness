"""Windows 小工具：隐藏子进程窗口、纯静默启动。

为什么需要这个文件：
* 用 ``subprocess`` 起 ``powershell`` 时，Windows 会**闪一个黑框**。用户明确反馈过
  "调取的时候总会有一个 windows 的命令框出来，不够美观"。凡是 WMI 读写的兜底路径、
  摄像头探活子进程，都必须加 :data:`CREATE_NO_WINDOW`。
* ``pythonw.exe -m camab tray`` 这类启动方式本身没有窗口，但用 ``cmd /c`` 起就会留一个。
  托盘版统一走 :func:`spawn_silent`。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional, Sequence

log = logging.getLogger(__name__)

#: 创建进程时不要窗口（Windows 专有；其它平台为 0）
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 隐藏窗口启动（pythonw.exe 的等价写法）
DETACHED_PROCESS = 0x00000008 if os.name == "nt" else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if os.name == "nt" else 0


def no_window_kwargs() -> dict:
    """给 ``subprocess.run/Popen`` 用的"不弹窗"参数集合。"""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0          # SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": CREATE_NO_WINDOW,
    }


def run_hidden(args: Sequence[str], timeout: float = 30.0, **kwargs) -> subprocess.CompletedProcess:
    """跑一个**绝对不弹窗**的子进程（替代裸 ``subprocess.run``）。"""
    kw = dict(no_window_kwargs())
    kw.update(kwargs)
    return subprocess.run(list(args), timeout=timeout, **kw)


def python_launcher() -> str:
    """优先用 pythonw.exe（无控制台），退而求其次用当前解释器。"""
    exe = sys.executable or ""
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe or "python"


def spawn_silent(args: Sequence[str], cwd: Optional[str] = None) -> Optional[int]:
    """静默启动一个独立进程（不继承控制台、不弹窗），返回 PID。

    用于托盘版自启 / 开机自启脚本调用。失败返回 ``None`` 并记日志。
    """
    try:
        if os.name == "nt":
            proc = subprocess.Popen(
                list(args),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                close_fds=True,
            )
        else:  # pragma: no cover - 非 Windows
            proc = subprocess.Popen(
                list(args), cwd=cwd,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        log.info("已静默启动：%s（PID %s）", " ".join(str(a) for a in args[:3]), proc.pid)
        return proc.pid
    except Exception as exc:  # noqa: BLE001
        log.error("静默启动失败：%s", exc)
        return None


def launch_tray_background() -> int:
    """把自己（当前程序）以托盘模式静默拉起，然后本进程退出。

    两种情形：
    * 源码运行：``pythonw -m camab tray``
    * 冻结 exe：直接再起一次 ``Camab.exe tray``（无控制台）
    """
    frozen = bool(getattr(sys, "frozen", False))
    env = os.environ.copy()
    if frozen:
        args = [sys.executable, "tray"]
    else:
        args = [python_launcher(), "-m", "camab", "tray"]
        # 保证子进程能 import camab（.pth 已配好，这里再兜一层更稳）
        here = os.path.abspath(__file__)
        src = os.path.dirname(os.path.dirname(os.path.dirname(here)))  # src/
        if os.path.isdir(src):
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    try:
        flags = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW) if os.name == "nt" else 0
        proc = subprocess.Popen(
            args, env=env, cwd=os.getcwd(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("后台启动失败：%s", exc)
        return 1
    log.info("托盘版已在后台启动（PID %s），本进程退出", proc.pid)
    return 0
