"""测光与调光决策 —— 纯逻辑，无 IO，便于单元测试。

流水线::

    帧 → luma_ratio → (曝光/增益补偿) → lux → 曲线映射 → 迟滞/冷却/限速 → 目标亮度

这里刻意不 import cv2 / win32，只依赖 numpy 和标准库。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

__all__ = [
    "AnalysisConfig", "Analysis", "analyze_frame", "luma_ratio",
    "lux_from_ratio", "MappingConfig", "MappingDecision", "BrightnessMapper",
    "clean_anchors", "default_anchors", "PRESETS",
    "ramp_from_speed", "speed_from_ramp",
    "interval_from_speed", "speed_from_interval",
    "RAMP_FAST", "RAMP_SLOW", "INTERVAL_FAST", "INTERVAL_SLOW",
]


#: 手感预设：名字 → 需要覆盖的映射/引擎参数
PRESETS: dict[str, dict] = {
    # 灵敏：转灯/拉窗帘后很快跟上，代价是可能多调几次
    "sensitive": {"interval_s": 1.5, "deadband": 3, "cooldown_s": 2.5,
                  "max_step": 20, "ema_alpha": 0.6,
                  "ramp_per_s": 30.0, "min_write_interval_s": 0.6},
    # 均衡（默认）：跟得上也稳得住
    "balanced": {"interval_s": 2.5, "deadband": 4, "cooldown_s": 4.0,
                 "max_step": 16, "ema_alpha": 0.45,
                 "ramp_per_s": 12.0, "min_write_interval_s": 1.2},
    # 保守：尽量少动、慢慢过渡，适合屏幕反光严重/摄像头照到屏幕的场景
    "calm": {"interval_s": 4.0, "deadband": 7, "cooldown_s": 8.0,
             "max_step": 10, "ema_alpha": 0.3,
             "ramp_per_s": 4.0, "min_write_interval_s": 3.0},
}

#: 「响应速度」滑块（0~100）↔ 变化速度上限（%/秒）的换算。
#: 0 = 尽快（≈40%/s），100 = 很平滑（≈1.5%/s）
RAMP_FAST = 40.0
RAMP_SLOW = 1.5


def ramp_from_speed(speed: float) -> float:
    """滑块位置（0~100，越大越平滑）→ ramp_per_s（%/秒）。"""
    f = max(0.0, min(1.0, float(speed) / 100.0))
    return RAMP_FAST * (RAMP_SLOW / RAMP_FAST) ** f


def speed_from_ramp(ramp: float) -> int:
    """:func:`ramp_from_speed` 的逆运算（面板回显用）。"""
    ramp = max(RAMP_SLOW, min(RAMP_FAST, float(ramp)))
    f = math.log(ramp / RAMP_FAST) / math.log(RAMP_SLOW / RAMP_FAST)
    return int(round(max(0.0, min(1.0, f)) * 100))


#: 采样速度滑块的取值范围（秒）。左=0.5s 最快，右=10s 最省电。
INTERVAL_FAST = 0.5
INTERVAL_SLOW = 10.0


def interval_from_speed(speed: float) -> float:
    """滑块位置（0~100，**越大越快**）→ 采样周期（秒）。"""
    f = 1.0 - max(0.0, min(1.0, float(speed) / 100.0))
    return round(INTERVAL_FAST * (INTERVAL_SLOW / INTERVAL_FAST) ** f, 2)


def speed_from_interval(interval: float) -> int:
    """:func:`interval_from_speed` 的逆运算（面板回显用）。"""
    iv = max(INTERVAL_FAST, min(INTERVAL_SLOW, float(interval)))
    f = math.log(iv / INTERVAL_FAST) / math.log(INTERVAL_SLOW / INTERVAL_FAST)
    return int(round((1.0 - max(0.0, min(1.0, f))) * 100))


def clean_anchors(anchors, floor: int = 0, ceil: int = 100) -> list[tuple[float, int]]:
    """清洗折点：过滤非法项、按照度升序、去掉过近的重复点。

    **不在这里把折点亮度钳进 [floor, ceil]** —— 那样 floor/ceil 会变成单向旋钮
    （调高有效、调低无效，实测踩到）。折点保留用户意图，边界在
    :meth:`MappingConfig.lux_to_level` 出口统一收口。
    """
    clean: list[tuple[float, int]] = []
    for a in (anchors or []):
        try:
            lux, lvl = float(a[0]), int(a[1])
        except (TypeError, ValueError, IndexError):
            continue
        if lux <= 0:
            continue
        clean.append((lux, max(0, min(100, lvl))))
    clean.sort(key=lambda x: x[0])
    out: list[tuple[float, int]] = []
    for lux, lvl in clean:
        if out and lux < out[-1][0] * 1.15:
            # 照度太近的两点会插值出台阶，保留亮度更高的那个意图
            out[-1] = (out[-1][0], max(out[-1][1], lvl))
            continue
        out.append((lux, lvl))
    return out


#: 默认曲线形状：``(照度 lux, 亮度占 floor~ceil 区间的比例)``。
#: 用"比例"而不是绝对亮度，是为了让 floor/ceil 真正双向有效：
#: 把最低亮度从 20 调到 10，整条曲线会跟着降下来，而不是被硬编码的折点顶住。
DEFAULT_CURVE_SHAPE: tuple[tuple[float, float], ...] = (
    (2.0, 0.00),      # 全黑/只有屏幕
    (15.0, 0.225),    # 夜里开个小灯
    (60.0, 0.50),     # 普通室内灯
    (200.0, 0.775),   # 明亮房间
    (500.0, 0.9375),  # 白天靠窗
    (1200.0, 1.00),   # 直射/室外
)


def default_anchors(floor: int = 20, ceil: int = 100) -> list[tuple[float, int]]:
    """按 floor/ceil 生成默认折点。"""
    span = max(0, int(ceil) - int(floor))
    return [(lux, int(round(int(floor) + span * frac))) for lux, frac in DEFAULT_CURVE_SHAPE]


# --------------------------------------------------------------------------- #
# 一、帧 → 亮度统计
# --------------------------------------------------------------------------- #
@dataclass
class AnalysisConfig:
    """取帧后的统计参数。"""

    #: 采样区域：0.1 表示裁掉四周 10%，避开窗口边框/水印
    crop: float = 0.10
    #: **注意默认 15 而不是 50**：真机实测（2026-10-07）摄像头正对人脸时，
    #: 画面里脸占一大块 —— 分位从 5 扫到 75，等效照度差了 30 倍（16 lx → 464 lx）。
    #: 50 分位量到的是"脸有多亮"（0.11），房间背景只有 0.014；用户反馈
    #: "调取不够灵敏、也不够线性"就是这个原因。15 分位 ≈ 房间暗部基调。
    #: 想更准可以跑 `camab calibrate-screen` 把屏幕反光分量直接扣掉。
    percentile: float = 15.0
    #: 逐像素从 sRGB 转线性光
    linearize: bool = True
    #: 忽略最暗/最亮的比例（抗高光与死黑）
    ignore_frac: float = 0.02


@dataclass
class Analysis:
    """一帧的分析结果，全部是 0-1 的线性亮度值。"""

    mean: float = 0.0
    median: float = 0.0
    robust: float = 0.0        # 主用指标（= 指定分位）
    p05: float = 0.0           # 暗部基线，用于自激/屏幕回流检测
    p95: float = 0.0
    std: float = 0.0
    #: 相对上一次分析的变化率（每秒），由 :func:`analyze_frame` 外部填
    dtx: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def _to_linear(x):  # x: np.ndarray[float32] in 0..1
    """sRGB → 线性光。"""
    import numpy as np

    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def luma_ratio(gray, cfg: AnalysisConfig = AnalysisConfig()) -> Analysis:
    """把灰度图（任意 dtype、任意通道形状）压成 :class:`Analysis`。

    :param gray: numpy 数组，2D 灰度 或 3D(BGR/BGRA)；uint8 / uint16 / float
    空图返回全零统计；非空图必须有两个或三个维度。
    """
    import numpy as np

    arr = np.asarray(gray)
    if arr.size == 0:
        return Analysis()
    if arr.ndim not in (2, 3):
        raise ValueError("测光帧必须是二维灰度图或三维彩色图")
    # Crop the view before allocating float/channel buffers. Keep the original
    # full-frame float peak: it defines normalization for unusual float inputs.
    integer = np.issubdtype(arr.dtype, np.integer)
    peak = 0.0 if integer else float(arr.max())
    h, w = arr.shape[:2]
    c = int(round(min(h, w) * max(0.0, min(0.45, cfg.crop))))
    if c > 0 and h - 2 * c > 4 and w - 2 * c > 4:
        arr = arr[c:h - c, c:w - c]
    unsigned = np.issubdtype(arr.dtype, np.unsignedinteger)
    if integer:
        # 整数图先归一化：np.uint8 * float 会被 numpy 提升成 float 而丢掉 dtype 信息
        arr = arr.astype(np.float32) / float(np.iinfo(arr.dtype).max)
    else:
        arr = arr.astype(np.float32)
        if peak > 1.5:  # 误传 0-255 的 float
            arr = arr / (255.0 if peak > 64.0 else max(1.0, peak))
    if not unsigned:
        np.clip(arr, 0.0, 1.0, out=arr)
    if arr.ndim == 3:
        if arr.shape[2] >= 3:
            # ITU-R BT.601 亮度（顺序无所谓，权重对称）
            arr = arr[..., 0] * 0.114 + arr[..., 1] * 0.587 + arr[..., 2] * 0.299
        else:
            arr = arr[..., 0]

    flat = arr.reshape(-1)
    if cfg.linearize:
        flat = _to_linear(flat)

    n = flat.size
    if n >= 32 and cfg.ignore_frac > 0:
        k = int(n * cfg.ignore_frac)
        if k > 0 and n - 2 * k > 8:
            # 只需分出最暗/最亮的 k 个像素，无需排序保留区域的全部像素。
            flat = np.partition(flat, (k, n - k - 1))[k:n - k]

    qs = (5.0, 50.0, 95.0, max(0.0, min(100.0, cfg.percentile)))
    # flat 来自内部归一化副本，可复用缓冲区做分位选择，不会改动调用方的帧。
    p05, p50, p95, sel = (float(v) for v in np.percentile(flat, qs, overwrite_input=True))
    return Analysis(
        mean=float(flat.mean()),
        median=p50,
        robust=sel,
        p05=p05,
        p95=p95,
        std=float(flat.std()),
    )


def analyze_frame(frame, cfg: AnalysisConfig = AnalysisConfig()) -> Analysis:
    """入口：接受 BGR 帧（cv2 原样）或已灰度图。"""
    return luma_ratio(frame, cfg)


# --------------------------------------------------------------------------- #
# 二、亮度统计 → 等效照度（lux）
# --------------------------------------------------------------------------- #
#: 标定常数：在 ~1.4 U 的相机响应下，画面中位线性亮度为 0.18（18% 灰）时约为 200 lx
LUX_REF = 200.0
REF_RATIO = 0.18
REF_EXPOSURE_S = 1.0 / 30.0
REF_GAIN = 1.0
#: 相机响应上下限，避免除零/爆表
RATIO_EPS = 0.0005
RATIO_CAP = 0.95


def lux_from_ratio(ratio: float, exposure_s: Optional[float] = None,
                   gain: Optional[float] = None,
                   calib: float = 1.0,
                   gamma: float = 1.0) -> float:
    """把画面亮度统计折算成等效照度（lux，估算值，非计量级）。

    相机的画面亮度 ≈ 环境照度 × 曝光时间 × 增益，因此反解即可：

    .. math::
        lux = K \\cdot \\frac{ratio^{\\gamma}}{t_{exp} \\cdot gain}

    其中 ``K`` 由 :data:`LUX_REF` / :data:`REF_RATIO` / :data:`REF_EXPOSURE_S` 标定：
    「曝光 1/30s、增益 1 时画面线性亮度 0.18」对应 200 lx。
    若拿不到曝光/增益（很多 DSHOW 驱动不给），就按参考值假设 —— 此时退化为相对测光，
    自动曝光带来的漂移由 :class:`~camab.metrics.BrightnessMapper` 的冷却/同向保护兜底。
    """
    r = max(RATIO_EPS, min(RATIO_CAP, float(ratio)))
    t = float(exposure_s) if exposure_s and exposure_s > 0 else REF_EXPOSURE_S
    g = float(gain) if gain and gain > 0 else REF_GAIN
    k = LUX_REF / (REF_RATIO ** gamma)
    lux = k * (r ** gamma) / (t * g) * REF_EXPOSURE_S * REF_GAIN * float(calib)
    return float(max(0.01, lux))


def lux_from_analysis(a: Analysis, exposure_s: Optional[float] = None,
                      gain: Optional[float] = None, calib: float = 1.0,
                      gamma: float = 1.0) -> float:
    return lux_from_ratio(a.robust, exposure_s, gain, calib, gamma)


# --------------------------------------------------------------------------- #
# 三、lux → 屏幕亮度（曲线 + 迟滞 + 冷却 + 限速）
# --------------------------------------------------------------------------- #
@dataclass
class MappingConfig:
    """从环境照度到屏幕亮度的映射参数（可直接暴露给设置面板）。

    曲线改成**分段线性**：``anchors`` 给出一串 (照度 lux, 屏幕亮度 %) 折点，中间线性插值。
    为什么不用指数曲线：真机反馈"调整不够线性"。分段线性每个折点的含义一眼能懂
    （"60 lx 的房间给 60% 亮度"），也能直接对着自己的房间调。
    """

    floor: int = 20            # 低于第一个折点照度时取它
    ceil: int = 100            # 高于最后一个折点照度时取它
    lux_dark: float = 5.0      # 兼容旧配置：没有 anchors 时的曲线下界
    lux_bright: float = 400.0  # 兼容旧配置：没有 anchors 时的曲线上界
    gamma: float = 0.45        # 兼容旧配置：旧公式的指数（有 anchors 时不生效）
    #: 分段线性折点（照度 lux → 屏幕亮度 %），须按照度升序，会自动清洗排序。
    #: 默认值由 :func:`default_anchors` 按 floor/ceil 生成 —— 这样改最低/最高亮度
    #: 会带着整条曲线一起动（不然折点会顶住，变成单向旋钮）。
    anchors: list[tuple[float, int]] = field(default_factory=default_anchors)
    #: 内部标记：折点是否等于"出厂默认形状"（等于就说明用户没自定义过）
    _anchors_auto: bool = field(default=True, repr=False, compare=False)

    def __post_init__(self) -> None:
        """只填了 floor/ceil、没给 anchors 时，按新范围重算默认折点。

        这样"最低亮度 20 → 10"能带着整条曲线一起降下来，而不是被
        `default_factory` 按旧范围算出的折点顶住（实测踩到：单向旋钮）。
        判定方式：折点等于出厂默认形状（floor=20/ceil=100 生成的那套）就当没自定义过。
        """
        if getattr(self, "_anchors_auto", True):
            base = default_anchors()      # 出厂形状
            if list(self.anchors or []) == base or not self.anchors:
                self.anchors = default_anchors(self.floor, self.ceil)
            # 自定义折点原样保留，不覆盖
        self._anchors_auto = False

    def clamped(self) -> "MappingConfig":
        d = asdict(self)
        was_auto = bool(d.pop("_anchors_auto", False))
        cfg = MappingConfig(**d)
        cfg.floor = max(0, min(100, int(cfg.floor)))
        cfg.ceil = max(cfg.floor + 1, min(100, int(cfg.ceil)))
        if was_auto:
            # 没手改过折点：按（可能已改的）floor/ceil 重算默认形状
            cfg.anchors = default_anchors(cfg.floor, cfg.ceil)
        elif not cfg.anchors:
            cfg.anchors = default_anchors(cfg.floor, cfg.ceil)
        cfg.lux_dark = max(0.05, float(cfg.lux_dark))
        cfg.lux_bright = max(cfg.lux_dark * 1.2, float(cfg.lux_bright))
        cfg.gamma = max(0.1, min(3.0, float(cfg.gamma)))
        cfg.deadband = max(0, min(30, int(cfg.deadband)))
        cfg.cooldown_s = max(0.0, float(cfg.cooldown_s))
        cfg.max_step = max(1, min(100, int(cfg.max_step)))
        cfg.ramp_per_s = max(0.0, min(100.0, float(getattr(cfg, "ramp_per_s", 0.0))))
        cfg.min_write_interval_s = max(0.0, float(getattr(cfg, "min_write_interval_s", 0.0)))
        cfg.ema_alpha = max(0.02, min(1.0, float(cfg.ema_alpha)))
        cfg.max_same_dir = max(1, int(cfg.max_same_dir))
        cfg.anchors = clean_anchors(cfg.anchors)
        cfg._anchors_auto = was_auto
        return cfg
    deadband: int = 4          # 迟滞带：目标差 < deadband 不动（防抖核心）
    cooldown_s: float = 4.0    # 两次调整的最小间隔
    max_step: int = 16         # 单次最大调整幅度
    min_write: int = 1
    #: 变化速度上限（亮度百分点/秒）。0 = 不限速，直接按 max_step 跳。
    #: 用户要的"调节速度"就是这个：12%/s 意味着从 40% 到 90% 大约 4 秒爬完。
    ramp_per_s: float = 12.0
    #: 两次写亮度之间的最小间隔（有些面板写太快会跟不上，只认第一次）
    min_write_interval_s: float = 1.2
    #: 目标计算平滑用的 EMA（对 lux 生效，抑制闪烁）
    ema_alpha: float = 0.45
    #: 连续同向调整次数上限（仅在判定为"疑似回流"时才刹车，抗正反馈）
    max_same_dir: int = 6
    #: 检测到屏幕回流时的步长阻尼系数：把闭环增益压下来，
    #: 让它收敛到不动点而不是锁死或来回振荡
    feedback_damping: float = 0.35
    #: 自激保护：屏幕回流检测阈值（线性亮度差）
    feedback_guard: float = 0.04

    # -- 曲线 -----------------------------------------------------------
    def curve_points(self) -> list[tuple[float, int]]:
        """实际使用的折点（至少两个）。"""
        pts = [tuple(p) for p in (self.anchors or [])]
        if len(pts) < 2:
            pts = [(self.lux_dark, self.floor), (self.lux_bright, self.ceil)]
        return pts

    def lux_to_level(self, lux: float) -> int:
        """静态曲线：照度 → 亮度百分比（分段线性，不含任何状态）。

        插值在 **log 照度** 轴上做：人眼对光近似对数响应，如果在线性 lux 轴上插值，
        暗部几档会挤成一团、亮部一大段没变化 —— 这正是"不够线性"的观感来源之一。
        出口统一钳到 ``[floor, ceil]``：这样 floor/ceil 是**双向**有效的边界，
        不会因为折点被预先钳过而变成单向旋钮。
        """
        lux = max(0.01, float(lux))
        pts = self.curve_points()
        lo_lv, hi_lv = self.floor, self.ceil
        if lux <= pts[0][0]:
            raw = pts[0][1]
        elif lux >= pts[-1][0]:
            raw = pts[-1][1]
        else:
            lo = pts[0]
            hi = pts[-1]
            for cand in pts[1:]:
                if lux <= cand[0]:
                    hi = cand
                    break
                lo = cand
            span = math.log(hi[0] / lo[0])
            frac = 0.0 if span <= 1e-9 else math.log(lux / lo[0]) / span
            frac = max(0.0, min(1.0, frac))
            raw = lo[1] + (hi[1] - lo[1]) * frac
        return int(round(max(lo_lv, min(hi_lv, raw))))

    def describe_curve(self) -> str:
        """人类可读的曲线描述（配置面板/日志用）。"""
        return "  ".join(f"{lux:.0f}lx→{lv}%" for lux, lv in self.curve_points())


@dataclass
class MappingDecision:
    """一次决策的结果。"""

    target: int            # 曲线给出的理想值（已平滑）
    applied: Optional[int]  # 实际要写下去的值；None 表示本次不动
    reason: str
    lux_smooth: float
    same_dir_streak: int = 0
    blocked_until: float = 0.0
    damped: bool = False    # 本次是否处于"疑似屏幕回流"的阻尼模式

    @property
    def changed(self) -> bool:
        return self.applied is not None


class BrightnessTransition:
    """已批准目标的缓入缓出轨迹；不访问硬件，不负责测光决策。

    冷却期限制新的目标段，而不是把整个调整压成一个台阶。轨迹的峰值
    速度仍受 ramp_per_s 约束，整数输出仅在受控间隔到期时生成。
    """

    def __init__(self) -> None:
        self.target: Optional[int] = None
        self.origin = 0
        self.started = 0.0
        self.duration = 0.0
        self.interval = 0.5
        self.next_due = float("inf")

    @property
    def active(self) -> bool:
        return self.target is not None

    def start(self, current: int, target: int, now: float, *,
              cooldown_s: float, ramp_per_s: float, min_interval_s: float) -> None:
        self.origin, self.target = int(current), int(target)
        self.started = now
        self.interval = max(0.5, float(min_interval_s))
        # smoothstep 的峰值导数为 1.5，不能用平均速度代替速度上限。
        travel_s = 1.5 * abs(target - current) / max(0.001, ramp_per_s)
        self.duration = max(float(cooldown_s), travel_s, 4 * self.interval)
        self.next_due = now + self.interval
        if current == target:
            self.cancel()

    def cancel(self) -> None:
        self.target = None
        self.next_due = float("inf")

    def next_level(self, current: int, now: float) -> Optional[int]:
        if self.target is None or now < self.next_due:
            return None
        if (self.target - current) * (self.target - self.origin) <= 0:
            self.cancel()
            return None
        frac = min(1.0, max(0.0, (now - self.started) / self.duration))
        eased = frac * frac * (3.0 - 2.0 * frac)
        level = int(round(self.origin + (self.target - self.origin) * eased))
        # 接受驱动的 1% 舍入漂移，也不能让前进中的渐变倒退一小格。
        level = max(current, level) if self.target > self.origin else min(current, level)
        # 驱动/系统阻塞后宁可延长过渡，也不一次跳过中间亮度来追赶时钟。
        level = max(current - 2, min(current + 2, level))
        self.next_due = now + self.interval
        return level if level != current else None

    def written(self, actual: int, finished: float, elapsed: float) -> None:
        # 慢驱动调用之后也留出休息，防止不断启动 CIM 子进程。
        self.next_due = finished + max(self.interval, elapsed)
        if actual == self.target:
            self.cancel()


class BrightnessMapper:
    """带状态机的调光器：曲线 + EMA + 迟滞 + 冷却 + 步进限速 + 同向计数。"""

    def __init__(self, cfg: MappingConfig = MappingConfig()) -> None:
        self.cfg = cfg.clamped()
        self.lux_smooth: Optional[float] = None
        self.last_write_t: float = -1e9
        self.last_applied: Optional[int] = None
        self.last_dir: int = 0
        self.same_dir_streak: int = 0
        self.force_cool_until: float = 0.0
        self.history: list[tuple[float, int]] = []
        #: 是否处于"疑似屏幕回流"的阻尼模式
        self._feedback_latch: bool = False
        self._quiet_count: int = 0

    # -- 状态重置 -------------------------------------------------------
    def reset(self, current: Optional[int] = None) -> None:
        self.lux_smooth = None
        self.last_write_t = -1e9
        self.last_applied = current
        self.last_dir = 0
        self.same_dir_streak = 0
        self.force_cool_until = 0.0
        self.history.clear()
        self._feedback_latch = False
        self._quiet_count = 0

    @property
    def damped(self) -> bool:
        return self._feedback_latch

    def smooth(self, lux: float) -> float:
        a = self.cfg.ema_alpha
        if self.lux_smooth is None or self.lux_smooth <= 0:
            self.lux_smooth = lux
        else:
            self.lux_smooth = math.exp((1 - a) * math.log(self.lux_smooth) + a * math.log(max(1e-3, lux)))
        return self.lux_smooth

    # -- 主决策 ---------------------------------------------------------
    def decide(self, lux: float, current: int,
               now: Optional[float] = None, damped: bool = False,
               allow_write: bool = True) -> MappingDecision:
        """给出这一拍的动作。

        :param damped: 上层判定"画面里可能混进了屏幕自己的光"（疑似回流）时置 True。
            此时不锁死，而是把步长乘 :attr:`MappingConfig.feedback_damping`，
            让闭环增益降下来 —— 既不会振荡，也仍然能跟上光照变化。
        """
        cfg = self.cfg
        now = time.monotonic() if now is None else now
        if damped:
            self._feedback_latch = True
        else:
            self._quiet_count += 1
            if self._quiet_count >= 3:
                self._feedback_latch = False
                self._quiet_count = 0

        # 1) 照度平滑（log 空间，等价于几何平均）
        s = self.smooth(lux)

        # 2) 曲线 → 理想目标
        ideal = cfg.lux_to_level(s)

        # 3) 冷却优先于迟滞：冷却期内不管差多少都不许动
        if now < self.force_cool_until:
            return MappingDecision(ideal, None, "自激冷却中", s, self.same_dir_streak,
                                   self.force_cool_until, damped)
        if not allow_write:
            return MappingDecision(ideal, None, "平滑调整中", s, self.same_dir_streak,
                                   self.force_cool_until, damped)
        if now - self.last_write_t < cfg.cooldown_s:
            return MappingDecision(ideal, None, "冷却中", s, self.same_dir_streak,
                                   self.force_cool_until, damped)
        # 3b) 写得太密面板可能跟不上（有些屏只认第一次），留一个最小间隔
        if now - self.last_write_t < cfg.min_write_interval_s:
            return MappingDecision(ideal, None, "写入间隔保护", s, self.same_dir_streak,
                                   self.force_cool_until, damped)

        # 4) 迟滞：与当前值差太小就不动
        if abs(ideal - current) < cfg.deadband:
            return MappingDecision(ideal, None, "迟滞带内", s, self.same_dir_streak,
                                   self.force_cool_until, damped)

        # 5) 步进限制：取「单次最大幅度」与「速度上限×已过时间」的较小值。
        #    速度上限就是用户调的"调节速度"：想让它慢慢过渡就调小 %/秒。
        delta = ideal - current
        direction = 1 if delta > 0 else -1
        limit = cfg.max_step
        if cfg.ramp_per_s > 0:
            elapsed = max(0.0, now - self.last_write_t)
            # last_write_t 还没写过时是 -1e9，别让它算出个天文数字
            if elapsed > 3600:
                elapsed = max(0.0, float(cfg.min_write_interval_s) + 0.001)
            ramp_limit = cfg.ramp_per_s * elapsed
            limit = min(limit, max(1.0, ramp_limit))
        if damped:
            limit = max(1, int(round(limit * cfg.feedback_damping)))
        limit = max(1, int(round(limit)))
        step = max(-limit, min(limit, delta))
        target = current + step
        target = int(max(cfg.floor, min(cfg.ceil, round(target))))
        if target == current:
            return MappingDecision(ideal, None, "限速后无变化", s, self.same_dir_streak,
                                   self.force_cool_until, damped)

        # 6) 同向连续计数：**只在疑似回流时**才刹车。正常的光照单调变化
        #    （傍晚变暗、开灯变亮）不该被这里挡住。
        if direction == self.last_dir:
            self.same_dir_streak += 1
        else:
            self.same_dir_streak = 1
        if damped and self.same_dir_streak > cfg.max_same_dir:
            self.force_cool_until = now + cfg.cooldown_s * 4
            self.same_dir_streak = 0
            log.warning("疑似屏幕回流且连续同向调整 %d 次，强制冷却 %.0fs",
                        cfg.max_same_dir, cfg.cooldown_s * 4)
            return MappingDecision(ideal, None, "回流嫌疑+同向超限→强制冷却", s,
                                   self.same_dir_streak, self.force_cool_until, damped)

        # 7) 落地
        self.last_dir = direction
        self.last_applied = target
        self.last_write_t = now
        self.history.append((now, target))
        if len(self.history) > 240:
            del self.history[:-240]
        note = "（回流阻尼）" if damped else ""
        return MappingDecision(ideal, target, f"曲线建议{ideal}%→限速到{target}%{note}", s,
                               self.same_dir_streak, self.force_cool_until, damped)

    # -- 外部事件 -------------------------------------------------------
    def note_manual_override(self, now: Optional[float] = None,
                             hold_s: float = 300.0) -> None:
        """用户手动改了亮度 → 一段时间内别再动。"""
        now = time.monotonic() if now is None else now
        self.force_cool_until = max(self.force_cool_until, now + hold_s)
        self.lux_smooth = None
        self.same_dir_streak = 0
        log.info("手动接管，暂停自动调光 %.0f 秒", hold_s)

    def adopted(self, level: int) -> None:
        """外部（用户/其它软件）把亮度改成了 level，同步内部状态。"""
        self.last_applied = int(level)
        self.last_dir = 0
        self.same_dir_streak = 0

    def oscillation_score(self, window_s: float = 120.0,
                          now: Optional[float] = None) -> int:
        """最近窗口内方向反转次数，>2 说明在摇。"""
        now = time.monotonic() if now is None else now
        pts = [lvl for t, lvl in self.history if now - t <= window_s]
        flips = 0
        for i in range(2, len(pts)):
            d1 = pts[i - 1] - pts[i - 2]
            d2 = pts[i] - pts[i - 1]
            if d1 * d2 < 0:
                flips += 1
        return flips
