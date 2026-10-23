"""端到端（合成）测试：用假摄像头 + 假屏幕跑完整的闭环引擎。

这两条测试是整个项目最重要的回归保护：
``test_end_to_end_dark_to_bright`` 会因为任何一处逻辑错误（曲线反向、决策不写、
状态机卡死）而失败，不需要真摄像头。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab.brightness import BrightnessBackend, DryRunBackend, MonitorInfo  # noqa: E402
from camab.camera import CameraStats, Frame  # noqa: E402
from camab.config import AppConfig  # noqa: E402
from camab.engine import Engine  # noqa: E402
from camab.lightmeter import LightMeter, feedback_suspect  # noqa: E402
from camab.metrics import AnalysisConfig, MappingConfig  # noqa: E402

from test_metrics import srgb_frame  # noqa: E402


# --------------------------------------------------------------------------- #
# 假件
# --------------------------------------------------------------------------- #
class FakeCamera:
    """按脚本给出「环境线性亮度」的假摄像头。"""

    def __init__(self, ratio: float = 0.02) -> None:
        self.ratio = ratio
        # 用真的 CameraStats，避免假件漏字段（引擎会读 signal_ok / success_rate）
        self.stats = CameraStats(backend="FAKE", opened=True)
        self.stats.ok = 1          # success_rate 是只读属性，靠 ok/fail 算

    def read(self, timeout: float = 1.0):
        img = srgb_frame(self.ratio)
        # 真实 Camera.read() 会顺手更新黑帧/信号状态，假件也要模拟这一步
        self._update_stats(img)
        return Frame(img, time.time(), None, None, 1)

    def _update_stats(self, img) -> None:
        import numpy as np

        arr = np.asarray(img)
        if float(arr.max()) < 8.0 and float(arr.mean()) < 0.5:
            self.stats.black_streak += 1
        else:
            self.stats.black_streak = 0
        self.stats.mark_signal(self.stats.black_streak < 3)

    def open(self, timeout: float = 5.0) -> bool:
        return True

    def close(self) -> None:
        pass


class ScriptedCamera(FakeCamera):
    """按 tick 序号切换环境亮度，用来测「变暗 → 屏幕应变暗」。"""

    def __init__(self, script: list[float]) -> None:
        super().__init__(script[0])
        self.script = script
        self.i = 0

    def read(self, timeout: float = 1.0):
        self.ratio = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return super().read(timeout)


class CountingBackend(BrightnessBackend):
    """记录每次写入的假屏幕（functional=True，会真的"改"）。"""

    name = "fake"
    functional = True

    def __init__(self, initial: int = 50) -> None:
        self.level = initial
        self.writes: list[int] = []

    def available(self) -> bool:
        return True

    def monitors(self):
        return [MonitorInfo(self.name, 0, "fake:0", "假屏幕", self.level)]

    def get(self, key: str) -> int:
        return self.level

    def set(self, key: str, level: int) -> int:
        self.level = int(max(0, min(100, level)))
        self.writes.append(self.level)
        return self.level


def make_engine(cam, backend, **mapping) -> Engine:
    cfg = AppConfig()
    # 测试统一用两点折点（等价于旧的"直线"），让断言里的数字可预测；
    # ramp/写入间隔默认关掉，这样"几拍内到达目标"的断言不被限速影响
    # （变化速度单独在 test_metrics.TestRampRate 里测）。
    params = dict(floor=15, ceil=100, anchors=[(5.0, 15), (300.0, 100)],
                  deadband=4, cooldown_s=0.0, max_step=100,
                  ema_alpha=1.0, max_same_dir=100,
                  ramp_per_s=0.0, min_write_interval_s=0.0)
    params.update(mapping)
    cfg.mapping = MappingConfig(**params)
    cfg.engine.interval_s = 0.01
    cfg.engine.warmup_s = 0.0
    eng = Engine(cfg, backend=backend)
    eng._backend = backend
    eng._monitor = backend.monitors()[0]
    eng._camera = cam
    eng._t0 = time.monotonic()
    return eng


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #
class TestEndToEnd:
    def test_dark_to_bright_raises_brightness(self):
        cam = ScriptedCamera([0.002, 0.02, 0.30, 0.30])
        be = CountingBackend(initial=20)
        eng = make_engine(cam, be)

        levels = []
        for _ in range(6):
            eng.tick()
            levels.append(be.level)

        assert levels[0] < levels[-1], f"环境变亮屏幕却没变亮：{levels}"
        assert be.level >= 70, f"强光下应接近上限，实际 {be.level}"
        assert eng.status.writes >= 1

    def test_bright_to_dark_lowers_brightness(self):
        cam = ScriptedCamera([0.30, 0.30, 0.002, 0.002])
        be = CountingBackend(initial=95)
        eng = make_engine(cam, be)
        for _ in range(6):
            eng.tick()
        assert be.level <= 35, f"环境变暗屏幕却没变暗：{be.level}"

    def test_no_write_when_stable(self):
        """光照稳定时不能反复写 —— 这是"不抖"的核心验收点。

        构造：初始亮度 40%，环境亮度对应的目标是 28%（差距 > 迟滞带），
        因此第一拍写一次；之后目标与当前只差在迟滞带内，再也不能写。
        """
        cam = FakeCamera(0.01)
        be = CountingBackend(initial=40)
        eng = make_engine(cam, be, deadband=6, cooldown_s=0.0)
        eng.tick()                                  # 允许它调一次
        first = len(be.writes)
        assert first == 1, f"首拍应写一次，实际 {be.writes}"
        for _ in range(20):
            eng.tick()
        assert len(be.writes) == first, f"稳定光照下写了 {len(be.writes)} 次：{be.writes}"
        assert eng.mapper.oscillation_score(window_s=1e6) == 0

    def test_pause_blocks_writes(self):
        cam = ScriptedCamera([0.002, 0.30])
        be = CountingBackend(initial=20)
        eng = make_engine(cam, be)
        eng.pause("测试")
        for _ in range(5):
            eng.tick()
        assert be.writes == []
        eng.resume()
        for _ in range(5):
            eng.tick()
        assert be.writes, "恢复后应该重新开始调光"

    def test_dry_run_does_not_touch_real_screen(self):
        cam = FakeCamera(0.30)
        be = CountingBackend(initial=20)
        eng = make_engine(cam, be)
        eng.cfg.engine.dry_run = True
        for _ in range(4):
            eng.tick()
        assert be.writes == []
        assert eng.status.writes >= 1, "干跑也要记录决策"

    def test_dry_run_does_not_mistake_own_write_for_manual_change(self):
        """回归：干跑时引擎会记下"自己以为的亮度"。

        不记的话，下一拍 read_level() 读到真实值（没被改过），
        就会被 _detect_manual 误判成"用户手动改了亮度"，于是暂停自动调光 ——
        表现为"自动调光调了一下就再也不动了"（真机踩到）。
        """
        cam = FakeCamera(0.30)
        be = CountingBackend(initial=20)
        eng = make_engine(cam, be)
        eng.cfg.engine.dry_run = True
        eng.tick()
        first_write = eng.status.writes
        assert first_write >= 1
        for _ in range(5):
            eng.tick()
        assert eng.status.message != "检测到外部改动，暂停自动"
        assert eng.hold_remaining() == 0.0, "干跑不该触发暂停"

    def test_external_change_hold_is_short_and_capped(self):
        """外部改动导致的暂停必须短、且不能无限续期（否则看起来就是"失灵"）。"""
        from camab.engine import EXTERNAL_HOLD_MAX_S

        cam = FakeCamera(0.30)
        be = CountingBackend(initial=50)
        eng = make_engine(cam, be)
        assert eng.cfg.engine.external_hold_s <= 30, "外部改动暂停时长要短"
        assert EXTERNAL_HOLD_MAX_S <= 120, "要有续期上限"

        eng.tick()
        be.level = 90                       # 假装屏幕被别的程序/电源计划改了
        eng.tick()
        assert eng.hold_remaining() > 0
        # 连续多拍都看到"外部改动"时，最多续到上限就不再续
        for _ in range(8):
            be.level = be.level - 1 if be.level > 85 else 85
            eng.tick()
        assert eng.hold_remaining() <= EXTERNAL_HOLD_MAX_S

    def test_manual_override_is_respected(self):
        cam = FakeCamera(0.30)
        be = CountingBackend(initial=20)
        eng = make_engine(cam, be)
        eng.tick()
        eng.set_level(77, manual=True)
        assert be.level == 77
        writes_before = len(be.writes)
        for _ in range(5):
            eng.tick()
        assert len(be.writes) == writes_before, "手动接管期间不应自动改亮度"
        assert be.level == 77

    def test_hold_remaining_reports_countdown(self):
        """暂停剩余时间要能被界面读到（用户靠它判断"是不是坏了"）。"""
        cam = FakeCamera(0.30)
        be = CountingBackend(initial=40)
        eng = make_engine(cam, be)
        assert eng.hold_remaining() == 0.0, "没暂停时应该是 0"
        eng.set_level(70, manual=True)
        left = eng.hold_remaining()
        assert 0 < left <= eng.cfg.engine.manual_hold_s
        # 手动暂停时长默认 60s（原 300s 太长，用户会误判成失灵）
        assert eng.cfg.engine.manual_hold_s <= 120

    def test_manual_hold_can_be_disabled(self):
        """manual_hold_s = 0 时，用户调完亮度后自动调光要立刻继续接管。"""
        cam = FakeCamera(0.30)
        be = CountingBackend(initial=40)
        eng = make_engine(cam, be)
        eng.cfg.engine.manual_hold_s = 0.0
        eng.set_level(70, manual=False)
        for _ in range(4):
            eng.tick()
        assert be.level != 70, "关掉暂停后应该继续自动调光"

    def test_external_change_detected(self):
        """别人（Fn 键/电源计划）改了亮度 → 引擎应交出控制权，不当场掰回去。"""
        cam = FakeCamera(0.30)
        be = CountingBackend(initial=50)
        eng = make_engine(cam, be)
        eng.tick()
        be.level = 80                      # 外部改动，引擎还不知道
        writes_before = len(be.writes)
        eng.tick()                         # 这一步应该察觉到
        assert be.level == 80, f"察觉到外部改动的那一拍就掰回去了：{be.level}"
        for _ in range(4):
            eng.tick()
        assert be.level == 80, f"手动接管期间不应自动改亮度：{be.level}"
        assert len(be.writes) == writes_before

    def test_camera_failure_tolerated(self):
        class DeadCamera(FakeCamera):
            def read(self, timeout: float = 1.0):
                return None

        be = CountingBackend(50)
        eng = make_engine(DeadCamera(), be)
        eng.tick()
        assert eng.status.errors >= 1
        assert eng.status.camera_ok in (False, True)   # 不崩即通过

    def test_write_readback_mismatch_is_detected(self):
        """驱动/显示器静默忽略写入时，要以真实回读值为准并计数。"""

        class StubbornBackend(CountingBackend):
            """写入被忽略：永远停留在初始值。"""

            def set(self, key: str, level: int) -> int:
                self.writes.append(int(level))
                return self.level          # 没真的变

        cam = FakeCamera(0.30)
        be = StubbornBackend(initial=40)
        eng = make_engine(cam, be)
        eng.tick()
        assert eng.status.write_mismatch >= 1
        assert eng.status.current == 40, "状态里的当前亮度应该是回读到的真实值"

    def test_black_frames_hold_brightness(self):
        """摄像头间歇掉线（连续黑帧）时不能把屏幕压暗，必须保持不动。

        实测踩到过：黑帧被当成"天黑了"，引擎一路把亮度压到 20% 再也升不回来。
        构造：环境本来就偏暗（目标≈23%），屏幕停在 70% 不写；随后摄像头掉线全黑。
        """
        import numpy as np

        class FlakyCamera(FakeCamera):
            """前 3 帧正常（暗），之后突然全黑（模拟掉线）。"""

            def __init__(self) -> None:
                super().__init__(0.02)
                self.n = 0

            def read(self, timeout: float = 1.0):
                self.n += 1
                if self.n > 3:
                    img = np.zeros((48, 64, 3), np.uint8)
                    self._update_stats(img)
                    return Frame(img, time.time(), None, None, self.n)
                return super().read(timeout)

        be = CountingBackend(initial=70)
        eng = make_engine(FlakyCamera(), be, anchors=[(1.0, 60), (100.0, 100)],
                          deadband=8, cooldown_s=0.0)
        # 先让它在前 3 帧（画面正常、光照稳定）里自己收敛到稳态
        for _ in range(3):
            eng.tick()
        settled = be.level
        writes_before = len(be.writes)
        # 之后摄像头掉线 → 全黑，亮度必须一动不动
        for _ in range(8):
            eng.tick()
        assert be.level == settled, f"黑帧期间亮度被改动了：{settled} → {be.level}"
        assert len(be.writes) == writes_before, "黑帧期间不该有任何写入"
        assert "全黑" in eng.status.message
        assert eng._camera.stats.signal_ok is False

    def test_recovers_after_signal_returns(self):
        """画面恢复后要能继续正常调光，不能卡在保护状态里。"""
        import numpy as np

        class RecoveringCamera(FakeCamera):
            """先暗 → 中途掉线全黑 → 恢复亮。"""

            def __init__(self) -> None:
                super().__init__(0.02)
                self.n = 0

            def read(self, timeout: float = 1.0):
                self.n += 1
                if 3 < self.n <= 9:
                    img = np.zeros((48, 64, 3), np.uint8)
                    self._update_stats(img)
                    return Frame(img, time.time(), None, None, self.n)
                if self.n > 9:
                    self.ratio = 0.6       # 恢复后是大白天
                return super().read(timeout)

        be = CountingBackend(initial=70)
        eng = make_engine(RecoveringCamera(), be, anchors=[(1.0, 60), (100.0, 100)],
                          deadband=4, cooldown_s=0.0, max_step=16)
        for _ in range(20):
            eng.tick()
        assert eng._camera.stats.signal_ok is True
        assert be.level >= 95, f"画面恢复（大白天）后没有重新调亮：{be.level}"


class TestAdaptiveInterval:
    """自适应采样节奏：刚调过就快回来，稳定就放慢。"""

    def _engine(self):
        cam = FakeCamera(0.30)
        be = CountingBackend(initial=20)
        eng = make_engine(cam, be)
        eng.cfg.engine.interval_s = 4.0
        return eng

    def test_faster_after_write(self):
        from camab.status import Status

        eng = self._engine()
        st = Status(current=20, ideal=80, applied=32)
        assert eng._interval_after(st) == 2.0        # base/2

    def test_base_when_target_far(self):
        from camab.status import Status

        eng = self._engine()
        st = Status(current=20, ideal=80, applied=None)
        assert eng._interval_after(st) == 4.0

    def test_slower_when_settled(self):
        from camab.status import Status

        eng = self._engine()
        st = Status(current=50, ideal=52, applied=None)
        assert eng._interval_after(st) == 6.0        # base*1.5

    def test_disabled_uses_base(self):
        from camab.status import Status

        eng = self._engine()
        eng.cfg.engine.adaptive_interval = False
        assert eng._interval_after(Status(current=20, ideal=80, applied=32)) == 4.0

    def test_never_below_one_second(self):
        from camab.status import Status

        eng = self._engine()
        eng.cfg.engine.interval_s = 0.5
        assert eng._interval_after(Status(applied=30)) >= 1.0


# --------------------------------------------------------------------------- #
# 全闭环回归：合成光序列 → 真·跟随
# --------------------------------------------------------------------------- #
class TestFullLoopTracking:
    """把"光照变化 → 屏幕亮度跟随"整条链路钉死。

    这是整个项目的核心行为，任何一环改坏（曲线反向、决策不写、状态机卡死、
    回流保护过度）都会在这里失败。
    """

    TICKS = 40

    def _run(self, ticks: int = TICKS):
        import math

        DARK, BRIGHT = 10.0, 400.0

        def lux_at(i: int) -> float:
            frac = 0.5 - 0.5 * math.cos(2 * math.pi * i / max(2, ticks))
            return math.exp(math.log(DARK) + (math.log(BRIGHT) - math.log(DARK)) * frac)

        def ratio_for(lux: float) -> float:
            """lux → 线性亮度比（与 metrics.lux_from_ratio 互逆，曝光取参考值）。"""
            return min(0.9, max(0.001, lux / 200.0 * 0.18))

        cam = ScriptedCamera([ratio_for(lux_at(i)) for i in range(ticks)])
        be = CountingBackend(initial=60)
        cfg = AppConfig()
        cfg.mapping = MappingConfig(floor=20, ceil=100, anchors=[(1.0, 20), (400.0, 100)],
                                    deadband=4, cooldown_s=0.0, max_step=12,
                                    ema_alpha=0.45, ramp_per_s=0.0,
                                    min_write_interval_s=0.0)
        cfg.engine.interval_s = 0.01
        cfg.engine.warmup_s = 0.0
        eng = Engine(cfg, backend=be)
        eng._backend = be
        eng._monitor = be.monitors()[0]
        eng._camera = cam
        eng._t0 = time.monotonic()
        eng._mapper.reset(current=be.level)
        rows = []
        for _ in range(ticks):
            st = eng.tick()
            rows.append(st)
            # 极速模式：手动推进冷却时钟，模拟时间流逝
            eng._mapper.last_write_t -= 0.01
        return rows, eng, be

    def test_follows_light_up_and_down(self):
        rows, eng, be = self._run()
        bright = [r.current for r in rows if r.lux > 250]
        dim = [r.current for r in rows if r.lux < 30]
        assert bright and dim, "合成序列没有覆盖到亮/暗两端"
        assert max(bright) >= 80, f"强光下没推到高档：峰值 {max(bright)}%"
        assert min(dim) <= 60, f"弱光下没降下来：最低 {min(dim)}%"
        assert max(bright) - min(dim) >= 25, \
            f"亮暗响应差距太小：{max(bright)}% vs {min(dim)}%"

    def test_tracks_curve_not_just_direction(self):
        """不只是方向对：末段亮度要贴着曲线目标走。"""
        rows, eng, be = self._run()
        tail = rows[-6:]
        for r in tail:
            assert abs(r.current - r.ideal) <= 14, \
                f"偏离曲线太远：当前 {r.current}% vs 理想 {r.ideal}%"

    def test_no_oscillation(self):
        rows, eng, be = self._run()
        levels = [r.current for r in rows]
        flips = 0
        for i in range(2, len(levels)):
            d1 = levels[i - 1] - levels[i - 2]
            d2 = levels[i] - levels[i - 1]
            if d1 * d2 < 0:
                flips += 1
        assert flips <= 3, f"亮度来回摇摆 {flips} 次：{levels}"
        assert eng.mapper.oscillation_score(window_s=1e6) <= 3

    def test_writes_are_throttled(self):
        rows, eng, be = self._run()
        assert eng.status.writes <= self.TICKS * 0.8, \
            f"写入太频繁：{eng.status.writes}/{self.TICKS}"
        assert eng.status.errors == 0

    def test_converges_near_curve_target(self):
        """最终亮度要落在曲线目标附近（不能偏太多，也不能死死锁在起点）。"""
        rows, eng, be = self._run()
        last = rows[-1]
        assert abs(last.current - last.ideal) <= 12, \
            f"末拍偏离目标太多：当前 {last.current}% vs 理想 {last.ideal}%"


# --------------------------------------------------------------------------- #
# 测光器
# --------------------------------------------------------------------------- #
class TestLightMeter:
    def test_smooths_single_spike(self):
        m = LightMeter(analysis=AnalysisConfig(), calib=1.0)
        base = m.measure(srgb_frame(0.02)).lux
        m.measure(srgb_frame(0.02))
        spike = m.measure(srgb_frame(0.9)).lux
        assert spike < base * 20, f"单帧强光不该立刻拉满：{base}→{spike}"

    def test_exposure_compensation(self):
        """画面一样亮但曝光长了 10 倍 → 判定环境暗了 10 倍。"""
        m1 = LightMeter()
        m1.measure(srgb_frame(0.2), exposure_s=1 / 30)
        a = m1.measure(srgb_frame(0.2), exposure_s=1 / 30).lux
        m2 = LightMeter()
        m2.measure(srgb_frame(0.2), exposure_s=1 / 3)
        b = m2.measure(srgb_frame(0.2), exposure_s=1 / 3).lux
        assert b < a / 5, (a, b)

    def test_reports_stabilized_after_window(self):
        m = LightMeter()
        for _ in range(10):
            est = m.measure(srgb_frame(0.05))
        assert est.stabilized and est.samples >= 5

    def test_feedback_detection(self):
        """画面亮度的增量跟着屏幕亮度的增量走 → 应报"疑似回流"。"""
        m = LightMeter()
        levels = [20, 35, 42, 58, 66, 80, 88, 95]
        ratios = [0.020, 0.028, 0.031, 0.040, 0.044, 0.053, 0.058, 0.062]
        last = None
        for lv, r in zip(levels, ratios):
            # 第一拍屏幕还是初始亮度 20，所以从第二拍开始才有"变化"可看
            m.observe_level(lv)
            last = m.measure(srgb_frame(r))
        assert last is not None and last.feedback_suspect, "应检测到屏幕回流"

    def test_normal_monotonic_ramp_is_not_feedback(self):
        """回归测试：环境自己单调变亮、屏幕跟着调亮 —— 不该被误判成回流。

        这正是真机实测踩到的坑：旧版用绝对值做相关，把亮度锁死在 91%。
        """
        m = LightMeter()
        ratios = [0.020, 0.030, 0.050, 0.080, 0.120, 0.180, 0.250, 0.320]
        levels = [30, 45, 60, 72, 84, 92, 96, 98]
        last = None
        for r, lv in zip(ratios, levels):
            last = m.measure(srgb_frame(r))
            m.observe_level(lv)
        assert last is not None
        assert not last.feedback_suspect, "正常单调调光被误判成回流"

    def test_no_feedback_when_screen_static(self):
        m = LightMeter()
        for r in (0.02, 0.05, 0.09, 0.15, 0.2):
            m.observe_level(50)                 # 屏幕一直没动
            est = m.measure(srgb_frame(r))
        assert not est.feedback_suspect


class TestFeedbackDetectorFunction:
    """直接测判定函数本身（比构造端到端序列更能锁定行为）。"""

    def test_coupled_sequences_are_suspicious(self):
        ok, corr = feedback_suspect(
            [0.020, 0.028, 0.031, 0.040, 0.044, 0.053, 0.058, 0.062],
            [20, 35, 42, 58, 66, 80, 88, 95])
        assert ok and corr is not None and corr > 0.9

    def test_independent_sequences_are_not_suspicious(self):
        ok, corr = feedback_suspect(
            [0.020, 0.030, 0.050, 0.080, 0.120, 0.180, 0.250, 0.320],
            [30, 45, 60, 72, 84, 92, 96, 98])
        assert not ok and corr is not None and corr < 0

    def test_static_screen_is_not_suspicious(self):
        ok, corr = feedback_suspect([0.02, 0.05, 0.09, 0.15, 0.2],
                                    [50, 50, 50, 50, 50])
        assert not ok and corr is None

    def test_too_few_samples(self):
        ok, corr = feedback_suspect([0.02, 0.03], [20, 30])
        assert not ok and corr is None
