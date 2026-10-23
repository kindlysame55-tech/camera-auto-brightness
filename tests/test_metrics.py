"""单元测试：测光统计、lux 换算、映射曲线与状态机。

全部是纯逻辑测试，不需要摄像头、不需要 Windows 亮度接口。
运行：``.venv\\Scripts\\python.exe -m pytest tests -q``
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab.metrics import (  # noqa: E402
    PRESETS, Analysis, AnalysisConfig, BrightnessMapper, MappingConfig,
    analyze_frame, lux_from_ratio, luma_ratio,
)


# --------------------------------------------------------------------------- #
# 工具：造一帧「线性亮度 = ratio」的图
# --------------------------------------------------------------------------- #
LINEAR = AnalysisConfig(linearize=False, ignore_frac=0.0)


def linear_frame(ratio: float, size: tuple[int, int] = (60, 80)) -> np.ndarray:
    """float32 帧，像素值**就是**线性亮度（配合 linearize=False 使用）。"""
    return np.full((*size, 3), float(ratio), dtype=np.float32)


def srgb_frame(ratio: float, size: tuple[int, int] = (60, 80)) -> np.ndarray:
    """uint8 sRGB 帧：模拟摄像头真实输出，用于验证解码路径。"""
    lin = np.clip(ratio, 0.0, 1.0)
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    val = np.clip(srgb * 255.0, 0, 255).astype(np.uint8)
    return np.full((*size, 3), val, dtype=np.uint8)


def reference_luma_ratio(gray, cfg: AnalysisConfig) -> Analysis:
    """优化前的全排序/两次分位计算，用于校验统计语义和微基准。"""
    arr = np.asarray(gray)
    if np.issubdtype(arr.dtype, np.integer):
        arr = arr.astype(np.float32) / float(np.iinfo(arr.dtype).max)
    else:
        arr = arr.astype(np.float32)
        peak = float(arr.max())
        if peak > 1.5:
            arr = arr / (255.0 if peak > 64.0 else max(1.0, peak))
    arr = np.clip(arr, 0.0, 1.0)
    if arr.ndim == 3:
        if arr.shape[2] >= 3:
            arr = arr[..., 0] * 0.114 + arr[..., 1] * 0.587 + arr[..., 2] * 0.299
        else:
            arr = arr[..., 0]
    h, w = arr.shape[:2]
    c = int(round(min(h, w) * max(0.0, min(0.45, cfg.crop))))
    if c > 0 and h - 2 * c > 4 and w - 2 * c > 4:
        arr = arr[c:h - c, c:w - c]
    flat = arr.reshape(-1)
    if cfg.linearize:
        flat = np.clip(flat, 0.0, 1.0)
        flat = np.where(flat <= 0.04045, flat / 12.92,
                        ((flat + 0.055) / 1.055) ** 2.4)
    n = flat.size
    if n >= 32 and cfg.ignore_frac > 0:
        k = int(n * cfg.ignore_frac)
        if n - 2 * k > 8:
            flat = flat[np.argsort(flat)[k:n - k]]
    p05, p50, p95 = (float(v) for v in np.percentile(flat, (5.0, 50.0, 95.0)))
    robust = float(np.percentile(flat, max(0.0, min(100.0, cfg.percentile))))
    return Analysis(mean=float(flat.mean()), median=p50, robust=robust,
                    p05=p05, p95=p95, std=float(flat.std()))


def measurement_frame(kind: str, repeated: bool) -> np.ndarray:
    rng = np.random.default_rng(20261011)
    shape, dtype, scale = {
        "gray-u8": ((57, 83), np.uint8, 255),
        "bgr-u8": ((57, 83, 3), np.uint8, 255),
        "bgra-u8": ((57, 83, 4), np.uint8, 255),
        "gray-u16": ((57, 83), np.uint16, 65535),
        "float-unit": ((57, 83, 3), np.float32, 1),
        "float-255": ((57, 83, 3), np.float32, 255),
        "single-channel": ((57, 83, 1), np.float32, 1),
        "two-channels": ((57, 83, 2), np.float32, 1),
        "tiny": ((4, 6), np.uint8, 255),
        "31-pixels": ((1, 31), np.float32, 1),
        "32-pixels": ((4, 8), np.float32, 1),
        "crop-too-small": ((7, 9, 3), np.uint8, 255),
        "strided": ((57, 83, 3), np.uint8, 255),
    }[kind]
    values = rng.integers(0, 5, size=shape) / 4 if repeated else rng.random(shape)
    frame = (values * scale).astype(dtype)
    return frame[::2, ::3] if kind == "strided" else frame


# --------------------------------------------------------------------------- #
# 测光统计
# --------------------------------------------------------------------------- #
class TestLumaRatio:
    @pytest.mark.parametrize("kind", [
        "gray-u8", "bgr-u8", "bgra-u8", "gray-u16", "float-unit", "float-255",
        "single-channel", "two-channels", "tiny", "31-pixels", "32-pixels",
        "crop-too-small", "strided",
    ])
    @pytest.mark.parametrize("repeated", [False, True], ids=["random", "repeated"])
    @pytest.mark.parametrize("crop,linearize,ignore_frac,percentile", [
        (0.0, False, 0.0, 0.0), (0.1, True, 0.02, 15.0),
        (0.25, False, 0.2, 50.0), (0.45, True, 0.1, 95.0),
        (0.0, True, 0.01, 100.0), (0.1, False, 0.6, 35.0),
        (0.0, True, 0.02, -10.0), (0.0, False, 0.02, 110.0),
        (0.1, True, 0.02, 5.0),
    ])
    def test_matches_sorting_reference(self, kind, repeated, crop, linearize,
                                      ignore_frac, percentile):
        frame = measurement_frame(kind, repeated)
        cfg = AnalysisConfig(crop=crop, linearize=linearize, ignore_frac=ignore_frac,
                             percentile=percentile)
        actual = luma_ratio(frame, cfg)
        expected = reference_luma_ratio(frame, cfg)
        assert actual.as_dict() == pytest.approx(expected.as_dict(), rel=2e-6, abs=2e-7)

    @pytest.mark.parametrize("shape", [(0,), (0, 0), (0, 10, 3), (10, 0, 3), (10, 10, 0)])
    def test_empty_frames_return_zero_statistics(self, shape):
        assert luma_ratio(np.empty(shape, np.uint8)).as_dict() == Analysis().as_dict()

    @pytest.mark.parametrize("shape", [(), (10,), (3, 4, 2, 2)])
    def test_unsupported_frame_dimensions_raise_clear_error(self, shape):
        with pytest.raises(ValueError, match="二维灰度图或三维彩色图"):
            luma_ratio(np.zeros(shape, np.uint8))

    def test_analysis_does_not_modify_input_frame(self):
        frame = measurement_frame("strided", False)
        before = frame.copy()
        luma_ratio(frame, AnalysisConfig(crop=0.0, linearize=False, ignore_frac=0.2))
        np.testing.assert_array_equal(frame, before)

    def test_linear_roundtrip(self):
        for ratio in (0.002, 0.01, 0.05, 0.18, 0.5, 0.9):
            a = analyze_frame(linear_frame(ratio), LINEAR)
            assert a.robust == pytest.approx(ratio, rel=0.02), f"ratio={ratio}"

    def test_srgb_uint8_decode(self):
        """真实摄像头给的是 uint8 sRGB，解码后应接近原始线性值。"""
        for ratio in (0.02, 0.18, 0.5):
            a = analyze_frame(srgb_frame(ratio))
            assert a.robust == pytest.approx(ratio, rel=0.08, abs=0.004), f"ratio={ratio}"

    def test_monotonic(self):
        vals = [analyze_frame(linear_frame(r), LINEAR).robust
                for r in (0.001, 0.005, 0.02, 0.08, 0.3, 0.7)]
        assert all(b > a for a, b in zip(vals, vals[1:])), vals

    def test_accepts_gray_and_bgr_and_float(self):
        g = srgb_frame(0.2)
        a1 = analyze_frame(g)
        a2 = analyze_frame(g[..., 0])                     # 单通道
        a3 = analyze_frame(g.astype(np.float32) / 255.0)  # float 0-1
        a4 = analyze_frame(g.astype(np.float32))          # float 0-255
        for a in (a2, a3, a4):
            assert a.robust == pytest.approx(a1.robust, rel=0.08, abs=0.005)

    def test_robust_beats_glare(self):
        """画面里有一块强光（模拟屏幕/台灯）时，低分位比均值稳。"""
        img = np.zeros((120, 160, 3), np.float32)
        img[:, :] = 0.05
        img[:40, :40] = 1.0                            # 刺眼高光块（占 8%）
        a = analyze_frame(img, AnalysisConfig(percentile=30.0, ignore_frac=0.0,
                                              linearize=False))
        assert a.robust == pytest.approx(0.05, rel=0.05), a.robust
        assert a.mean > a.robust * 1.5, (a.mean, a.robust)
        assert a.std > 0.1

    def test_crop_ignores_border(self):
        img = np.full((60, 80, 3), 0.05, np.float32)
        img[:10, :] = 1.0                              # 边框高光
        a = analyze_frame(img, AnalysisConfig(crop=0.25, ignore_frac=0.0,
                                              linearize=False))
        assert a.robust == pytest.approx(0.05, rel=0.05), a.robust

    def test_empty_or_uniform(self):
        a = analyze_frame(np.zeros((10, 10, 3), np.uint8))
        assert a.robust == 0.0
        b = analyze_frame(np.full((10, 10, 3), 255, np.uint8))
        assert b.robust == pytest.approx(1.0, abs=0.02)


class TestLux:
    def test_reference_point(self):
        """18% 灰 @1/30s 增益1 → 200 lx（标定锚点）。"""
        assert lux_from_ratio(0.18, 1 / 30, 1.0) == pytest.approx(200.0, rel=0.02)

    def test_longer_exposure_means_darker_scene(self):
        """同样画面亮度，曝光越长说明环境越暗。"""
        bright = lux_from_ratio(0.2, 1 / 30, 1.0)
        dark = lux_from_ratio(0.2, 1 / 3, 1.0)
        assert dark < bright
        assert bright / dark == pytest.approx(10.0, rel=0.05)

    def test_darker_frame_gives_lower_lux(self):
        assert lux_from_ratio(0.02) < lux_from_ratio(0.2) < lux_from_ratio(0.6)

    def test_never_zero(self):
        assert lux_from_ratio(0.0) > 0
        assert lux_from_ratio(-5) > 0

    def test_calib_scales(self):
        assert lux_from_ratio(0.1, calib=2.0) == pytest.approx(
            2 * lux_from_ratio(0.1, calib=1.0), rel=1e-6)


# --------------------------------------------------------------------------- #
# 映射曲线（分段线性）
# --------------------------------------------------------------------------- #
ANCHORS = [(10.0, 20), (100.0, 60), (1000.0, 100)]


class TestCurve:
    def test_anchors_hit_exactly(self):
        cfg = MappingConfig(anchors=ANCHORS).clamped()
        for lux, lvl in ANCHORS:
            assert cfg.lux_to_level(lux) == lvl, f"{lux} lx 应恰好是 {lvl}%"

    def test_clamps_outside_range(self):
        cfg = MappingConfig(anchors=ANCHORS).clamped()
        assert cfg.lux_to_level(0.01) == 20      # 低于第一折点 → 取第一点
        assert cfg.lux_to_level(100000) == 100   # 高于最后折点 → 取最后一点

    def test_monotonic(self):
        cfg = MappingConfig(anchors=ANCHORS).clamped()
        luxes = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 400, 800, 1600, 5000]
        levels = [cfg.lux_to_level(l) for l in luxes]
        assert all(b >= a for a, b in zip(levels, levels[1:])), levels

    def test_piecewise_linear_in_log_lux(self):
        """每一段内：log 照度等距 → 亮度等距（这就是"线性"的含义）。"""
        cfg = MappingConfig(anchors=[(10.0, 20), (1000.0, 100)]).clamped()
        # 10 → 100 → 1000 是 log 等距的三点，中间点亮度应取中点 60
        assert cfg.lux_to_level(10) == 20
        assert cfg.lux_to_level(100) == 60
        assert cfg.lux_to_level(1000) == 100
        # 再做一次四等分，步长应基本一致（±1）
        pts = [cfg.lux_to_level(10 * (100 ** (i / 4))) for i in range(5)]
        steps = [pts[i + 1] - pts[i] for i in range(4)]
        assert max(steps) - min(steps) <= 1, (pts, steps)

    def test_default_curve_is_reasonable(self):
        """默认曲线对常见室内照度要给出手感正常的亮度。"""
        cfg = MappingConfig().clamped()
        cases = {2: (15, 30), 15: (30, 50), 60: (50, 72), 200: (72, 92),
                 500: (88, 100), 1200: (95, 100)}
        for lux, (lo, hi) in cases.items():
            lvl = cfg.lux_to_level(lux)
            assert lo <= lvl <= hi, f"{lux} lx → {lvl}%（期望 {lo}~{hi}%）"

    def test_default_curve_is_not_the_old_gamma_formula(self):
        """回归：新旧曲线都是单调的，但新曲线是显式折点，好调、可解释。

        这条测试把两件事钉死：① 两条曲线在常用区间不应该差得离谱（说明换曲线
        没把亮度整体带偏）；② 新曲线在 60 lx 这种"普通室内灯"下落在舒服区间。
        """
        old = MappingConfig(anchors=[], lux_dark=5.0, lux_bright=400.0,
                            gamma=0.45).clamped()
        new = MappingConfig().clamped()
        for lux in (10, 60, 200, 500):
            assert abs(old.lux_to_level(lux) - new.lux_to_level(lux)) <= 8, \
                f"{lux} lx 两条曲线差太多"
        assert 50 <= new.lux_to_level(60) <= 72, new.lux_to_level(60)

    def test_custom_anchors_respected(self):
        cfg = MappingConfig(anchors=[(1.0, 45), (50.0, 70)]).clamped()
        assert cfg.lux_to_level(1) == 45
        assert cfg.lux_to_level(50) == 70
        assert cfg.lux_to_level(0.5) == 45

    def test_anchor_cleanup(self):
        from camab.metrics import clean_anchors

        # 乱序 + 负照度 + 超范围亮度 + 过近重复点
        out = clean_anchors([(100, 60), (-5, 10), (10, 999), (10.5, 30), (1, 5)],
                            floor=0, ceil=100)
        luxes = [p[0] for p in out]
        assert luxes == sorted(luxes)
        assert all(lux > 0 for lux in luxes)
        assert all(0 <= lvl <= 100 for _lux, lvl in out)
        # 10 与 10.5 太近，被合并
        assert len([l for l in luxes if 9.5 < l < 11]) == 1

    def test_curve_points_fallback_without_anchors(self):
        """折点为空时按 floor/ceil 生成两点直线（老配置兼容）。"""
        cfg = MappingConfig(anchors=[], lux_dark=7.0, lux_bright=300.0,
                            floor=25, ceil=95).clamped()
        # 没有折点 → 用出厂形状按新范围生成
        pts = cfg.curve_points()
        assert pts[0][1] == 25 and pts[-1][1] == 95
        assert cfg.lux_to_level(0.5) == 25
        assert cfg.lux_to_level(100000) == 95

    def test_describe_curve(self):
        text = MappingConfig(anchors=ANCHORS).clamped().describe_curve()
        assert "10lx→20%" in text and "1000lx→100%" in text

    def test_clamped_keeps_sane(self):
        c = MappingConfig(floor=90, ceil=10, lux_dark=0, lux_bright=-5).clamped()
        assert c.ceil > c.floor
        assert c.lux_bright > c.lux_dark
        assert 0 <= c.floor <= 100 and 0 <= c.ceil <= 100

    def test_floor_and_ceil_are_two_way_knobs(self):
        """回归：floor/ceil 必须双向有效。

        旧实现把折点亮度钳进 [floor, ceil]，导致"调高有效、调低无效"——
        比如默认第一个折点是 20，把 floor 设成 10 完全没用（实测踩到）。
        """
        base = MappingConfig().clamped()
        low = MappingConfig(floor=10, ceil=100).clamped()
        high = MappingConfig(floor=45, ceil=100).clamped()
        # 调低 floor：暗端要能低于默认的 20
        assert low.lux_to_level(0.5) == 10
        assert low.lux_to_level(0.5) < base.lux_to_level(0.5)
        # 调高 floor：暗端升到 45
        assert high.lux_to_level(0.5) == 45
        assert high.lux_to_level(200) >= 45
        # ceil 双向
        assert MappingConfig(ceil=60).clamped().lux_to_level(5000) == 60

    def test_anchors_survive_out_of_range_floor_ceil(self):
        """折点保留原始意图，边界只在出口收口。"""
        cfg = MappingConfig(anchors=[(2, 20), (300, 95)], floor=5, ceil=100).clamped()
        assert cfg.curve_points() == [(2.0, 20), (300.0, 95)]
        assert cfg.lux_to_level(100000) == 95


class TestPresets:
    def test_presets_are_ordered(self):
        """灵敏 → 均衡 → 保守：周期越来越长、迟滞越来越大、过渡越来越慢。"""
        s, b, c = PRESETS["sensitive"], PRESETS["balanced"], PRESETS["calm"]
        assert s["interval_s"] < b["interval_s"] < c["interval_s"]
        assert s["deadband"] < b["deadband"] < c["deadband"]
        assert s["cooldown_s"] < b["cooldown_s"] < c["cooldown_s"]
        assert s["max_step"] > b["max_step"] > c["max_step"]
        assert s["ramp_per_s"] > b["ramp_per_s"] > c["ramp_per_s"], "变化速度要跟着变"

    def test_default_matches_balanced(self):
        cfg = MappingConfig()
        b = PRESETS["balanced"]
        assert cfg.deadband == b["deadband"]
        assert cfg.cooldown_s == b["cooldown_s"]
        assert cfg.max_step == b["max_step"]
        assert cfg.ramp_per_s == b["ramp_per_s"]


class TestSpeedMapping:
    def test_speed_slider_roundtrip(self):
        from camab.metrics import RAMP_FAST, RAMP_SLOW, ramp_from_speed, speed_from_ramp

        for speed in (0, 25, 50, 75, 100):
            ramp = ramp_from_speed(speed)
            assert RAMP_SLOW <= ramp <= RAMP_FAST
            assert abs(speed_from_ramp(ramp) - speed) <= 2, f"往返不一致：{speed} → {ramp}"

    def test_bigger_slider_means_smoother(self):
        from camab.metrics import ramp_from_speed

        assert ramp_from_speed(0) > ramp_from_speed(50) > ramp_from_speed(100)


class TestRampRate:
    """变化速度：用户要的"调节快慢"就是这条。"""

    def mk(self, **kw):
        base = dict(floor=20, ceil=100, anchors=[(1.0, 20), (400.0, 100)],
                    deadband=0, cooldown_s=0.0, min_write_interval_s=0.0,
                    max_step=100, ema_alpha=1.0, ramp_per_s=10.0)
        base.update(kw)
        return BrightnessMapper(MappingConfig(**base))

    def test_ramp_limits_step_by_elapsed_time(self):
        """10%/s 且过了 1 秒 → 这一拍最多走 10 个点。"""
        m = self.mk(ramp_per_s=10.0)
        d = m.decide(lux=400.0, current=20, now=100.0)   # 第一次写：无历史间隔
        assert d.applied is not None
        start = d.applied
        d2 = m.decide(lux=400.0, current=start, now=101.0)   # 过了 1 秒
        assert d2.applied is not None
        assert d2.applied - start <= 10, f"超过 10%/s：{start} → {d2.applied}"

    def test_slower_ramp_moves_less(self):
        """同一个初始条件：慢速档这一拍走得更少。

        注意第一次写没有"上一拍时间"，所以两者都会被最小写入间隔限制；
        这里手动给一个已过去的时间，模拟"已经在调速过程中"。
        """
        fast = self.mk(ramp_per_s=40.0, max_step=100)
        slow = self.mk(ramp_per_s=3.0, max_step=100)
        fast.last_write_t = 100.0        # 假装 1 秒前写过
        slow.last_write_t = 100.0
        f = fast.decide(lux=400.0, current=20, now=101.0).applied
        s = slow.decide(lux=400.0, current=20, now=101.0).applied
        assert s - 20 < f - 20, f"慢速档走得不比快速档少：{s} vs {f}"
        assert (f - 20) <= 40 and (s - 20) <= 3

    def test_zero_ramp_means_no_limit(self):
        m = self.mk(ramp_per_s=0.0, max_step=15)
        m.last_write_t = 1000.0           # 已经过去很久，ramp 不该再起作用
        d = m.decide(lux=400.0, current=20, now=1000.1)
        assert d.applied == 35, "ramp=0 时应该只受 max_step 限制"

    def test_min_write_interval_blocks_too_frequent_writes(self):
        m = self.mk(min_write_interval_s=2.0, cooldown_s=0.0, ramp_per_s=0.0, max_step=20)
        # 先假装刚刚写过（last_write_t 是真实时间戳才会走间隔判定）
        m.last_write_t = 50.0
        m.last_applied = 20
        d2 = m.decide(lux=400.0, current=20, now=50.5)
        assert d2.applied is None and "间隔" in d2.reason
        d3 = m.decide(lux=400.0, current=20, now=53.0)
        assert d3.applied is not None

    def test_reaches_target_eventually(self):
        """限速只是慢，不能永远到不了。"""
        m = self.mk(ramp_per_s=8.0, max_same_dir=99)
        cur, t = 20, 100.0
        for _ in range(40):
            d = m.decide(lux=400.0, current=cur, now=t)
            t += 1.0
            if d.applied is not None:
                cur = d.applied
        assert cur >= 95, f"40 秒还没走到目标：{cur}"


# --------------------------------------------------------------------------- #
# 状态机：迟滞 / 冷却 / 限速 / 同向保护
# --------------------------------------------------------------------------- #
class TestMapper:
    def mk(self, **kw) -> BrightnessMapper:
        # 用两点折点，等价于旧的 (lux_dark→floor, lux_bright→ceil) 直线。
        # ramp/minute 间隔默认关掉，这样断言里的数字只受被测机制影响
        # （变化速度单独在 TestRampRate 里测）。
        base = dict(floor=10, ceil=100, anchors=[(5.0, 10), (400.0, 100)],
                    deadband=6, cooldown_s=8.0, max_step=100, ema_alpha=1.0,
                    ramp_per_s=0.0, min_write_interval_s=0.0)
        base.update(kw)
        return BrightnessMapper(MappingConfig(**base))

    def test_deadband_blocks_small_change(self):
        m = self.mk()
        # 目标与当前只差 1% → 不动
        d = m.decide(lux=10.0, current=m.cfg.lux_to_level(10.0) + 1, now=100.0)
        assert d.applied is None and "迟滞" in d.reason

    def test_big_change_applies(self):
        m = self.mk()
        d = m.decide(lux=400.0, current=10, now=100.0)
        assert d.applied is not None and d.applied > 10

    def test_cooldown_blocks_second_write(self):
        m = self.mk(cooldown_s=30.0)
        d1 = m.decide(lux=400.0, current=10, now=100.0)
        assert d1.applied is not None
        d2 = m.decide(lux=400.0, current=d1.applied, now=105.0)
        assert d2.applied is None and "冷却" in d2.reason
        # 冷却结束后：把当前值调离目标，应能再次写入
        d3 = m.decide(lux=400.0, current=d1.applied - 20, now=200.0)
        assert d3.applied is not None, d3.reason

    def test_max_step_limits_jump(self):
        m = self.mk(max_step=8)
        d = m.decide(lux=400.0, current=10, now=100.0)
        assert d.applied == 18  # 10 + 8

    def test_same_direction_guard_only_when_damped(self):
        """连续同向刹车只在"疑似回流"时生效，正常单调调光不该被挡。"""
        # 正常模式：一直朝同一方向调，不该被强制冷却
        m = self.mk(max_step=5, max_same_dir=3, cooldown_s=1.0)
        cur, t = 10, 100.0
        applied = 0
        for _ in range(10):
            d = m.decide(lux=400.0, current=cur, now=t, damped=False)
            t += 2.0
            if d.applied is not None:
                applied += 1
                cur = d.applied
        assert applied == 10, f"正常调光不该被刹车，实际只调了 {applied} 次"
        assert m.force_cool_until == 0.0

    def test_same_direction_guard_brakes_when_damped(self):
        """判定为回流时，连续同向太多次要强制冷却。"""
        m = self.mk(max_step=5, max_same_dir=3, cooldown_s=1.0)
        cur, t = 10, 100.0
        blocked = 0
        for _ in range(10):
            d = m.decide(lux=400.0, current=cur, now=t, damped=True)
            t += 2.0
            if d.applied is not None:
                cur = d.applied
            else:
                blocked += 1
        assert blocked >= 2, f"阻尼模式下同向保护没生效（blocked={blocked}）"
        assert m.force_cool_until > 0

    def test_damping_shrinks_step(self):
        """阻尼模式：同样的差距只走更小的步。"""
        normal = self.mk(max_step=20, cooldown_s=0)
        damped = self.mk(max_step=20, cooldown_s=0, feedback_damping=0.25)
        d1 = normal.decide(lux=400.0, current=10, now=100.0, damped=False)
        d2 = damped.decide(lux=400.0, current=10, now=100.0, damped=True)
        assert d1.applied == 30          # 10 + 20
        assert d2.applied == 15          # 10 + 5
        assert d2.damped is True and "阻尼" in d2.reason

    def test_damping_is_not_a_deadlock(self):
        """阻尼只是变慢，不能把亮度锁死：持续给同样的光，最终要能到达目标。"""
        m = self.mk(max_step=20, cooldown_s=0, feedback_damping=0.25, max_same_dir=99)
        cur, t = 10, 100.0
        for _ in range(40):
            d = m.decide(lux=400.0, current=cur, now=t, damped=True)
            t += 1.0
            if d.applied is not None:
                cur = d.applied
        assert cur >= 95, f"阻尼模式下最终没能跟上光照：{cur}"

    def test_damping_releases_after_quiet_frames(self):
        m = self.mk(cooldown_s=0, deadband=0)
        m.decide(lux=400.0, current=10, now=100.0, damped=True)
        assert m.damped is True
        for i in range(3):
            m.decide(lux=400.0, current=10, now=200.0 + i, damped=False)
        assert m.damped is False

    def test_ema_smooths_spike(self):
        m = self.mk(ema_alpha=0.2, deadband=0)
        m.smooth(10.0)
        s = m.smooth(1000.0)
        assert s < 100.0      # 一次强光不该直接拉满

    def test_manual_override_holds(self):
        m = self.mk(cooldown_s=1.0)
        m.note_manual_override(now=100.0, hold_s=300.0)
        d = m.decide(lux=400.0, current=10, now=150.0)
        assert d.applied is None and "冷却" in d.reason

    def test_oscillation_score_counts_flips(self):
        m = self.mk(deadband=0, cooldown_s=0)
        for i, lvl in enumerate([20, 30, 20, 30, 20]):
            m.history.append((100.0 + i, lvl))
        assert m.oscillation_score(window_s=1000, now=110.0) >= 3

    def test_reset_forgets_state(self):
        m = self.mk()
        m.decide(lux=400.0, current=10, now=100.0)
        m.reset(current=55)
        assert m.last_write_t < 0 and m.same_dir_streak == 0 and m.last_applied == 55
