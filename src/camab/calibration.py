"""自动校准：用户拖滑块定亮度，软件反推曲线。

**为什么需要**：分段线性曲线虽然可解释，但要用户自己去改 `mapping.anchors`
（"60 lux 给 60%"）还是太难 —— 没人知道自己的房间是多少 lux。
反过来做就简单多了：

1. 软件在当前光照下量出 lux，显示"现在环境 ≈ 62 lx"；
2. 用户拖一个滑块，选"这个光照下我想要 70% 亮度"；
3. 换个光照（开灯/关灯）重复一次；
4. 收集 2~3 个点，直接反推出折点 → 保存。

这样用户只需要回答"亮不亮"这个他一定会的问题，剩下的交给软件。

本模块只做纯计算（便于单元测试），不碰 GUI / 摄像头 / 亮度接口。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

log = logging.getLogger(__name__)

#: 采集点少于 2 个无法定曲线
MIN_POINTS = 2
#: 两个采集点的照度至少要差这么多倍，否则曲线会挤在一起
MIN_LUX_RATIO = 1.8
#: 亮度差至少要这么多，否则是同一个意图
MIN_LEVEL_GAP = 5


@dataclass
class Sample:
    """用户在某个光照下选定的一点。"""

    lux: float
    level: int
    label: str = ""

    def describe(self) -> str:
        return f"{self.label or '采样'}：{self.lux:.1f} lx → {self.level}%"


@dataclass
class CalibrationResult:
    """校准结果。"""

    anchors: list[tuple[float, int]] = field(default_factory=list)
    floor: int = 20
    ceil: int = 100
    ok: bool = False
    message: str = ""

    def describe(self) -> str:
        return "  ".join(f"{lux:.0f}lx→{lv}%" for lux, lv in self.anchors)


def build_anchors(samples: Sequence[Sample], floor: int = 20, ceil: int = 100,
                  span: float = 3.0) -> CalibrationResult:
    """把用户采的点变成折点表。

    :param samples: 用户的采样（自动按照度排序）
    :param floor: 最低亮度（低于最暗采样时用）
    :param ceil: 最高亮度（高于最亮采样时用）
    :param span: 两端各向外延伸多少倍照度，让采到的点不落在折点上

    生成的折点结构（以 2 个采样为例）::

        采样点        → 折点
        暗 a lx / X%  →  a/span → floor,  a → X
        亮 b lx / Y%  →  b → Y,           b*span → ceil
    """
    pts = sorted([s for s in samples if s.lux > 0], key=lambda s: s.lux)
    if len(pts) < MIN_POINTS:
        return CalibrationResult([], floor, ceil, False,
                                 f"至少要 {MIN_POINTS} 个采样点（现在 {len(pts)} 个）")

    lux_lo, lux_hi = pts[0].lux, pts[-1].lux
    if lux_hi / max(lux_lo, 1e-6) < MIN_LUX_RATIO:
        return CalibrationResult(
            [], floor, ceil, False,
            f"两次采样的光照太接近（{lux_lo:.0f} lx vs {lux_hi:.0f} lx），"
            f"至少要差 {MIN_LUX_RATIO:.1f} 倍；换个光照（开灯/关灯/拉窗帘）再采一次")

    levels = [max(floor, min(ceil, int(p.level))) for p in pts]
    if max(levels) - min(levels) < MIN_LEVEL_GAP:
        return CalibrationResult(
            [], floor, ceil, False,
            f"两次选的亮度几乎一样（{min(levels)}% vs {max(levels)}%），"
            f"至少要差 {MIN_LEVEL_GAP} 个百分点，否则曲线是平的")

    # 两端延伸：比最暗还暗 → 保持最低亮度；比最亮还亮 → 保持最高亮度
    anchors: list[tuple[float, int]] = [
        (round(max(0.2, lux_lo / span), 2), int(floor)),
    ]
    for p, lv in zip(pts, levels):
        anchors.append((round(p.lux, 2), int(lv)))
    anchors.append((round(lux_hi * span, 2), int(ceil)))

    # 去掉照度过近的重复点（clean_anchors 也会做，这里先做一次让结果更干净）
    out: list[tuple[float, int]] = []
    for lux, lv in anchors:
        if out and lux < out[-1][0] * 1.15:
            out[-1] = (out[-1][0], max(out[-1][1], lv))
            continue
        out.append((lux, lv))

    res = CalibrationResult(out, floor, ceil, True,
                            f"已根据 {len(pts)} 个采样点生成曲线")
    log.info("校准结果：%s", res.describe())
    return res


def explain(samples: Sequence[Sample]) -> str:
    """给用户看的解释（面板/CLI 都用）。"""
    if not samples:
        return "还没有采样点。"
    lines = [s.describe() for s in sorted(samples, key=lambda s: s.lux)]
    return "\n".join(lines)


def suggest_levels(lux: float, current: int, floor: int = 20,
                   ceil: int = 100) -> tuple[int, int]:
    """给滑块一个合理的初始建议值与范围。

    :return: ``(建议亮度, 滑块下限)``

    建议值来自"默认曲线在这个光照下会给多少"，但会用当前亮度做一点微调
    （70% 曲线 + 30% 现状），这样界面上显示的数值用户一眼能接受，
    不会一上来就跳到一个陌生数字。
    """
    lo = max(0, floor)
    hi = min(100, ceil)
    if hi <= lo:
        hi = min(100, lo + 20)
    lux = max(0.1, float(lux))
    frac = max(0.0, min(1.0, (math.log10(lux) - math.log10(2.0))
                            / (math.log10(1200.0) - math.log10(2.0))))
    curve = lo + (hi - lo) * frac
    if current and lo <= current <= hi:
        suggested = int(round(curve * 0.7 + current * 0.3))
    else:
        suggested = int(round(curve))
    return max(lo, min(hi, suggested)), lo
