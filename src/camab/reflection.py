"""屏幕反光扣除。

**要解决的问题**：摄像头难免拍到屏幕本身。屏幕越亮 → 画面越亮 → 测出的"环境光"
越大 → 屏幕再变亮 …… 这是正反馈；即使没振荡，也会让曲线失真（读数里混进了
"我们自己造的光"），表现为"调取不够灵敏、也不线性"。

**做法**：把画面某点的亮度看成两项之和 ——

    L(屏幕亮度 b) ≈ A（环境光贡献） + k · b（屏幕反光贡献）

在用户校准的那一瞬间，把屏幕短暂压到两个不同亮度 b1/b2 各测一次，就能解出

    k = (L1 − L2) / (b1 − b2)

之后每一帧都用 ``A = L − k · b`` 反推真正的环境光，再用 A 去查曲线。

**边界**：
* 房间亮的时候 A 远大于 k·b，反推精度差 → 退回不扣（并标记不可靠）。
* k 必须 ≥0（屏幕变亮不该让画面变暗）；异常值直接拒绝。
* 只在用户显式跑过 ``camab calibrate-screen`` 之后才生效。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

#: k 的合理上限（每 1% 屏幕亮度对画面线性亮度的贡献）
MAX_K = 0.006
#: 环境光至少要占画面亮度的这个比例，否则认为反推不可靠
MIN_AMBIENT_SHARE = 0.12


@dataclass
class ReflectionCompensation:
    """一次标定得到的“屏幕反光系数”，用来反推环境光。"""

    k: float = 0.0              # 每 1% 屏幕亮度贡献的画面线性亮度
    calibrated: bool = False
    samples: int = 0
    note: str = ""

    # -- 反推 -----------------------------------------------------------
    def ambient_ratio(self, ratio: float, level: int) -> tuple[float, bool]:
        """从"画面亮度 + 当前屏幕亮度"反推环境光亮度。

        :return: ``(环境光线性亮度, 是否可靠)``；未标定或反推不可靠时原样返回
        """
        ratio = max(0.0, float(ratio))
        if not self.calibrated or self.k <= 0:
            return ratio, False
        screen_part = self.k * max(0, int(level))
        ambient = ratio - screen_part
        if ambient <= 0:
            # 扣成负数：说明 k 偏大或画面本来就比屏幕贡献还暗 → 当成"几乎全黑"
            return max(0.0, ratio * (1.0 - MIN_AMBIENT_SHARE)), False
        share = ambient / ratio if ratio > 0 else 1.0
        reliable = share >= MIN_AMBIENT_SHARE
        return ambient, reliable

    def as_dict(self) -> dict:
        return {"k": round(self.k, 6), "calibrated": self.calibrated,
                "samples": self.samples, "note": self.note}

    # -- 标定 -----------------------------------------------------------
    @staticmethod
    def estimate(pairs: list[tuple[int, float]],
                 min_level_gap: int = 25) -> "ReflectionCompensation":
        """用若干 (屏幕亮度%, 画面线性亮度) 观测点最小二乘拟合 k。

        :param pairs: 至少要有一高一低两档屏幕亮度
        :param min_level_gap: 两档亮度差太小则无法分辨，直接判失败
        """
        pts = [(int(b), float(r)) for b, r in pairs if r is not None]
        if len(pts) < 2:
            return ReflectionCompensation(0.0, False, len(pts), "观测点不足")
        levels = [b for b, _ in pts]
        gap = max(levels) - min(levels)
        if gap < min_level_gap:
            return ReflectionCompensation(
                0.0, False, len(pts),
                f"两档屏幕亮度只差 {gap}%，分辨不出反光分量（需要 ≥{min_level_gap}%）")

        n = len(pts)
        mb = sum(b for b, _ in pts) / n
        mr = sum(r for _, r in pts) / n
        den = sum((b - mb) ** 2 for b, _ in pts)
        if den <= 1e-9:
            return ReflectionCompensation(0.0, False, n, "屏幕亮度没有变化")
        k = sum((b - mb) * (r - mr) for b, r in pts) / den
        if k < 0:
            return ReflectionCompensation(
                0.0, False, n, "反光系数为负（屏幕变亮画面反而变暗），拒绝标定")
        if k > MAX_K:
            return ReflectionCompensation(
                MAX_K, True, n, f"反光系数 {k:.5f} 超过上限，已钳到 {MAX_K}")
        if k < 1e-6:
            return ReflectionCompensation(
                0.0, False, n, "几乎测不到屏幕反光（可能摄像头没拍到屏幕）")
        return ReflectionCompensation(k, True, n, f"反光系数 {k:.5f}/1%屏幕亮度")
