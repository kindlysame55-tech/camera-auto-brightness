"""引擎状态对象与日志初始化。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
@dataclass
class Status:
    """引擎的一次快照，供 CLI / GUI 展示。"""

    t: float = 0.0
    running: bool = False
    state: str = "idle"          # idle / warming / running / paused / error
    message: str = ""
    # 测光
    lux: float = 0.0
    ratio: float = 0.0
    samples: int = 0
    stabilized: bool = False
    auto_exposure_drift: float = 0.0
    feedback_suspect: bool = False
    screen_compensated: bool = False     # 是否已扣除屏幕自身反光
    compensation_reliable: bool = False  # 扣除结果是否可信
    # 决策
    ideal: int = 0
    applied: Optional[int] = None
    current: int = 0
    reason: str = ""
    same_dir_streak: int = 0
    cooling: bool = False
    damped: bool = False          # 正在做"回流阻尼"（步长被压低）
    transitioning: bool = False  # 正把已经批准的目标段逐步写到屏幕
    transition_target: Optional[int] = None
    # 相机 / 后端
    camera_ok: bool = False
    camera_backend: str = ""
    camera_success: float = 1.0
    camera_error: str = ""
    bright_backend: str = ""
    monitor_key: str = ""
    bright_error: str = ""       # 最近一次真实亮度写入失败；成功写入后才清除
    monitors: list = field(default_factory=list)
    # 统计
    writes: int = 0
    write_mismatch: int = 0     # 写入后回读不一致的次数（驱动/显示器不支持写）
    errors: int = 0
    uptime_s: float = 0.0
    interval_s: float = 3.0

    def board(self) -> str:  # pragma: no cover - 展示用
        return (f"{self.state:>8} lux={self.lux:7.2f} 当前={self.current:3d}% "
                f"目标={self.ideal:3d}% 写入={self.applied} {self.reason}")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: Optional[str] = None,
                  to_stdout: bool = True) -> logging.Logger:
    """配置根 logger：控制台 + 可选文件。幂等。"""
    global _CONFIGURED
    root = logging.getLogger()
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(lvl)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-22s %(message)s", datefmt="%H:%M:%S"
    )
    if not _CONFIGURED:
        if to_stdout:
            sh = logging.StreamHandler(stream=sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        _CONFIGURED = True

    # Windows 控制台中文
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        # 压掉 OpenCV 的 native 警告（MSMF/FFMPEG 找不到设备时会刷屏，实际不影响）
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
        os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")

    if log_file:
        from pathlib import Path

        p = Path(log_file)
        already = any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(p.resolve())
            for h in root.handlers
        )
        if not already:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                fh = RotatingFileHandler(p, maxBytes=2 * 1024 * 1024,
                                         backupCount=3, encoding="utf-8")
                fh.setFormatter(fmt)
                root.addHandler(fh)
            except OSError as exc:
                root.warning("日志文件不可写，将继续运行：%s", exc)
    return root


def default_log_path() -> str:
    from .config import default_config_path

    return str(default_config_path().parent / "camab.log")
