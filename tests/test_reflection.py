"""屏幕反光扣除、静默启动、预设 的测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab.config import AppConfig  # noqa: E402
from camab.reflection import MAX_K, ReflectionCompensation  # noqa: E402
from camab.winutil import CREATE_NO_WINDOW, no_window_kwargs  # noqa: E402


# --------------------------------------------------------------------------- #
# 反光系数估计
# --------------------------------------------------------------------------- #
class TestEstimate:
    def test_recovers_known_coefficient(self):
        """L = A + k·b：给定 A=0.02、k=0.001，两档亮度应能解出 k。"""
        a, k = 0.02, 0.001
        pairs = [(25, a + k * 25), (90, a + k * 90), (60, a + k * 60)]
        comp = ReflectionCompensation.estimate(pairs)
        assert comp.calibrated
        assert comp.k == pytest.approx(k, rel=0.02)

    def test_rejects_small_level_gap(self):
        comp = ReflectionCompensation.estimate([(50, 0.03), (55, 0.0305)])
        assert not comp.calibrated
        assert "分辨不出" in comp.note

    def test_rejects_negative_slope(self):
        comp = ReflectionCompensation.estimate([(20, 0.05), (90, 0.02)])
        assert not comp.calibrated
        assert "负" in comp.note

    def test_rejects_flat_response(self):
        comp = ReflectionCompensation.estimate([(20, 0.04), (90, 0.04)])
        assert not comp.calibrated
        assert "测不到" in comp.note or "没有变化" in comp.note

    def test_clamps_absurd_coefficient(self):
        comp = ReflectionCompensation.estimate([(0, 0.0), (100, 0.9)])
        assert comp.calibrated
        assert comp.k == pytest.approx(MAX_K)

    def test_too_few_points(self):
        comp = ReflectionCompensation.estimate([(30, 0.1)])
        assert not comp.calibrated and "不足" in comp.note


# --------------------------------------------------------------------------- #
# 反推环境光
# --------------------------------------------------------------------------- #
class TestAmbientRatio:
    def test_identity_when_not_calibrated(self):
        comp = ReflectionCompensation()
        ratio, reliable = comp.ambient_ratio(0.08, level=70)
        assert ratio == 0.08 and reliable is False

    def test_subtracts_screen_contribution(self):
        comp = ReflectionCompensation(k=0.001, calibrated=True)
        # 画面 0.10，屏幕 80% → 屏幕贡献 0.08，环境只剩 0.02
        ratio, reliable = comp.ambient_ratio(0.10, level=80)
        assert ratio == pytest.approx(0.02, abs=1e-9)
        assert reliable is True

    def test_screen_dominant_is_flagged_unreliable(self):
        comp = ReflectionCompensation(k=0.001, calibrated=True)
        ratio, reliable = comp.ambient_ratio(0.081, level=80)
        assert ratio == pytest.approx(0.001, abs=1e-9)
        assert reliable is False, "环境只占 1%，反推不可信"

    def test_negative_never_returned(self):
        comp = ReflectionCompensation(k=0.002, calibrated=True)
        ratio, reliable = comp.ambient_ratio(0.05, level=90)
        assert ratio >= 0 and reliable is False

    def test_brighter_screen_lowers_ambient_estimate(self):
        """同一画面亮度下，屏幕越亮 → 扣掉的越多 → 环境光估计越低。"""
        comp = ReflectionCompensation(k=0.001, calibrated=True)
        low, _ = comp.ambient_ratio(0.12, level=30)
        high, _ = comp.ambient_ratio(0.12, level=90)
        assert high < low

    def test_compensation_makes_curve_more_stable(self):
        """扣除后：屏幕亮度变化对"测出的环境光"影响应显著变小。"""
        from camab.metrics import MappingConfig

        comp = ReflectionCompensation(k=0.001, calibrated=True)
        m = MappingConfig().clamped()
        ambient = 0.02

        def measured(level: int) -> float:
            return ambient + 0.001 * level

        raw_levels = [m.lux_to_level(measured(b) / 0.18 * 200) for b in (20, 99)]
        comp_levels = [m.lux_to_level(comp.ambient_ratio(measured(b), b)[0] / 0.18 * 200)
                       for b in (20, 99)]
        assert abs(comp_levels[0] - comp_levels[1]) <= abs(raw_levels[0] - raw_levels[1])


# --------------------------------------------------------------------------- #
# 配置集成
# --------------------------------------------------------------------------- #
class TestConfigIntegration:
    def test_defaults_off(self):
        cfg = AppConfig()
        assert cfg.screen_reflection_k == 0.0
        assert cfg.screen_calibrated is False
        assert cfg.reflection() is None

    def test_reflection_built_when_calibrated(self):
        cfg = AppConfig()
        cfg.screen_reflection_k = 0.0012
        cfg.screen_calibrated = True
        comp = cfg.reflection()
        assert comp is not None and comp.calibrated
        assert comp.k == pytest.approx(0.0012)

    def test_clamped_disables_without_k(self):
        cfg = AppConfig()
        cfg.screen_reflection_k = 0.0
        cfg.screen_calibrated = True
        cfg.clamped()
        assert cfg.screen_calibrated is False

    def test_roundtrip(self, tmp_path: Path):
        cfg = AppConfig()
        cfg.screen_reflection_k = 0.0009
        cfg.screen_calibrated = True
        p = tmp_path / "c.json"
        cfg.save(p)
        back = AppConfig.load(p)
        assert back.screen_calibrated is True
        assert back.screen_reflection_k == pytest.approx(0.0009)

    def test_default_percentile_is_low(self):
        """默认分位要低：真机实测人脸会把 50 分位带偏一个数量级。"""
        cfg = AppConfig()
        assert cfg.analysis.percentile <= 20
        assert cfg.effective_percentile <= 20


# --------------------------------------------------------------------------- #
# 静默启动
# --------------------------------------------------------------------------- #
class TestNoWindow:
    def test_windows_flags(self):
        if sys.platform != "win32":
            pytest.skip("仅 Windows")
        assert CREATE_NO_WINDOW == 0x08000000
        kw = no_window_kwargs()
        assert kw["creationflags"] & CREATE_NO_WINDOW
        assert kw["startupinfo"].wShowWindow == 0

    def test_engine_uses_hidden_subprocess(self):
        """源码层面守住：不能有人把 run_hidden 改回裸 subprocess（会闪黑框）。"""
        root = Path(__file__).resolve().parent.parent / "src" / "camab"
        for name in ("brightness.py", "camera.py"):
            text = (root / name).read_text(encoding="utf-8")
            assert "subprocess.run(" not in text, f"{name} 里出现了裸 subprocess.run"
            assert "run_hidden" in text, f"{name} 没有用 run_hidden"


# --------------------------------------------------------------------------- #
# 亮度后端：失败要"记性"，别每次白试
# --------------------------------------------------------------------------- #
class TestWmiStickyFailure:
    def test_com_write_failure_is_remembered(self):
        """原生写入失败后不反复调用，CIM 回退仍须每次写入并回读。"""
        from camab.brightness import BrightnessError, WmiBackend

        be = WmiBackend()
        calls = {"com": 0, "ps": 0, "get": 0, "level": 50}

        class BadMethod:
            def Methods_(self, *a):     # noqa: N802 - 模拟 COM 参数查询失败
                raise BrightnessError("无效的参数")

        class BadWmi:
            def ExecQuery(self, query):         # noqa: N802 - 模拟 COM 接口
                return [BadMethod()]

        real_com_set = WmiBackend._com_set

        def counting_com_set(self, level):
            calls["com"] += 1
            # 查询获得会抛异常的实例，验证故障路径只尝试一次。
            self._com_objs = lambda: BadWmi()
            return real_com_set(self, level)

        # 用真实实现包一层，这样才能验证 _com_write_disabled 的"记性"逻辑真的生效
        def guarded_com_set(self, level):
            if self._com_write_disabled:
                raise BrightnessError("pywin32 写亮度已确认不可用，改用 PowerShell")
            return counting_com_set(self, level)

        def fake_ps(script):
            from camab.brightness import _WMI_PS_GET
            import re
            if script == _WMI_PS_GET:
                calls["get"] += 1
                return str(calls["level"])
            calls["ps"] += 1
            calls["level"] = int(re.search(r"Brightness=(\d+)", script).group(1))
            return ""

        be._com_set = guarded_com_set.__get__(be)    # type: ignore[assignment]
        be._ps = fake_ps                             # type: ignore[assignment]

        assert be.set("internal:0", 70) == 70
        assert be.set("internal:0", 75) == 75
        assert be.set("internal:0", 80) == 80
        # 关键：失败一次之后 COM 路径要么只试一次、要么完全不再试；PowerShell 每次都要跑
        assert calls["com"] <= 1, f"失败的 COM 写不该反复试：{calls}"
        assert calls["ps"] == 3, "每次写入都必须真的落到 PowerShell"
        assert calls["get"] == 3, "回退写入也必须返回实际亮度"
        assert be._com_write_disabled is True

    def test_powershell_script_has_no_window_style(self):
        """实测：给 powershell 同时加 -WindowStyle Hidden 与 CREATE_NO_WINDOW 会卡死。"""
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "src" / "camab"
                / "brightness.py").read_text(encoding="utf-8")
        assert '"-WindowStyle", "Hidden"' not in text


# --------------------------------------------------------------------------- #
# 预设
# --------------------------------------------------------------------------- #
class TestPreset:
    def test_apply_known_preset(self):
        cfg = AppConfig()
        assert cfg.engine.apply_preset("sensitive")
        assert cfg.apply_mapping_preset("sensitive")
        assert cfg.engine.preset == "sensitive"
        assert cfg.engine.interval_s == pytest.approx(1.5)
        assert cfg.mapping.ramp_per_s == pytest.approx(30.0)
        assert cfg.mapping.deadband == 3

    def test_reject_unknown(self):
        cfg = AppConfig()
        assert cfg.engine.apply_preset("没有这个") is False
        assert cfg.apply_mapping_preset("没有这个") is False

    def test_preset_lowercased(self):
        cfg = AppConfig()
        assert cfg.engine.apply_preset("CALM") is True
        assert cfg.engine.preset == "calm"
        assert cfg.apply_mapping_preset("CALM") is True
        assert cfg.mapping.ramp_per_s == pytest.approx(4.0)
