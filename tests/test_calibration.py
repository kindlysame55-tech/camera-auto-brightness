"""自动校准与校准向导的测试（纯逻辑部分不需要显示器）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab.calibration import (  # noqa: E402
    MIN_LEVEL_GAP, MIN_LUX_RATIO, Sample, build_anchors, explain, suggest_levels,
)
from camab.config import AppConfig  # noqa: E402
from camab.metrics import interval_from_speed  # noqa: E402


# --------------------------------------------------------------------------- #
# 由采样点反推曲线
# --------------------------------------------------------------------------- #
class TestBuildAnchors:
    def test_two_points_produce_working_curve(self):
        """两个点就能定出曲线，且用户选的点要落在折点上。"""
        res = build_anchors([Sample(20.0, 35, "夜里"), Sample(200.0, 80, "开灯")],
                            floor=20, ceil=100)
        assert res.ok, res.message
        from camab.metrics import MappingConfig

        m = MappingConfig(anchors=res.anchors, floor=20, ceil=100).clamped()
        assert m.lux_to_level(20) == 35, "用户选的暗点没被尊重"
        assert m.lux_to_level(200) == 80, "用户选的亮点没被尊重"
        # 单调
        levels = [m.lux_to_level(l) for l in (1, 5, 20, 60, 200, 600, 3000)]
        assert all(b >= a for a, b in zip(levels, levels[1:])), levels

    def test_three_points(self):
        res = build_anchors([Sample(10.0, 25), Sample(80.0, 55), Sample(400.0, 90)],
                            floor=15, ceil=100)
        assert res.ok
        from camab.metrics import MappingConfig

        m = MappingConfig(anchors=res.anchors, floor=15, ceil=100).clamped()
        assert m.lux_to_level(10) == 25
        assert m.lux_to_level(80) == 55
        assert m.lux_to_level(400) == 90

    def test_outside_range_holds_floor_and_ceil(self):
        res = build_anchors([Sample(20.0, 40), Sample(200.0, 80)], floor=20, ceil=100)
        from camab.metrics import MappingConfig

        m = MappingConfig(anchors=res.anchors, floor=20, ceil=100).clamped()
        assert m.lux_to_level(0.5) == 20      # 比最暗还暗 → 最低亮度
        assert m.lux_to_level(5000) == 100    # 比最亮还亮 → 最高亮度

    def test_needs_two_points(self):
        res = build_anchors([Sample(50.0, 60)])
        assert not res.ok and "2 个采样点" in res.message

    def test_rejects_too_similar_light(self):
        res = build_anchors([Sample(50.0, 40), Sample(60.0, 80)])
        assert not res.ok
        assert "太接近" in res.message
        assert f"{MIN_LUX_RATIO}" in res.message

    def test_rejects_flat_levels(self):
        res = build_anchors([Sample(20.0, 60), Sample(300.0, 62)])
        assert not res.ok
        assert f"{MIN_LEVEL_GAP}" in res.message

    def test_clamps_levels_into_range(self):
        res = build_anchors([Sample(20.0, 5), Sample(300.0, 200)], floor=25, ceil=95)
        assert res.ok
        assert res.anchors[1][1] == 25      # 5% 被抬到 floor
        assert res.anchors[2][1] == 95      # 200% 被压到 ceil

    def test_description_is_readable(self):
        res = build_anchors([Sample(20.0, 35), Sample(200.0, 80)])
        assert "lx→" in res.describe()

    def test_explain_lists_points_sorted(self):
        text = explain([Sample(200.0, 80, "开灯"), Sample(20.0, 35, "夜里")])
        assert text.index("夜里") < text.index("开灯")


# --------------------------------------------------------------------------- #
# 滑块建议值
# --------------------------------------------------------------------------- #
class TestSuggestLevels:
    def test_brighter_room_suggests_brighter(self):
        dark, _ = suggest_levels(5.0, 0, floor=20, ceil=100)
        mid, _ = suggest_levels(60.0, 0, floor=20, ceil=100)
        bright, _ = suggest_levels(800.0, 0, floor=20, ceil=100)
        assert dark < mid < bright

    def test_respects_floor_and_ceil(self):
        for lux in (0.1, 1, 100, 100000):
            lv, lo = suggest_levels(lux, 0, floor=35, ceil=70)
            assert 35 <= lv <= 70

    def test_blends_with_current_level(self):
        """建议值 = 70% 曲线值 + 30% 现状，不会一上来跳到一个陌生数字。"""
        pure, _ = suggest_levels(60.0, 0, floor=20, ceil=100)
        blended, _ = suggest_levels(60.0, 90, floor=20, ceil=100)
        assert pure < blended < 90
        assert abs(blended - (pure * 0.7 + 90 * 0.3)) <= 1

    def test_ignores_current_outside_range(self):
        lv, _ = suggest_levels(60.0, 5, floor=40, ceil=100)
        assert 40 <= lv <= 100


# --------------------------------------------------------------------------- #
# 向导界面（无桌面会话时由 tk_window fixture 自动跳过）
# --------------------------------------------------------------------------- #
@pytest.fixture
def _gui_parent(tk_window):
    """GUI 用例共用的父窗口 —— 每个用例只建一个 Toplevel。

    早先每个用例都 ``tk.Tk()`` + ``destroy()``，整套跑时 Tcl 会偶发
    "Can't find a usable init.tcl"（反复建销解释器导致）。
    另外**不要**用"先建个 Tk 探一探"的 _has_display()：那本身也会多建一次
    解释器，反而让会话级根窗口偶发创建失败（实测 3 次里有 1 次全被跳过）。
    """
    return tk_window

class TestWizard:
    @pytest.fixture(autouse=True)
    def _parent(self, _gui_parent):
        self._win_parent = _gui_parent
        yield

    @staticmethod
    def _wait(root, condition, timeout=3.0):
        import time

        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.005)
        root.update()
        assert condition(), "后台校准操作未及时完成"

    def _record(self, root, wiz):
        wiz.record()
        self._wait(root, lambda: not wiz._record_pending)

    def _make(self, samples_lux=(40.0, 300.0)):
        from camab.calib_dialog import CalibrationWizard

        root = self._win_parent
        lux = {"v": samples_lux[0]}
        applied: list[int] = []
        done: list[list] = []
        wiz = CalibrationWizard(
            root,
            read_lux=lambda: lux["v"],
            apply_level=lambda lv: applied.append(lv),
            get_level=lambda: 60,
            on_done=lambda anchors: done.append(anchors),
            floor=20, ceil=100,
        )
        return root, wiz, lux, applied, done

    def test_records_and_builds_curve(self):
        import time as _t

        root, wiz, lux, applied, done = self._make()
        try:
            wiz.var_level.set(35)
            self._record(root, wiz)
            lux["v"] = 300.0
            wiz.var_level.set(80)
            _t.sleep(0.05)          # 给后台取样线程一点时间（真实环境本来就有人手操作的间隔）
            self._record(root, wiz)
            assert len(wiz.samples) == 2
            wiz.finish()
            assert done and len(done[0]) >= 3
            from camab.metrics import MappingConfig

            m = MappingConfig(anchors=done[0], floor=20, ceil=100).clamped()
            assert m.lux_to_level(300) == 80
        finally:
            wiz._close()
            root.destroy()

    def test_refuses_duplicate_light(self):
        root, wiz, lux, applied, done = self._make()
        try:
            wiz.var_level.set(35)
            self._record(root, wiz)
            wiz.var_level.set(70)
            self._record(root, wiz)          # 同一个光照
            assert len(wiz.samples) == 1
            assert "太接近" in wiz.lbl_status.cget("text")
        finally:
            wiz._close()
            root.destroy()

    def test_refuses_dark_frame(self):
        root, wiz, lux, applied, done = self._make(samples_lux=(0.2, 300.0))
        try:
            self._record(root, wiz)
            assert not wiz.samples
            assert "接近 0" in wiz.lbl_status.cget("text")
        finally:
            wiz._close()
            root.destroy()

    def test_preview_sets_level(self):
        root, wiz, lux, applied, done = self._make()
        try:
            wiz.var_level.set(72)
            wiz.preview()
            self._wait(root, lambda: not wiz._preview_pending)
            assert applied == [72]
        finally:
            wiz._close()
            root.destroy()

    def test_done_button_disabled_until_two_points(self):
        root, wiz, lux, applied, done = self._make()
        try:
            assert wiz.btn_done.cget("state") == "disabled"
            wiz.var_level.set(30)
            self._record(root, wiz)
            assert wiz.btn_done.cget("state") == "disabled"
            lux["v"] = 400.0
            wiz._lux = None          # 让下一次取样必定重新读
            wiz.var_level.set(85)
            self._record(root, wiz)
            assert len(wiz.samples) == 2, wiz.lbl_status.cget("text")
            assert wiz.btn_done.cget("state") == "normal"
        finally:
            wiz._close()
            root.destroy()

    def test_slider_value_survives_refresh(self):
        """回归：用户拖动滑块后，0.5s 刷新不能把值覆盖回去。

        早先每次 _tick 都无条件 set 建议值，表现为"滑块没法滑动"（用户实测反馈）。
        """
        root, wiz, lux, applied, done = self._make()
        try:
            wiz._tick()                      # 第一次刷新会给初值
            wiz.var_level.set(88)            # 模拟用户拖动
            wiz._on_drag()
            for _ in range(3):
                wiz._tick()                  # 后续刷新不许再改
                root.update()
            assert int(float(wiz.var_level.get())) == 88
            assert wiz.lbl_level.cget("text") == "88%"
        finally:
            wiz._close()
            root.destroy()

    def test_slider_is_actually_draggable(self):
        """滑块要够厚、够宽、能改值（用户反馈过"太细抓不住"）。"""
        root, wiz, lux, applied, done = self._make()
        try:
            wiz.win.update_idletasks()
            wiz.scale.set(73)
            root.update()
            assert int(float(wiz.var_level.get())) == 73
            # 用请求尺寸断言：无桌面会话时 winfo_height() 恒为 1，测不出布局
            assert wiz.scale.winfo_reqheight() >= 20, \
                f"滑块太薄抓不住：{wiz.scale.winfo_reqheight()}px"
            assert wiz.scale.winfo_reqwidth() >= 200
            assert wiz.scale.cget("to") == 100
            assert wiz.scale.cget("from") == 20
            # 高度不足时可读的滑轨，也要能通过访问器读到值（ttk 的 get）
            assert int(float(wiz.scale.get())) == 73
        finally:
            wiz._close()
            root.destroy()

    def test_keyboard_nudge(self):
        root, wiz, lux, applied, done = self._make()
        try:
            wiz.var_level.set(50)
            wiz._nudge(1)
            wiz._nudge(1)
            assert int(float(wiz.var_level.get())) == 52
            wiz._nudge(-100)                         # 不能越过下限
            assert int(float(wiz.var_level.get())) == 20
            wiz._nudge(1000)                         # 不能越过上限
            assert int(float(wiz.var_level.get())) == 100
        finally:
            wiz._close()
            root.destroy()

    def test_slider_gets_thick_style(self):
        """滑块要够厚才好抓（用户反馈太细抓不住）。"""
        root, wiz, lux, applied, done = self._make()
        try:
            assert wiz.scale.cget("style") == "Calib.Horizontal.TScale"
            assert wiz.scale.cget("length") >= 200
        finally:
            wiz._close()
            root.destroy()

    def test_record_waits_for_a_sample_started_after_click_without_blocking(self):
        """延迟返回的旧光照不能被记录，取样期间 Tk 继续响应。"""
        import threading
        import time
        from camab.calib_dialog import CalibrationWizard

        root = self._win_parent
        first_started = threading.Event()
        release_first = threading.Event()
        lux = {"value": 40.0}
        calls = []

        def read():
            value = lux["value"]
            calls.append(value)
            if len(calls) == 1:
                first_started.set()
                release_first.wait(2.0)
            return value

        wiz = CalibrationWizard(root, read, lambda _lv: None, lambda: 60, lambda _a: None)
        try:
            assert first_started.wait(0.5)
            lux["value"] = 300.0
            wiz.var_level.set(35)
            start = time.monotonic()
            wiz.record()
            assert time.monotonic() - start < 0.1
            assert wiz.btn_record.cget("state") == "disabled"
            wiz.var_level.set(88)
            wiz.record()  # 重复点击不会再开取样线程或改变已提交的亮度。
            heartbeat = []
            root.after(0, lambda: heartbeat.append(True))
            root.update()
            assert heartbeat
            release_first.set()
            self._wait(root, lambda: not wiz._record_pending)
            assert calls[:2] == [40.0, 300.0]
            assert [(s.lux, s.level) for s in wiz.samples] == [(300.0, 35)]
        finally:
            release_first.set()
            wiz._close()
            root.destroy()

    def test_record_timeout_does_not_commit_late_result(self):
        import threading

        root, wiz, _lux, _applied, _done = self._make()
        release = threading.Event()
        try:
            self._wait(root, lambda: not wiz._sampling)
            wiz.read_lux = lambda: (release.wait(2.0), 100.0)[1]
            wiz._sample_timeout = 0.06
            self._record(root, wiz)
            assert "超时" in wiz.lbl_status.cget("text")
            assert not wiz.samples
            release.set()
            self._wait(root, lambda: not wiz._sampling)
            assert not wiz.samples
            assert wiz.btn_record.cget("state") == "normal"
        finally:
            release.set()
            wiz._close()
            root.destroy()

    @pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1.0])
    def test_record_rejects_nonfinite_or_negative_lux(self, invalid):
        root, wiz, _lux, _applied, _done = self._make(samples_lux=(invalid, 300.0))
        try:
            self._record(root, wiz)
            assert not wiz.samples
            assert "无效照度" in wiz.lbl_status.cget("text")
        finally:
            wiz._close()
            root.destroy()

    def test_preview_cancel_restores_brightness_after_pending_write(self):
        """慢硬件写不会卡窗口；取消后先完成写入再恢复，不留下预览亮度。"""
        import threading
        import time
        from camab.calib_dialog import CalibrationWizard

        root = self._win_parent
        ui_thread = threading.get_ident()
        started = threading.Event()
        release = threading.Event()
        applied = []

        def get_level():
            assert threading.get_ident() != ui_thread
            return 60

        def apply(level):
            assert threading.get_ident() != ui_thread
            applied.append(level)
            if level == 72:
                started.set()
                release.wait(2.0)

        wiz = CalibrationWizard(root, lambda: 40.0, apply, get_level, lambda _a: None)
        try:
            wiz.var_level.set(72)
            start = time.monotonic()
            wiz.preview()
            assert time.monotonic() - start < 0.1
            assert started.wait(0.5)
            heartbeat = []
            root.after(0, lambda: heartbeat.append(True))
            root.update()
            assert heartbeat
            start = time.monotonic()
            wiz.cancel()
            assert time.monotonic() - start < 0.1
            assert wiz._closed
            assert wiz.win.state() == "withdrawn"
            release.set()
            self._wait(root, lambda: not wiz.win.winfo_exists())
            assert applied == [72, 60]
            assert not wiz._after_ids
        finally:
            release.set()
            wiz._close()
            root.destroy()

    def test_close_cancels_scheduled_callbacks(self):
        root, wiz, _lux, _applied, _done = self._make()
        wiz._tick()
        wiz._tick()
        assert len(wiz._after_ids) == 1
        wiz._close()
        assert not wiz._after_ids
        root.update()
        root.destroy()

    def test_parent_destroy_cancels_callbacks_and_discards_pending_record(self):
        import threading

        root, wiz, _lux, _applied, _done = self._make()
        release = threading.Event()
        try:
            self._wait(root, lambda: not wiz._sampling)
            wiz.read_lux = lambda: (release.wait(2.0), 100.0)[1]
            wiz.record()
            assert wiz._record_pending
            assert wiz._after_ids
            root.destroy()
            assert wiz._closed
            assert not wiz._record_pending
            assert not wiz._after_ids
            release.set()
            assert not wiz.samples
        finally:
            release.set()
            wiz._close()

    def test_tk_variable_is_released_on_ui_thread_before_sampling_thread(self, monkeypatch):
        """关窗后的摄像头回调仍持有向导，也不能在其尾部析构 Tk 变量。"""
        import threading
        import tkinter as tk
        import weakref
        from camab.calib_dialog import CalibrationWizard

        root = self._win_parent
        ui_thread = threading.get_ident()
        started = threading.Event()
        release = threading.Event()
        finalized = []
        original_del = tk.Variable.__del__

        def trace_del(variable):
            finalized.append((variable._name, threading.get_ident()))
            original_del(variable)

        def read():
            started.set()
            release.wait(2.0)
            return 40.0

        monkeypatch.setattr(tk.Variable, "__del__", trace_del)
        wiz = CalibrationWizard(root, read, lambda _level: None, lambda: 60, lambda _points: None)
        try:
            assert started.wait(0.5)
            variable_name = wiz.var_level._name
            reference = weakref.ref(wiz)
            wiz._close()
            assert wiz.var_level is None
            del wiz
            assert reference() is not None  # 后台取样仍持有向导。
            assert (variable_name, ui_thread) in finalized
            release.set()
            self._wait(root, lambda: reference() is None)
            assert all(owner == ui_thread for name, owner in finalized if name == variable_name)
        finally:
            release.set()
            root.destroy()

    def test_remove_selected_removes_displayed_sample_in_sorted_order(self):
        root, wiz, _lux, _applied, _done = self._make()
        try:
            bright = Sample(300.0, 80)
            dark = Sample(40.0, 35)
            wiz.samples = [bright, dark]
            wiz._refresh()
            wiz.lst.selection_set(0)
            wiz.remove_selected()
            assert wiz.samples == [bright]
            assert wiz.btn_done.cget("state") == "disabled"
        finally:
            wiz._close()
            root.destroy()

    def test_user_choice_before_initial_sample_survives(self):
        import threading
        from camab.calib_dialog import CalibrationWizard

        root = self._win_parent
        release = threading.Event()
        wiz = CalibrationWizard(root, lambda: (release.wait(2.0), 40.0)[1],
                                lambda _lv: None, lambda: 60, lambda _a: None)
        try:
            wiz.scale.set(82)
            wiz._on_drag("82")
            release.set()
            self._wait(root, lambda: not wiz._sampling)
            wiz._tick()
            assert int(float(wiz.var_level.get())) == 82
            assert wiz.lbl_level.cget("text") == "82%"
        finally:
            release.set()
            wiz._close()
            root.destroy()


# --------------------------------------------------------------------------- #
# 向导：暂停状态下也要拿到新读数（用户实测"改了光照记不了下一个点"）
# --------------------------------------------------------------------------- #
class TestFreshSampling:
    def test_fresh_lux_refetches_when_stale(self):
        """缓存过期后 _fresh_lux 必须重新取样，不能一直用冻结的旧值。"""
        import time as _t
        from camab.calib_dialog import CalibrationWizard

        class FakeWiz(CalibrationWizard):
            def __init__(self) -> None:      # 跳过真界面
                self._closed = False
                self._sampling = False
                self._lux = None
                self._lux_t = 0.0
                self._sample_timeout = 1.0
                self.calls = 0

            def _kick_sample(self) -> None:  # 同步版，方便断言
                self.calls += 1
                self._lux = 100.0 * self.calls
                self._lux_t = _t.time()

        w = FakeWiz()
        first = w._fresh_lux()
        assert first == 100.0 and w.calls == 1
        # 缓存很新 + 直接读 current_lux → 不重复取样
        assert w.current_lux() == 100.0 and w.calls == 1
        # _fresh_lux 每次都强制重新测（记录点必须用"此刻"的值）
        assert w._fresh_lux() == 200.0 and w.calls == 2

    def test_engine_sample_once_works_while_paused(self):
        """引擎暂停时 sample_once 仍要真的测光（向导依赖这个）。"""
        import numpy as np

        from camab.engine import Engine

        cam = FakeEngineCamera(ratio=0.30)
        be = CountingEngineBackend()
        eng = Engine(_engine_cfg(), backend=be)
        eng._camera = cam
        eng._monitor = be.monitors()[0]
        eng._t0 = __import__("time").monotonic()
        eng.pause("测试暂停")

        est = eng.sample_once(timeout=1.0)
        assert est is not None and est.lux > 100, f"暂停时没测到光：{est}"
        assert be.writes == [], "sample_once 不许写亮度"
        before = eng.status.lux
        be2 = CountingEngineBackend()
        eng._monitor = be2.monitors()[0]
        cam.ratio = 0.005                    # 环境变暗
        est2 = eng.sample_once(timeout=1.0)
        assert est2 is not None and est2.raw_lux < est.raw_lux, "换了光照读数没变"
        assert before != est2.raw_lux


def _engine_cfg():
    from camab.config import AppConfig

    cfg = AppConfig()
    cfg.engine.dry_run = False
    cfg.mapping = cfg.mapping.clamped()
    return cfg


class FakeEngineCamera:
    """带比例的假摄像头（给引擎测试用）。"""

    def __init__(self, ratio: float = 0.02) -> None:
        from camab.camera import CameraStats

        self.ratio = ratio
        self.stats = CameraStats(backend="FAKE", opened=True)
        self.stats.ok = 1

    def read(self, timeout: float = 1.0):
        import time

        import numpy as np

        from camab.camera import Frame

        lin = max(0.0, min(1.0, self.ratio))
        srgb = np.where(lin <= 0.0031308, lin * 12.92,
                        1.055 * lin ** (1 / 2.4) - 0.055)
        img = np.full((48, 64, 3), int(round(float(srgb) * 255)), dtype=np.uint8)
        return Frame(img, time.time(), None, None, 1)

    def open(self, timeout: float = 5.0) -> bool:
        return True

    def close(self) -> None:
        pass


class CountingEngineBackend:
    from camab.brightness import BrightnessBackend as _BB

    class _Impl(_BB):
        name = "fake"
        functional = True

        def __init__(self) -> None:
            self.level = 60
            self.writes: list[int] = []

        def available(self) -> bool:
            return True

        def monitors(self):
            from camab.brightness import MonitorInfo

            return [MonitorInfo(self.name, 0, "fake:0", "假屏幕", self.level)]

        def get(self, key: str) -> int:
            return self.level

        def set(self, key: str, level: int) -> int:
            self.level = int(level)
            self.writes.append(self.level)
            return self.level

    def __new__(cls):  # noqa: D102
        return CountingEngineBackend._Impl()


# --------------------------------------------------------------------------- #
# 主窗口亮度滑块（用户实测"滑不了"）
# --------------------------------------------------------------------------- #
class TestMainWindowSlider:
    @pytest.fixture(autouse=True)
    def _parent(self, _gui_parent):
        self._win_parent = _gui_parent
        yield

    def _app(self):
        from camab.tray import TrayApp

        cfg = AppConfig()
        cfg.engine.dry_run = False
        cfg.engine.backend = "dryrun"
        app = TrayApp(cfg)
        # 不启动引擎，只建界面
        app.engine._backend = CountingEngineBackend()
        app.engine._monitor = app.engine._backend.monitors()[0]
        app.engine._status.current = 60
        root = self._win_parent
        app._win = root
        app._build_ui()
        return root, app

    def test_guard_does_not_stick(self):
        """回归：渲染状态后 _scale_guard 必须回到 False。

        早先它被设成 True 后只在 `not instate(...)` 分支里复位，
        而 instate 判定不可靠 → 标志永远 True → 滑块拖不动（用户实测反馈）。
        """
        root, app = self._app()
        try:
            app._render(app.engine.status)
            assert app._scale_guard is False, "_scale_guard 卡在 True 会把滑块锁死"
            app._on_scale_drag("70")
            assert app.lbl_level.cget("text") == "70%"
        finally:
            root.destroy()

    def test_drag_then_release_applies_level(self):
        root, app = self._app()
        try:
            app.var_level.set(73)
            app._on_scale_press(None)
            app._on_scale_drag("73")
            assert app.lbl_level.cget("text") == "73%"
            app._on_scale_release(None)
            assert app.engine._backend.level == 73, "松手后应该真的写亮度"
            assert app._scale_dragging is False
        finally:
            root.destroy()

    def test_render_does_not_fight_drag(self):
        """拖动过程中，状态刷新不能把滑块位置抢回去。"""
        root, app = self._app()
        try:
            app.var_level.set(80)
            app._on_scale_press(None)
            app.engine._status.current = 30     # 引擎那边的值
            app._render(app.engine.status)
            assert int(float(app.var_level.get())) == 80, "拖动时被状态刷新覆盖了"
        finally:
            root.destroy()

    def test_keyboard_nudge_applies(self):
        root, app = self._app()
        try:
            app.var_level.set(50)
            app._nudge_level(1)
            assert app.engine._backend.level == 51
            assert app.lbl_level.cget("text") == "51%"
            app._nudge_level(-100)
            assert app.engine._backend.level == 0
        finally:
            root.destroy()

    def test_slider_has_thickness(self):
        root, app = self._app()
        try:
            app._win.update_idletasks()
            assert app.scale.winfo_reqheight() >= 20, \
                f"滑块太薄：{app.scale.winfo_reqheight()}px"
        finally:
            root.destroy()


# --------------------------------------------------------------------------- #
# 自定义速度 / 采样速度 + 自动校准并入设置面板
# --------------------------------------------------------------------------- #
class TestCustomSpeeds:
    @pytest.fixture(autouse=True)
    def _parent(self, _gui_parent):
        self._win_parent = _gui_parent
        yield

    def _app(self):
        from camab.tray import TrayApp

        cfg = AppConfig()
        cfg.engine.dry_run = False
        cfg.engine.backend = "dryrun"
        app = TrayApp(cfg)
        app.engine._backend = CountingEngineBackend()
        app.engine._monitor = app.engine._backend.monitors()[0]
        root = self._win_parent
        app._win = root
        app._build_ui()
        return root, app

    # -- 响应速度自定义 --
    def test_custom_ramp_accepts_exact_value(self):
        root, app = self._app()
        try:
            app.var_speed_value.set("7.5")
            app._apply_custom_speed()
            assert app.cfg.mapping.ramp_per_s == pytest.approx(7.5), "自定义速度没生效"
            assert "7.5" in app.lbl_speed.cget("text")
        finally:
            root.destroy()

    def test_custom_ramp_clamps_and_rejects(self):
        root, app = self._app()
        try:
            app.var_speed_value.set("9999")
            app._apply_custom_speed()
            assert app.cfg.mapping.ramp_per_s == pytest.approx(40.0), "超出上限要钳住"
            app.var_speed_value.set("0.01")
            app._apply_custom_speed()
            assert app.cfg.mapping.ramp_per_s == pytest.approx(1.5), "低于下限要钳住"
            app.var_speed_value.set("不是数字")
            app._apply_custom_speed()
            assert app.cfg.mapping.ramp_per_s == pytest.approx(1.5), "非法输入不该改配置"
        finally:
            root.destroy()

    def test_custom_ramp_survives_reload(self, tmp_path):
        """自定义速度要能存进配置文件，重启后还在。"""
        from camab.config import AppConfig as AC

        root, app = self._app()
        try:
            cfg_path = tmp_path / "c.json"
            app.var_speed_value.set("22.5")
            app._apply_custom_speed()
            app.cfg.save(cfg_path)
            again = AC.load(cfg_path)
            assert again.mapping.ramp_per_s == pytest.approx(22.5)
        finally:
            root.destroy()

    def test_tests_never_write_user_config(self):
        """回归：测试不许写用户的真实配置文件。

        实测踩到过 —— GUI 用例的 ``cfg.save()`` 默认写到
        ``%LOCALAPPDATA%\\camera-auto-brightness\\config.json``，
        把用户配置改成了「干跑 + 模拟后端」，重启后自动亮度直接失灵。
        conftest 的 autouse fixture 把 CAMAB_CONFIG 指到临时目录来防这个。
        """
        from camab.config import AppConfig as AC, default_config_path

        real = Path(os.environ["LOCALAPPDATA"]) / "camera-auto-brightness" / "config.json"
        injected = default_config_path()
        assert injected.resolve() != real.resolve(), \
            f"测试的配置路径指向了真实文件：{injected}"
        assert "camab-config" in str(injected), injected
        root, app = self._app()
        try:
            app.open_settings()          # 控件在设置面板里
            app.var_interval.set("3.3")
            app._apply_custom_interval()
            assert real.exists() is False or AC.load(real).engine.interval_s != 3.3, \
                "测试把采样周期写进了用户真实配置"
        finally:
            root.destroy()

    # -- 采样速度自定义（在设置面板的自动校准组里）--
    def test_custom_interval_accepts_exact_value(self):
        root, app = self._app()
        try:
            s = app.open_settings()
            assert s is not None
            app.var_interval.set("1.8")
            app._apply_custom_interval()
            assert app.cfg.engine.interval_s == pytest.approx(1.8)
            assert app.engine.cfg.engine.interval_s == pytest.approx(1.8), "没灌进引擎"
        finally:
            root.destroy()

    def test_custom_interval_clamps(self):
        root, app = self._app()
        try:
            app.open_settings()          # 采样速度控件在设置面板里，先打开
            app.var_interval.set("0.01")
            app._apply_custom_interval()
            assert app.cfg.engine.interval_s == pytest.approx(0.5)
            app.var_interval.set("500")
            app._apply_custom_interval()
            assert app.cfg.engine.interval_s == pytest.approx(10.0)
        finally:
            root.destroy()

    def test_dragging_sample_slider_actually_applies(self):
        """回归（用户实测"采样速度设置不生效"）：**拖完滑块松手必须真的改配置**。

        早先只绑了 ttk 的 ``command``（拖动中触发），而它只更新标签文字、
        不写配置也不灌引擎 —— 拖完什么都没发生。这里模拟"拖 + 松手"。
        """
        root, app = self._app()
        try:
            s = app.open_settings()
            assert s is not None
            before = app.cfg.engine.interval_s
            app.var_sample_speed.set(100)          # 拖到最右 = 最快
            app._on_sample_speed_drag()            # 拖动中的回调
            app._apply_sample_speed()              # 松手
            assert app.cfg.engine.interval_s == pytest.approx(0.5), "滑块松手没生效"
            assert app.engine.cfg.engine.interval_s == pytest.approx(0.5), "没灌进引擎"
            assert app.cfg.engine.interval_s != before
            assert "0.5" in app.lbl_sample.cget("text")
            # 再拖到最左 = 最慢
            app.var_sample_speed.set(0)
            app._on_sample_speed_drag()
            app._apply_sample_speed()
            assert app.cfg.engine.interval_s == pytest.approx(10.0)
        finally:
            root.destroy()

    def test_interval_state_label_shows_live_value(self):
        """界面上要能一眼看出现在多久测一次、以及引擎侧是不是同一个值。"""
        root, app = self._app()
        try:
            app.open_settings()
            app.var_sample_speed.set(70)
            app._on_sample_speed_drag()
            app._apply_sample_speed()
            txt = app.lbl_interval_state.cget("text")
            assert "engine" in txt or "引擎" in txt, txt
            live = f"{app.engine.cfg.engine.interval_s:g}"
            assert live in txt, f"状态行没反映引擎实际周期：{txt}"
        finally:
            root.destroy()

    def test_settings_save_persists_slider_interval(self, monkeypatch):
        """回归：拖完滑块**直接点保存**也要把采样速度存进去。

        早先 save() 只写勾选框，不认采样速度控件 —— 滑块改动会被丢掉。
        """
        import tkinter as tk

        from camab import tray as tray_mod

        class _NoDialog:
            @staticmethod
            def showinfo(*_a, **_k):
                return "ok"

            @staticmethod
            def showwarning(*_a, **_k):
                return "ok"

            @staticmethod
            def showerror(*_a, **_k):
                return "ok"

        monkeypatch.setattr(tray_mod, "messagebox", _NoDialog)

        root, app = self._app()
        try:
            s = app.open_settings()
            app.var_sample_speed.set(85)
            app._on_sample_speed_drag()

            def find_buttons(widget, out):
                for ch in widget.winfo_children():
                    if isinstance(ch, tk.Button):
                        out.append(ch)
                    find_buttons(ch, out)
                return out

            save_btn = next(b for b in find_buttons(s, []) if b.cget("text") == "保存")
            save_btn.invoke()
            expected = interval_from_speed(85)
            assert app.cfg.engine.interval_s == pytest.approx(expected), "保存丢了滑块改动"
            assert app.var_interval.get().startswith(f"{expected:g}"[:3])
        finally:
            root.destroy()

    def test_arrow_keys_nudge_interval(self):
        root, app = self._app()
        try:
            app.open_settings()
            app.var_sample_speed.set(50)
            app._on_sample_speed_drag()
            app._apply_sample_speed()
            first = app.cfg.engine.interval_s
            app._nudge_interval(1)                  # 右 = 更快
            assert app.cfg.engine.interval_s < first
            app._nudge_interval(-1)
            assert app.cfg.engine.interval_s == pytest.approx(first, abs=0.05)
        finally:
            root.destroy()

    def test_sample_speed_slider_maps_both_ways(self):
        from camab.metrics import INTERVAL_FAST, INTERVAL_SLOW, interval_from_speed, speed_from_interval

        for speed in (0, 30, 60, 100):
            iv = interval_from_speed(speed)
            assert INTERVAL_FAST <= iv <= INTERVAL_SLOW
            assert abs(speed_from_interval(iv) - speed) <= 4, f"往返不一致：{speed}→{iv}"
        # 滑块越靠右越快 → 周期越短
        assert interval_from_speed(100) == pytest.approx(INTERVAL_FAST), "最右应该是采样最快"
        assert interval_from_speed(0) == pytest.approx(INTERVAL_SLOW), "最左应该是采样最慢"

    def test_calibration_group_lives_in_settings(self):
        """自动校准（含采样速度）必须在设置面板里，而不是主窗口单独一个按钮。"""
        root, app = self._app()
        try:
            s = app.open_settings()
            assert s is not None
            # 设置面板里应该有这些控件
            assert app.scale_sample.winfo_exists()
            assert app.ent_interval.winfo_exists()
            assert "s" in app.lbl_sample.cget("text")
            assert "当前曲线" in app.lbl_curve.cget("text")
        finally:
            root.destroy()

    def test_main_window_has_no_calibration_button(self):
        """主窗口不该再有独立的「自动校准」按钮（已并入设置）。"""
        import tkinter as tk

        root, app = self._app()
        try:
            labels: list[str] = []

            def walk(widget):
                for ch in widget.winfo_children():
                    try:
                        if isinstance(ch, tk.Button):
                            labels.append(ch.cget("text"))
                    except Exception:  # noqa: BLE001
                        pass
                    walk(ch)

            walk(app._win)
            assert "自动校准" not in labels, f"主窗口还有独立校准按钮：{labels}"
            assert any("设置" in t for t in labels), labels
        finally:
            root.destroy()

    def test_settings_save_keeps_sampling_speed(self, monkeypatch):
        """点「保存」时采样速度不该被旧值覆盖回默认。

        注意：save() 成功会弹 messagebox（模态、等人点确定）。
        自动化里必须把它替换掉，否则测试会永远卡在弹窗上（实测踩到）。
        """
        import tkinter as tk

        from camab import tray as tray_mod

        class _NoDialog:
            @staticmethod
            def showinfo(*_a, **_k):
                return "ok"

            @staticmethod
            def showwarning(*_a, **_k):
                return "ok"

            @staticmethod
            def showerror(*_a, **_k):
                return "ok"

        monkeypatch.setattr(tray_mod, "messagebox", _NoDialog)

        root, app = self._app()
        try:
            s = app.open_settings()
            app.var_interval.set("2.2")
            app._apply_custom_interval()

            def find_buttons(widget, out):
                for ch in widget.winfo_children():
                    if isinstance(ch, tk.Button):
                        out.append(ch)
                    find_buttons(ch, out)
                return out

            btns = find_buttons(s, [])
            save_btn = next(b for b in btns if b.cget("text") == "保存")
            save_btn.invoke()
            assert app.cfg.engine.interval_s == pytest.approx(2.2)
        finally:
            root.destroy()
# --------------------------------------------------------------------------- #
# 配置联动
# --------------------------------------------------------------------------- #
class TestSaturationWarning:
    """回归：用户真机记的两个点把曲线做"陡"了（11 lx→76%、64 lx→100%），
    结果任何正常房间都被顶到 100%。向导必须当场提醒。"""

    def _wiz(self, samples):
        from camab.calib_dialog import CalibrationWizard

        class _W:
            floor = 20
            ceil = 100

            def __init__(self, s):
                self.samples = s

            _preview_anchors = CalibrationWizard._preview_anchors
            saturation_warning = CalibrationWizard.saturation_warning

        return _W(samples)

    def test_warns_on_steep_curve(self):
        w = self._wiz([Sample(lux=11.12, level=76), Sample(lux=64.02, level=100)])
        msg = w.saturation_warning()
        assert msg, "这么陡的曲线竟然没预警"
        assert "lx" in msg and "100%" in msg

    def test_quiet_on_reasonable_curve(self):
        # 关灯 8 lx→25%、白天靠窗 400 lx→100%：跨度合理，不该预警
        w = self._wiz([Sample(lux=8.0, level=25), Sample(lux=400.0, level=100)])
        assert w.saturation_warning() == "", "合理曲线被误报"

    def test_quiet_on_factory_curve(self):
        from camab.metrics import default_anchors
        import camab.metrics as M

        class _W:
            floor = 20
            ceil = 100
            samples = []

        w = self._wiz([])
        # 直接喂出厂折点
        assert w.saturation_warning(default_anchors(20, 100)) == "", "出厂曲线被误报"
        assert M.MappingConfig(anchors=default_anchors(20, 100)).lux_to_level(170) < 90


class TestConfigWiring:
    def test_calibrated_anchors_roundtrip(self, tmp_path: Path):
        res = build_anchors([Sample(25.0, 40), Sample(250.0, 85)])
        cfg = AppConfig()
        cfg.mapping.anchors = res.anchors
        cfg.clamped()
        p = tmp_path / "c.json"
        cfg.save(p)
        back = AppConfig.load(p)
        assert back.mapping.curve_points() == cfg.mapping.curve_points()
        assert back.mapping.lux_to_level(250) == 85
