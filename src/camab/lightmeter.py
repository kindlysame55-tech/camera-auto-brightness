"""测光：把一帧的统计量变成稳定的「等效照度」估计。

除了算 lux，这一层还负责三件工程上必须做的事：

1. **滚动中位数**：摄像头单帧噪声/瞬间闪灯不会直接改变亮度。
2. **曝光补偿**：能读到曝光/增益时用物理关系反解；读不到就记录 ``auto_exposure_drift``
   供上层判断「相机正在自己补偿，我的读数可能失真」。
3. **屏幕回流检测**：如果画面亮度与最近一次屏幕亮度调整高度相关，说明摄像头照到了屏幕，
   读数被污染 —— 交给引擎提高迟滞或降权。
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Iterable, Optional, Sequence

from .metrics import Analysis, AnalysisConfig, analyze_frame, lux_from_ratio

log = logging.getLogger(__name__)


@dataclass
class LuxEstimate:
    """一次测光结果。"""

    lux: float = 0.0
    raw_lux: float = 0.0
    ratio: float = 0.0
    exposure_s: Optional[float] = None
    gain: Optional[float] = None
    samples: int = 0
    auto_exposure_drift: float = 0.0   # >0 表示相机在把自己调亮（环境可能正在变暗）
    feedback_suspect: bool = False     # 疑似摄像头照到屏幕 → 存在正反馈
    stabilized: bool = False           # 滚动窗口是否已填满
    screen_compensated: bool = False   # 这一拍是否做了屏幕反光扣除
    compensation_reliable: bool = False  # 扣除后的环境光是否够可信

    def as_dict(self) -> dict:
        return {
            "lux": round(self.lux, 2), "ratio": round(self.ratio, 5),
            "samples": self.samples, "ae_drift": round(self.auto_exposure_drift, 4),
            "feedback": self.feedback_suspect, "stabilized": self.stabilized,
            "comp": self.screen_compensated, "comp_ok": self.compensation_reliable,
        }


@dataclass
class MeterConfig:
    window: int = 7                    # 滚动中位数窗口（帧数）
    ratio_floor: float = 0.0008
    ratio_ceil: float = 0.9
    #: 回流检测：最近屏幕亮度与画面亮度的相关系数阈值
    feedback_corr_threshold: float = 0.55
    feedback_min_samples: int = 4      # 参与相关的最少增量样本数


class LightMeter:
    """把连续的帧变成稳定的 lux 估计。"""

    def __init__(self, analysis: AnalysisConfig = AnalysisConfig(),
                 meter: MeterConfig = MeterConfig(), calib: float = 1.0,
                 lux_gamma: float = 1.0,
                 compensation=None) -> None:
        self.acfg = analysis
        self.mcfg = meter
        self.calib = calib
        self.lux_gamma = lux_gamma
        #: 屏幕反光扣除（:class:`~camab.reflection.ReflectionCompensation`）或 None
        self.compensation = compensation
        self._ratios: Deque[float] = deque(maxlen=max(1, meter.window))
        self._luxes: Deque[float] = deque(maxlen=max(1, meter.window))
        #: 每次 measure 的"平滑后画面亮度"时间序列（与 _levels 一一对应，用于回流检测）
        self._series: Deque[float] = deque(maxlen=32)
        #: (时间, 屏幕亮度) 历史，用于回流相关性
        self._levels: Deque[tuple[float, int]] = deque(maxlen=32)
        self._last_analysis: Optional[Analysis] = None
        self._ae_sign: int = 0
        self._ae_drift: float = 0.0
        self._prev_exposure: Optional[float] = None

    # -- 喂数据 ---------------------------------------------------------
    def observe_level(self, level: int, now: Optional[float] = None) -> None:
        """记录当前屏幕亮度（每次改完或读到后调用）。"""
        self._levels.append((time.monotonic() if now is None else now, int(level)))

    def reset(self) -> None:
        self._ratios.clear()
        self._luxes.clear()
        self._series.clear()
        self._levels.clear()
        self._last_analysis = None
        self._ae_drift = 0.0
        self._ae_sign = 0
        self._prev_exposure = None

    # -- 主入口 ---------------------------------------------------------
    def measure(self, frame, exposure_s: Optional[float] = None,
                gain: Optional[float] = None,
                level: Optional[int] = None) -> LuxEstimate:
        a = analyze_frame(frame, self._analysis_cfg())
        self._last_analysis = a

        # 曝光趋势：曝光时间变长说明环境在变暗（相机在自救）
        if exposure_s and exposure_s > 0:
            if self._prev_exposure and self._prev_exposure > 0:
                r = exposure_s / self._prev_exposure
                if r > 1.08:
                    self._ae_sign = -1
                elif r < 0.92:
                    self._ae_sign = 1
                self._ae_drift = 0.9 * self._ae_drift + 0.1 * (1.0 if r > 1 else (-1.0 if r < 1 else 0.0))
            self._prev_exposure = exposure_s

        # 屏幕反光扣除：把"屏幕自己造的光"从画面亮度里减掉，剩下的才是环境光。
        # 没标定过（compensation=None）时这一步是恒等变换。
        ratio_used = a.robust
        comp_reliable = False
        if self.compensation is not None and level is not None:
            ratio_used, comp_reliable = self.compensation.ambient_ratio(a.robust, level)

        raw = lux_from_ratio(ratio_used, exposure_s, gain, self.calib, self.lux_gamma)
        self._ratios.append(ratio_used)
        self._luxes.append(raw)

        # 滚动中位数（几何）
        med_ratio = _geo_median(self._ratios)
        med_lux = _geo_median(self._luxes)
        # 记录"这一拍看到的画面亮度"。这里刻意存**原始**比值而不是平滑值：
        # 平滑会把回流造成的台阶抹平，导致检测器永远沉默（实测踩到过）。
        # 反光扣除也刻意用原始值：回流检测看的就是"屏幕一动画面就动"。
        self._series.append(a.robust)
        est = LuxEstimate(
            lux=med_lux,
            raw_lux=raw,
            ratio=med_ratio,
            exposure_s=exposure_s,
            gain=gain,
            samples=len(self._ratios),
            auto_exposure_drift=self._ae_drift,
            feedback_suspect=self._feedback_suspect(),
            stabilized=len(self._ratios) >= self._ratios.maxlen,
            screen_compensated=self.compensation is not None,
            compensation_reliable=comp_reliable,
        )
        return est

    def _analysis_cfg(self) -> AnalysisConfig:
        return self.acfg

    # -- 回流检测 -------------------------------------------------------
    def _feedback_suspect(self) -> bool:
        """画面亮度的**增量**是否跟着屏幕亮度的**增量**一起动。

        为什么不用绝对值做相关：真实正反馈的因果链是「屏幕变亮 → 画面随后变亮」，
        表现为两个差分序列同向；而「房间慢慢变亮、我跟着把屏幕调亮」这种正常过程，
        其差分与屏幕亮度并不同步。早期版本用绝对值相关，把正常调光误判成回流、
        把亮度锁死在 91%（真机实测踩到），所以改成差分 + 滞后半拍对齐。
        """
        ok, corr = feedback_suspect(
            list(self._series),
            [lv for _t, lv in self._levels],
            threshold=self.mcfg.feedback_corr_threshold,
            min_samples=self.mcfg.feedback_min_samples,
        )
        if ok:
            log.debug("疑似屏幕回流：增量相关系数 %.2f", corr or 0.0)
        return ok

    # -- 诊断 -----------------------------------------------------------
    @property
    def last_analysis(self) -> Optional[Analysis]:
        return self._last_analysis


def _geo_median(vals: Iterable[float]) -> float:
    """几何中位数：对数量级差异大的亮度更合理，且天然平滑。"""
    v = sorted(max(1e-9, float(x)) for x in vals)
    if not v:
        return 0.0
    n = len(v)
    mid = v[n // 2] if n % 2 else math.sqrt(v[n // 2 - 1] * v[n // 2])
    return float(mid)


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx < 1e-12 or dy < 1e-12:
        return None
    return num / (dx * dy)


def _interp_prev(values: list[float]) -> list[float]:
    """按"上一次"的值重采样：第 i 项取 (v[i-1]+v[i])/2（备用工具，当前未被使用）。

    真实回流里"屏幕亮度已经变了"和"相机拍到变化"之间可能有半拍到几拍延迟；
    实测（见 notes）直接对同拍差分做相关已经足够区分，故保留此函数备用。
    """
    if len(values) < 2:
        return list(values)
    return [(values[i - 1] + values[i]) / 2.0 for i in range(1, len(values))]


def feedback_suspect(ratios: Sequence[float], levels: Sequence[float],
                     threshold: float = 0.6,
                     min_samples: int = 4,
                     min_level_delta: float = 2.0,
                     ) -> tuple[bool, Optional[float]]:
    """画面亮度增量与屏幕亮度增量是否同步（= 正反馈嫌疑）。

    做法：对两支序列做一阶差分，再把屏幕亮度序列按"滞后半拍"重采样，
    最后取受限的 Pearson 相关系数（Python 3.12+ 可用时优先用 ``statistics.correlation``）。

    :return: ``(是否可疑, 相关系数)``；样本不足或屏幕压根没动时返回 ``(False, None)``
    """
    n = min(len(ratios), len(levels))
    if n < min_samples:
        return False, None
    r = [float(v) for v in list(ratios)[-n:]]
    l = [float(v) for v in list(levels)[-n:]]

    # 屏幕亮度必须真的动过，否则谈不上回流
    if max(l) - min(l) < min_level_delta:
        return False, None

    d_r = [r[i] - r[i - 1] for i in range(1, len(r))]
    d_l = [l[i] - l[i - 1] for i in range(1, len(l))]
    m = min(len(d_r), len(d_l))
    if m < 3:
        return False, None
    try:
        corr = statistics.correlation(d_r[:m], d_l[:m])
    except (statistics.StatisticsError, ValueError):  # pragma: no cover - 常量序列
        corr = None
    if corr is None:
        corr = _pearson(d_r[:m], d_l[:m])
    if corr is None:
        return False, None
    return bool(corr >= threshold), float(corr)
