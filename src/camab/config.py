"""配置：默认值 + JSON 持久化 + 环境变量覆盖。

配置文件位置：``%LOCALAPPDATA%\\camera-auto-brightness\\config.json``
（可用环境变量 ``CAMAB_CONFIG`` 指向别处）
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from .metrics import AnalysisConfig, MappingConfig

log = logging.getLogger(__name__)

APP_NAME = "camera-auto-brightness"
_SAVE_LOCK = threading.Lock()


def _coerce_value(value: Any, default: Any, key: str) -> Any:
    """按默认值的类型恢复单项；一项损坏不应丢掉整份配置。"""
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                return value.strip().lower() == "true"
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
        elif isinstance(default, (int, float)):
            if not isinstance(value, bool) and math.isfinite(float(value)):
                return int(value) if isinstance(default, int) else float(value)
        elif isinstance(default, str):
            if isinstance(value, str):
                return value
        elif isinstance(default, list) and isinstance(value, (list, tuple)):
            if key.endswith(".backends"):
                names = [v.strip().upper() for v in value if isinstance(v, str)]
                names = list(dict.fromkeys(v for v in names if v in ("DSHOW", "MSMF", "ANY")))
                if names:
                    return names
            elif key.endswith(".anchors"):
                points = []
                for point in value:
                    if not isinstance(point, (list, tuple)) or len(point) != 2:
                        continue
                    try:
                        lux, level = point
                        if isinstance(lux, bool) or isinstance(level, bool):
                            continue
                        if math.isfinite(float(lux)) and math.isfinite(float(level)):
                            points.append((float(lux), int(level)))
                    except (TypeError, ValueError, OverflowError):
                        continue
                return points
    except (TypeError, ValueError, OverflowError):
        pass
    log.warning("配置项 %s 无效，已恢复默认值", key)
    return copy.deepcopy(default)


def _update_fields(target: Any, data: dict, prefix: str = "") -> None:
    defaults = type(target)()
    known = {f.name for f in fields(target)}
    for key in data.keys() - known:
        log.warning("配置项 %s%s 不存在，已忽略", prefix, key)
    for f in fields(target):
        default = getattr(defaults, f.name)
        if f.name in data and not dataclasses.is_dataclass(default):
            setattr(target, f.name, _coerce_value(data[f.name], default, prefix + f.name))


def default_config_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME


def stop_flag_path() -> Path:
    """托盘程序的"请优雅退出"信号文件。

    外部（`camab stop`）写这个文件，运行中的托盘程序在下一次轮询时
    自己收掉通知区域图标、停引擎、正常退出 —— 避免被强杀留下幽灵图标。
    """
    return default_config_path().parent / "stop.flag"


def default_config_path() -> Path:
    env = os.environ.get("CAMAB_CONFIG")
    if env:
        return Path(env)
    return default_config_dir() / "config.json"


@dataclass
class CameraConfig:
    index: int = 0
    #: 视频源：留空=用 index 指定的摄像头；也可以是视频文件路径或网络流地址
    #: （手机当光传感器：``http://192.168.1.5:8080/video``）
    source: str = ""
    width: int = 320
    height: int = 240
    fps: float = 2.0
    lock_exposure: bool = False
    backends: list[str] = field(default_factory=lambda: ["DSHOW", "MSMF", "ANY"])
    open_timeout_s: float = 10.0
    read_timeout_s: float = 3.0
    #: 连续多少帧全黑就判定摄像头没出画面（40 帧 ≈ 20 秒）
    black_frame_limit: int = 40

    def effective_source(self) -> int | str:
        """给 :class:`~camab.camera.Camera` 用的源：字符串优先。"""
        s = (self.source or "").strip()
        if s:
            return s
        return int(self.index)


@dataclass
class EngineConfig:
    backend: str = "auto"            # auto / wmi / ddc / dryrun
    dry_run: bool = False
    allow_dryrun_fallback: bool = True
    interval_s: float = 2.5          # 采样周期（默认走"均衡"手感）
    #: 手动改亮度后暂停自动的时长。默认 60s（原 300s 太长，实测用户会以为"自动失灵了"）。
    #: 设成 0 = 不暂停：你自己调完亮度后自动调光立刻继续接管。
    manual_hold_s: float = 60.0
    #: 检测到"亮度被外部改了"（Fn 键/电源计划/屏幕自己省电变暗）时让出控制权的时长。
    #: 必须短：它可能每一拍都被触发，太长就表现成"自动调光再也不动"（实测踩到）。
    external_hold_s: float = 20.0
    resume_threshold: int = 3        # 用户改亮度多少 % 才算手动接管
    log_level: str = "INFO"
    status_every_s: float = 1.0
    warmup_s: float = 1.5            # 启动后先丢弃几帧（自动曝光稳定）
    #: 自适应采样：刚调过亮度就快点看结果，稳定时放慢省 CPU
    adaptive_interval: bool = True
    #: 手感预设名（sensitive / balanced / calm），由 `camab config preset` 套用
    preset: str = "balanced"

    def apply_preset(self, name: str) -> bool:
        """套用手感预设的**引擎侧**参数（采样周期）。

        映射侧参数（迟滞/冷却/步进/EMA/变化速度）用
        :meth:`AppConfig.apply_mapping_preset` —— 两者分开是因为
        ``EngineConfig`` 看不到 ``mapping``。
        """
        from .metrics import PRESETS

        p = PRESETS.get(str(name or "").strip().lower())
        if not p:
            return False
        self.preset = str(name).strip().lower()
        self.interval_s = float(p["interval_s"])
        return True


@dataclass
class AppConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    mapping: MappingConfig = field(default_factory=MappingConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    #: 相机响应标定系数（<1 表示相机比假定更灵敏，等效 lux 下调）
    calib: float = 1.0
    #: 照度 → 等效 lux 的幂次（用于粗校不同相机的响应曲线）
    lux_gamma: float = 1.0
    #: 是否把估算 lux 的统计量压低（避屏幕反光/避人脸，见 AnalysisConfig.percentile）
    avoid_screen: bool = True
    #: 屏幕反光系数（每 1% 屏幕亮度贡献的画面线性亮度）。
    #: >0 且 screen_calibrated=True 时，测光会先把"屏幕自己造的光"扣掉再查曲线。
    screen_reflection_k: float = 0.0
    screen_calibrated: bool = False

    # -- 持久化 ---------------------------------------------------------
    def apply_mapping_preset(self, name: str) -> bool:
        """把手感预设里的映射侧参数（迟滞/冷却/步进/EMA/变化速度）写进 mapping。

        与 ``engine.apply_preset`` 配合使用；只动"手感"，不动曲线与亮度范围。
        """
        from .metrics import PRESETS

        p = PRESETS.get(str(name or "").strip().lower())
        if not p:
            return False
        m = self.mapping
        m.deadband = int(p["deadband"])
        m.cooldown_s = float(p["cooldown_s"])
        m.max_step = int(p["max_step"])
        m.ema_alpha = float(p["ema_alpha"])
        m.ramp_per_s = float(p.get("ramp_per_s", m.ramp_per_s))
        m.min_write_interval_s = float(p.get("min_write_interval_s", m.min_write_interval_s))
        return True

    def to_dict(self) -> dict:
        return asdict(self)

    def snapshot(self) -> "AppConfig":
        """独立、已校验的副本，供设置草稿和运行中的引擎使用。"""
        return copy.deepcopy(self).clamped()

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        cfg = cls()
        if not isinstance(data, dict):
            log.warning("配置根节点必须是对象，已使用默认值")
            return cfg
        _update_fields(cfg, data)
        for name in ("camera", "analysis", "mapping", "engine"):
            section = getattr(cfg, name)
            val = data.get(name, {})
            if isinstance(val, type(section)):
                val = asdict(val)
            if isinstance(val, dict):
                _update_fields(section, val, name + ".")
                # 保留 MappingConfig.__post_init__ 对缺省折点的范围适配。
                kwargs = {f.name: getattr(section, f.name) for f in fields(section)
                          if f.name in val}
                setattr(cfg, name, type(section)(**kwargs))
            else:
                log.warning("配置段 %s 必须是对象，已使用默认值", name)
        return cfg.clamped()

    def save(self, path: Optional[Path] = None) -> Path:
        p = Path(path) if path else default_config_path()
        payload = json.dumps(self.snapshot().to_dict(), ensure_ascii=False, indent=2,
                             allow_nan=False)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            # 每次保存使用独立文件，避免设置窗口和后台同时保存时互相覆盖。
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                             prefix=f".{p.name}.", suffix=".tmp",
                                             delete=False) as stream:
                tmp = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows 会拒绝两个线程同时替换同一目标，替换阶段需要串行。
            with _SAVE_LOCK:
                os.replace(tmp, p)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("临时配置清理失败：%s", exc)
        log.info("配置已保存：%s", p)
        return p

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        p = Path(path) if path else default_config_path()
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8-sig")))
        except FileNotFoundError:
            return cls()
        except Exception as exc:  # noqa: BLE001
            log.warning("配置读取失败（用默认值）：%s", exc)
            return cls()

    # -- 校验 -----------------------------------------------------------
    def clamped(self) -> "AppConfig":
        _update_fields(self, vars(self))
        for name, section_cls in (("camera", CameraConfig), ("analysis", AnalysisConfig),
                                  ("mapping", MappingConfig), ("engine", EngineConfig)):
            section = getattr(self, name)
            if not isinstance(section, section_cls):
                section = section_cls()
                setattr(self, name, section)
            _update_fields(section, vars(section), name + ".")
        # MappingConfig 要求 floor < ceil；floor=100 会使旧校验生成 ceil=101。
        self.mapping.floor = max(0, min(99, self.mapping.floor))
        self.mapping = self.mapping.clamped()
        self.mapping.min_write = max(0, min(100, self.mapping.min_write))
        self.mapping.feedback_damping = max(0.0, min(1.0, self.mapping.feedback_damping))
        self.mapping.feedback_guard = max(0.0, min(1.0, self.mapping.feedback_guard))
        self.camera.width = max(64, min(1920, int(self.camera.width)))
        self.camera.height = max(48, min(1080, int(self.camera.height)))
        self.camera.fps = max(0.1, min(30.0, float(self.camera.fps)))
        self.camera.index = max(0, min(9, int(self.camera.index)))
        self.camera.open_timeout_s = max(0.1, min(120.0, self.camera.open_timeout_s))
        self.camera.read_timeout_s = max(0.05, min(30.0, self.camera.read_timeout_s))
        self.camera.black_frame_limit = max(0, self.camera.black_frame_limit)
        self.engine.interval_s = max(0.5, min(120.0, float(self.engine.interval_s)))
        self.engine.manual_hold_s = max(0.0, float(self.engine.manual_hold_s))
        self.engine.external_hold_s = max(0.0, self.engine.external_hold_s)
        self.engine.resume_threshold = max(1, min(100, self.engine.resume_threshold))
        self.engine.status_every_s = max(0.1, self.engine.status_every_s)
        self.engine.warmup_s = max(0.0, min(10.0, float(self.engine.warmup_s)))
        self.analysis.crop = max(0.0, min(0.45, float(self.analysis.crop)))
        self.analysis.percentile = max(0.0, min(100.0, float(self.analysis.percentile)))
        self.analysis.ignore_frac = max(0.0, min(0.2, float(self.analysis.ignore_frac)))
        self.calib = max(0.01, min(100.0, float(self.calib)))
        self.lux_gamma = max(0.2, min(5.0, float(self.lux_gamma)))
        self.screen_reflection_k = max(0.0, min(0.01, float(self.screen_reflection_k)))
        if self.screen_reflection_k <= 0:
            self.screen_calibrated = False
        return self

    @property
    def dark_lux(self) -> float:
        """曲线最低折点的照度（只读，供界面/日志展示）。"""
        return self.mapping.clamped().curve_points()[0][0]

    @property
    def bright_lux(self) -> float:
        """曲线最高折点的照度（只读）。"""
        return self.mapping.clamped().curve_points()[-1][0]

    def reflection(self):
        """当前配置对应的 :class:`~camab.reflection.ReflectionCompensation`。"""
        from .reflection import ReflectionCompensation

        if not self.screen_calibrated or self.screen_reflection_k <= 0:
            return None
        return ReflectionCompensation(k=self.screen_reflection_k, calibrated=True,
                                     note="来自配置")

    # -- 快捷属性 -------------------------------------------------------
    @property
    def effective_percentile(self) -> float:
        """avoid_screen 打开时把分位压到 35 以下，尽量避开画面里的屏幕/人脸。

        真机教训（2026-10-07）：摄像头正对人时，画面中位数主要反映"脸有多亮"，
        与房间环境光相差一个数量级。低分位更接近人眼感知的环境亮度。
        """
        base = self.analysis.percentile
        return min(base, 35.0) if self.avoid_screen else base

    def mapping_with_effective_percentile(self) -> MappingConfig:
        return self.mapping
