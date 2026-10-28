"""硬件替身回归：线程、缓存帧、干跑及驱动调用次数。"""

from __future__ import annotations

import subprocess
import ctypes
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from camab import brightness, camera, engine
from camab.brightness import BrightnessError, WmiBackend
from camab.camera import Camera, _Grabber
from camab.config import AppConfig
from camab.lightmeter import LightMeter
from test_engine import CountingBackend, FakeCamera, make_engine
from test_metrics import srgb_frame


@pytest.mark.parametrize("artifact", ["empty", "hot_pixels", "bright_border"])
def test_black_input_artifacts_do_not_count_as_camera_signal(artifact, monkeypatch):
    from camab import devices
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    if artifact == "hot_pixels":
        img[40:44, 40:44] = 255
    elif artifact == "bright_border":
        img[:2] = 255
        img[-2:] = 255
    assert not camera.frame_has_signal(img)
    monkeypatch.setattr(devices, "full_report", lambda **_: pytest.fail("sampling must not enumerate devices"))
    cam = Camera(black_frame_limit=3)
    for _ in range(3):
        cam._check_black(img)
    assert not cam.stats.signal_ok and cam.stats.black_streak == 3
    cam._check_black(np.ones_like(img))
    assert cam.stats.signal_ok and cam.stats.black_streak == 0


def test_genuine_dim_frame_with_spatial_signal_is_valid():
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[30:40, 30:40] = 1
    assert img.mean() < .5
    assert camera.frame_has_signal(img)


def test_black_camera_reopens_and_recovers_without_writing_invalid_frames():
    class ReturningPhysicalCamera(FakeCamera):
        opens = 0
        closes = 0

        def open(self, timeout=5):
            self.opens += 1
            self.ratio = .4
            self.stats.black_streak = 0
            self.stats.mark_signal(True)
            return True

        def close(self):
            self.closes += 1

    cam = ReturningPhysicalCamera(0)
    be = CountingBackend(initial=20)
    eng = make_engine(cam, be)
    eng.cfg.camera.black_frame_limit = 3
    for _ in range(3):
        assert eng.tick().applied is None
        assert be.writes == [] and not eng._meter._ratios
    assert cam.opens == cam.closes == 1
    st = eng.tick()
    assert st.camera_ok and st.applied == be.level > 20
    assert st.message == ""


def test_failed_black_camera_reopen_is_throttled(monkeypatch):
    cam = FakeCamera(0)
    attempts = []
    clock = [100.0]
    def slow_failed_open(timeout):
        attempts.append(timeout)
        clock[0] += 20
        return False
    cam.open = slow_failed_open
    eng = make_engine(cam, CountingBackend())
    eng.cfg.camera.black_frame_limit = 3
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    for _ in range(8):
        eng.tick()
    assert len(attempts) == 1 and eng._reopen_at == 125


@pytest.mark.parametrize("black,mean,expected", [(False, .01, 0), (True, 3.0, 1), (None, .01, 1)])
def test_preflight_uses_probe_signal_instead_of_mean(monkeypatch, capsys, black, mean, expected):
    from camab import preflight, devices
    item = dict(frame=True, backend="DSHOW", shape=[10, 10, 3], mean=mean, seconds=.1)
    if black is not None:
        item["black"] = black
    monkeypatch.setattr(preflight, "_deps", lambda: {})
    monkeypatch.setattr(preflight, "_brightness", lambda **_: {"backends": [{"name": "wmi", "available": True}]})
    monkeypatch.setattr(preflight, "_camera", lambda *_, **__: {"results": [item]})
    monkeypatch.setattr(devices, "full_report", lambda **kw: {"severity": "black" if kw["frame_mean"] is not None and kw["frame_mean"] < 1 else "ok"})
    assert preflight.run_preflight(as_json=True) == expected


def test_preflight_write_diagnostic_restores_even_if_readback_fails(monkeypatch):
    from camab import preflight
    writes = []
    reads = [60]
    def get(_key):
        if reads:
            return reads.pop()
        raise BrightnessError("readback failed")
    be = SimpleNamespace(first=lambda: SimpleNamespace(key="internal:0"), get=get,
                         set=lambda key, level: writes.append(level))
    monkeypatch.setattr(preflight, "backend_report", lambda: {})
    monkeypatch.setattr(preflight, "open_backend", lambda **_: be)
    monkeypatch.setattr(preflight.time, "sleep", lambda _: None)
    rep = preflight._brightness(write_test=True)
    assert writes == [56, 60] and not rep["write_test"]["ok"]


class RecoverableWriteBackend(CountingBackend):
    def __init__(self, initial=35):
        super().__init__(initial=initial)
        self.fail_write = True

    def set(self, key, level):
        if self.fail_write:
            raise BrightnessError("AccessDenied: brightness write refused")
        return super().set(key, level)


def test_manual_write_failure_keeps_actual_level_and_counts_error():
    be = RecoverableWriteBackend(initial=35)
    eng = make_engine(FakeCamera(), be)
    eng._set_status(current=35, applied=35)
    with pytest.raises(BrightnessError, match="AccessDenied"):
        eng.set_level(72)
    st = eng.status
    assert st.current == be.level == 35
    assert st.applied is None
    assert st.errors == 1
    assert "AccessDenied" in st.bright_error
    assert eng.hold_remaining() == 0  # 未写成功，不应进入手动接管。
    eng.pause()
    eng.tick()
    assert eng.status.bright_error == st.bright_error
    be.fail_write = False
    assert eng.set_level(72) == 72
    assert eng.status.bright_error == ""
    assert eng.status.errors == 1


def test_auto_write_failure_persists_across_no_write_tick_until_recovered():
    be = RecoverableWriteBackend(initial=20)
    eng = make_engine(FakeCamera(0.3), be)
    st = eng.tick()
    assert st.current == be.level == 20
    assert st.applied is None and st.errors == 1
    assert "AccessDenied" in st.bright_error
    eng.mapper.cfg.deadband = 101
    quiet = eng.tick()
    assert quiet.applied is None
    assert quiet.bright_error == st.bright_error
    assert quiet.errors == 1
    eng.mapper.cfg.deadband = 4
    be.fail_write = False
    recovered = eng.tick()
    assert recovered.applied == be.level and be.level > 20
    assert recovered.bright_error == ""
    assert recovered.errors == 1


@pytest.mark.parametrize("manual", [False, True])
def test_simulated_write_cannot_clear_real_write_failure(manual):
    be = RecoverableWriteBackend(initial=20)
    eng = make_engine(FakeCamera(0.3), be)
    failure = eng.tick().bright_error
    assert failure
    eng.cfg.engine.dry_run = True
    if manual:
        eng.set_level(72)
    else:
        assert eng.tick().applied is not None
    assert eng.status.bright_error == failure
    assert be.level == 20 and not be.writes


def test_manual_backend_not_ready_records_persistent_failure():
    eng = engine.Engine(AppConfig())
    with pytest.raises(BrightnessError, match="未就绪"):
        eng.set_level(72)
    assert "未就绪" in eng.status.bright_error
    assert eng.status.errors == 1
    assert eng.status.applied is None


def test_manual_dry_run_never_writes_functional_backend():
    be = CountingBackend(initial=35)
    eng = make_engine(FakeCamera(), be)
    eng.cfg.engine.dry_run = True
    assert eng.set_level(72) == 72
    assert be.writes == []
    assert eng.read_level() == 72
    eng.tick()
    assert be.writes == []
    assert eng.status.current == 72


def test_engine_configuration_is_an_independent_snapshot():
    cfg = AppConfig()
    eng = engine.Engine(cfg)
    cfg.mapping.floor = 88
    cfg.analysis.percentile = 80
    assert eng.cfg.mapping.floor != 88
    assert eng.cfg.analysis.percentile != 80


def test_leaving_dry_run_aligns_actual_level_without_false_external_hold():
    be = CountingBackend(initial=20)
    eng = make_engine(FakeCamera(0.3), be)
    cfg = eng.cfg.snapshot()
    cfg.engine.dry_run = True
    eng.apply_config(cfg)
    assert eng.tick().current > be.level
    smooth = eng.mapper.lux_smooth
    cfg.engine.dry_run = False
    eng.apply_config(cfg)
    assert eng.status.current == eng._last_obs_level == 20
    assert eng.mapper.lux_smooth == smooth
    eng.tick()
    assert eng.hold_remaining() == 0
    assert be.writes


def test_dry_run_only_hold_does_not_pause_real_mode():
    be = CountingBackend(initial=20)
    eng = make_engine(FakeCamera(0.3), be)
    cfg = eng.cfg.snapshot()
    cfg.engine.dry_run = True
    eng.apply_config(cfg)
    eng.set_level(95, manual=True)
    assert eng.hold_remaining() > 0
    cfg.engine.dry_run = False
    eng.apply_config(cfg)
    assert eng.hold_remaining() == 0
    assert eng.status.current == eng.read_level() == 20


def test_real_manual_hold_survives_simulated_hold_changes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    be = CountingBackend(initial=20)
    eng = make_engine(FakeCamera(), be)
    eng.set_level(77, manual=True)
    assert eng.mapper.force_cool_until == 160
    cfg = eng.cfg.snapshot()
    cfg.engine.dry_run = True
    clock[0] = 105
    eng.apply_config(cfg)
    clock[0] = 110
    eng.set_level(95, manual=True)
    assert eng.mapper.force_cool_until == 170
    cfg.engine.dry_run = False
    clock[0] = 120
    eng.apply_config(cfg)
    assert eng.mapper.force_cool_until == 160
    assert eng.hold_remaining() == 40
    assert eng.status.current == eng.read_level() == be.level == 77


def test_entering_dry_run_aligns_latest_physical_brightness():
    be = CountingBackend(initial=20)
    eng = make_engine(FakeCamera(0.3), be)
    be.level = 47  # 模式切换前，Fn 键改变了屏幕亮度。
    cfg = eng.cfg.snapshot()
    cfg.engine.dry_run = True
    eng.apply_config(cfg)
    assert eng.status.current == eng._last_obs_level == eng.read_level() == 47
    eng.tick()
    assert eng.hold_remaining() == 0
    assert be.writes == []


def test_apply_config_updates_meter_and_preserves_smoothing():
    eng = make_engine(FakeCamera(), CountingBackend())
    eng.tick()
    smooth = eng.mapper.lux_smooth
    samples = len(eng._meter._ratios)
    cfg = AppConfig()
    cfg.avoid_screen = False
    cfg.analysis.percentile = 76
    cfg.calib = 2.4
    cfg.lux_gamma = 0.8
    cfg.screen_calibrated = True
    cfg.screen_reflection_k = 0.001
    eng.apply_config(cfg)
    assert eng._meter.acfg.percentile == 76
    assert eng._meter.calib == 2.4
    assert eng._meter.lux_gamma == 0.8
    assert eng._meter.compensation.k == 0.001
    assert eng.mapper.lux_smooth == smooth
    assert len(eng._meter._ratios) == samples
    cfg.calib = 8
    assert eng.cfg.calib == 2.4
    assert eng._trigger.is_set()


def test_status_monitor_list_is_detached():
    eng = engine.Engine(AppConfig())
    eng._set_status(monitors=["internal"])
    status = eng.status
    status.monitors.append("changed")
    assert eng.status.monitors == ["internal"]


def test_zero_brightness_is_preserved_on_read_failure():
    be = CountingBackend(initial=0)
    eng = make_engine(FakeCamera(), be)
    eng._set_status(current=0)

    def fail(_key):
        raise BrightnessError("offline")

    be.get = fail
    assert eng.read_level() == 0


def test_backend_verified_write_skips_duplicate_readback():
    class VerifiedBackend(CountingBackend):
        verifies_writes = True

        def __init__(self):
            super().__init__(initial=20)
            self.gets = 0

        def get(self, key):
            self.gets += 1
            return super().get(key)

    be = VerifiedBackend()
    eng = make_engine(FakeCamera(0.3), be)
    st = eng.tick()
    assert len(be.writes) == 1
    assert be.gets == 1  # 只读决策前的当前值，set() 已经负责回读。
    assert st.current == st.applied == be.level


def test_tick_and_calibration_sample_are_serialized_without_blocking_status():
    entered, release = threading.Event(), threading.Event()

    class SlowCamera(FakeCamera):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.maximum = 0

        def read(self, timeout=1.0):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            entered.set()
            try:
                assert release.wait(2)
                return super().read(timeout)
            finally:
                self.active -= 1

    cam = SlowCamera()
    eng = make_engine(cam, CountingBackend())
    worker = threading.Thread(target=eng.tick)
    calibration = threading.Thread(target=eng.sample_once)
    worker.start()
    assert entered.wait(1)
    calibration.start()
    try:
        # 操作锁被硬件线程持有时，状态锁仍可以立即获得。
        assert eng._lock.acquire(timeout=0.1)
        eng._lock.release()
        assert eng.status is not None
    finally:
        release.set()
        worker.join(2)
        calibration.join(2)
    assert not worker.is_alive() and not calibration.is_alive()
    assert cam.maximum == 1


def test_pause_during_capture_prevents_pending_automatic_write():
    entered, release = threading.Event(), threading.Event()

    class SlowCamera(FakeCamera):
        def read(self, timeout=1.0):
            entered.set()
            assert release.wait(2)
            return super().read(timeout)

    be = CountingBackend(initial=20)
    eng = make_engine(SlowCamera(0.3), be)
    worker = threading.Thread(target=eng.tick)
    worker.start()
    assert entered.wait(1)
    try:
        eng.pause()
    finally:
        release.set()
        worker.join(2)
    assert be.writes == []
    assert eng.status.state == "paused"


def test_pause_during_native_write_keeps_paused_status_and_reason():
    entered, release = threading.Event(), threading.Event()

    class SlowBackend(CountingBackend):
        def set(self, key, level):
            entered.set()
            assert release.wait(2)
            return super().set(key, level)

    be = SlowBackend(initial=20)
    eng = make_engine(FakeCamera(0.3), be)
    worker = threading.Thread(target=eng.tick)
    worker.start()
    assert entered.wait(1)
    try:
        eng.pause("用户要求暂停")
    finally:
        release.set()
        worker.join(2)
    assert eng.paused
    assert eng.status.state == "paused"
    assert eng.status.message == "用户要求暂停"
    before = len(be.writes)
    eng.tick()
    assert len(be.writes) == before


def test_stop_timeout_keeps_camera_owned_by_exiting_thread():
    entered, release = threading.Event(), threading.Event()

    class SlowCamera(FakeCamera):
        closed = 0

        def read(self, timeout=1.0):
            entered.set()
            assert release.wait(2)
            return super().read(timeout)

        def close(self):
            self.closed += 1

    cam = SlowCamera(0.3)
    be = CountingBackend(initial=20)
    eng = make_engine(cam, be)
    eng._next_tick = time.monotonic()
    eng._thread = threading.Thread(target=eng._run)
    eng._thread.start()
    assert entered.wait(1)
    try:
        eng.stop(timeout=0.01)
        assert cam.closed == 0
        assert eng.status.state == "stopping"
        assert eng.start() is False
    finally:
        release.set()
        eng._thread.join(2)
    assert cam.closed == 1
    assert be.writes == []
    assert eng.status.state == "idle"
    assert eng.status.running is False


def test_start_is_idempotent_with_live_worker(monkeypatch):
    eng = engine.Engine(AppConfig())
    stopped = threading.Event()
    worker = threading.Thread(target=lambda: stopped.wait(2))
    worker.start()
    eng._thread = worker
    monkeypatch.setattr(eng, "prepare", lambda: pytest.fail("already running"))
    try:
        assert eng.start() is True
        assert eng._thread is worker
    finally:
        stopped.set()
        worker.join(2)


def test_stop_timeout_does_not_wait_indefinitely_for_prepare_lock():
    eng = engine.Engine(AppConfig())
    finished = threading.Event()
    eng._operation_lock.acquire()
    worker = threading.Thread(target=lambda: (eng.stop(timeout=0.01), finished.set()))
    worker.start()
    try:
        assert finished.wait(0.5)
        assert eng.status.state == "stopping"
    finally:
        eng._operation_lock.release()
        worker.join(2)
    assert not worker.is_alive()


def test_stop_during_prepare_cancels_start_and_closes_after_prepare_returns():
    entered, release = threading.Event(), threading.Event()

    class PreparedCamera(FakeCamera):
        closed = 0

        def close(self):
            self.closed += 1

    cam = PreparedCamera()

    class SlowPreparingEngine(engine.Engine):
        def prepare(self):
            with self._operation_lock:
                entered.set()
                assert release.wait(2)
                self._camera = cam
                self._set_status(camera_ok=True)
                return True

    eng = SlowPreparingEngine(AppConfig())
    result = []
    starter = threading.Thread(target=lambda: result.append(eng.start()))
    starter.start()
    assert entered.wait(1)
    try:
        eng.stop(timeout=0.01)
        assert eng.status.state == "stopping"
        assert cam.closed == 0
    finally:
        release.set()
        starter.join(2)
    assert not starter.is_alive()
    assert result == [False]
    assert eng._camera is None and eng._thread is None
    assert cam.closed == 1
    assert eng.status.state == "idle" and eng.status.running is False


def test_external_override_burst_cannot_renew_beyond_90_seconds(monkeypatch):
    eng = make_engine(FakeCamera(), CountingBackend())
    eng._last_obs_level = 40
    clock = [100.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    for index in range(9):
        clock[0] = 100 + index * 10
        assert eng._detect_manual(60 if index % 2 == 0 else 40)
        assert eng.mapper.force_cool_until <= 190
    clock[0] = 190
    assert eng._detect_manual(40) is False
    assert eng.mapper.force_cool_until == 0


def test_invalid_camera_signal_does_not_fill_measurement_window():
    class BlockedCamera(FakeCamera):
        def read(self, timeout=1.0):
            frame = super().read(timeout)
            self.stats.mark_signal(False)
            return frame

    eng = make_engine(BlockedCamera(), CountingBackend())
    eng.tick()
    assert len(eng._meter._ratios) == 0
    assert eng.sample_once() is None
    assert eng.status.camera_ok is False


def test_meter_reset_discards_old_brightness_pairing():
    meter = LightMeter()
    meter.observe_level(30)
    meter.measure(srgb_frame(0.02))
    meter._ae_sign = 1
    meter.reset()
    assert not meter._levels and not meter._series
    assert meter._ae_sign == 0


def test_failed_paused_measurement_never_returns_old_lux():
    eng = make_engine(FakeCamera(), CountingBackend())
    eng._set_status(lux=120)
    eng.pause()
    eng._camera.read = lambda timeout: None
    assert eng.measure_now(timeout=0.1) is None


@pytest.mark.parametrize("camera_ok,state,expected", [
    (True, "running", 42.0), (False, "running", None), (True, "error", None),
])
def test_measure_now_only_accepts_new_valid_camera_measurement(camera_ok, state, expected):
    eng = make_engine(FakeCamera(), CountingBackend())
    eng._set_status(t=1, lux=120)
    eng.trigger = lambda: eng._set_status(t=2, camera_ok=camera_ok, state=state, lux=42)
    assert eng.measure_now(timeout=0.1) == expected


def test_measure_now_timeout_never_returns_cached_lux():
    eng = make_engine(FakeCamera(), CountingBackend())
    eng._set_status(t=1, lux=120, camera_ok=True)
    assert eng.measure_now(timeout=0.1) is None


def test_camera_reconnect_failure_is_retried_after_backoff(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])

    class OfflineCamera(FakeCamera):
        opened = 0

        def __init__(self):
            super().__init__()
            self.stats.opened = False

        def read(self, timeout=1):
            return super().read(timeout) if self.stats.opened else None

        def open(self, timeout=1):
            self.opened += 1
            self.stats.opened = self.opened >= 2
            return self.stats.opened

        def close(self):
            self.stats.opened = False

    cam = OfflineCamera()
    eng = make_engine(cam, CountingBackend())
    eng.tick()
    assert cam.opened == 1 and eng.status.state == "error"
    clock[0] = 101
    eng.tick()
    assert cam.opened == 1
    clock[0] = 106
    eng.tick()
    assert cam.opened == 2
    clock[0] = 107
    eng.tick()
    assert eng.status.camera_ok is True and eng.status.state == "running"


def _camera_with_cached_frame(monkeypatch):
    cam = Camera()
    grab = _Grabber(SimpleNamespace(), interval=0.1)
    grab._frame = np.full((4, 4, 3), 32, np.uint8)
    grab.ok = 1
    grab._frame_t = 123.0
    cam._grab = grab
    monkeypatch.setattr(cam, "exposure", lambda: (None, None))
    return cam, grab


def test_camera_returns_capture_timestamp_and_only_fresh_frames(monkeypatch):
    cam, _grab = _camera_with_cached_frame(monkeypatch)
    frame = cam.read(timeout=0)
    assert frame.t == 123.0 and frame.index == 1
    assert cam.read(timeout=0) is None


def test_camera_waits_on_condition_without_polling(monkeypatch):
    cam, grab = _camera_with_cached_frame(monkeypatch)
    cam.read(timeout=0)
    results = []
    waiter = threading.Thread(target=lambda: results.append(cam.read(timeout=1)))
    waiter.start()
    with grab._ready:
        grab._frame = np.full((4, 4, 3), 44, np.uint8)
        grab.ok += 1
        grab._frame_t = 456.0
        grab._ready.notify_all()
    waiter.join(2)
    assert not waiter.is_alive()
    assert results[0].index == 2 and results[0].t == 456.0


@pytest.mark.parametrize("sample_fps", [2.0, 30.0])
def test_physical_camera_uses_paced_read_without_changing_sensor_fps(monkeypatch, sample_fps):
    class CompatibleCapture:
        def __init__(self):
            self.properties = []
            self.reads = 0
            self.grabs = 0
            self.released = 0

        def isOpened(self):
            return True

        def set(self, key, value):
            self.properties.append((key, value))
            return True

        def get(self, key):
            return {3: 4, 4: 4, 5: 30.0}.get(key, 0.0)

        def read(self):
            self.reads += 1
            return True, np.full((4, 4, 3), 64, np.uint8)

        def grab(self):
            self.grabs += 1
            return False

        def retrieve(self):
            pytest.fail("Physical camera must use its working read() path")

        def release(self):
            self.released += 1

    cap = CompatibleCapture()
    cv2 = SimpleNamespace(
        CAP_ANY=0, CAP_DSHOW=700, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5, CAP_PROP_BUFFERSIZE=38, CAP_PROP_EXPOSURE=15, CAP_PROP_GAIN=14,
        VideoCapture=lambda index, backend: cap,
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    cam = Camera(index=0, fps=sample_fps, backends=("DSHOW",))
    try:
        assert cam.open(timeout=1)
        assert not cam._grab.drain
        assert cam._grab.interval == pytest.approx(1.0 / sample_fps)
        first = cam.read(timeout=1)
        assert first is not None
        second = cam.read(timeout=1)
        assert second is not None and second.index > first.index
        assert cam.stats.signal_ok and cam.stats.success_rate == 1.0
        assert all(key != cv2.CAP_PROP_FPS for key, _value in cap.properties)
        assert cap.reads >= 3 and cap.grabs == 0  # 打开预读 + 两张新帧。
    finally:
        cam.close()
    assert cap.released == 1


def test_grabber_failure_invalidates_cached_frame():
    done = threading.Event()
    grab = None

    class FailedCapture:
        def read(self):
            done.set()
            return False, None

    grab = _Grabber(FailedCapture(), interval=0.01)
    grab._frame = np.ones((3, 3, 3))
    grab.ok = 1
    grab.start()
    try:
        assert done.wait(1)
        # stop() 等待正在完成的失败发布后再检查快照。
    finally:
        grab.stop()
    assert grab.latest() is None
    assert grab.fail >= 1


def test_grabber_close_wakes_waiting_reader():
    grab = _Grabber(SimpleNamespace(), interval=0.1)
    results = []
    waiter = threading.Thread(target=lambda: results.append(grab.wait_latest(0, 10)))
    waiter.start()
    grab.stop()
    waiter.join(1)
    assert not waiter.is_alive()
    assert results == [None]


def test_exposure_uses_cache_when_native_capture_is_busy(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace())
    cam = Camera()
    cam._cap = SimpleNamespace(get=lambda _key: pytest.fail("concurrent native call"))
    cam._grab = _Grabber(cam._cap, interval=0.1)
    cam.stats.exposure, cam.stats.gain = 1 / 30, 2.0
    cam._grab._capture_lock.acquire()
    try:
        assert cam.exposure() == (1 / 30, 2.0)
    finally:
        cam._grab._capture_lock.release()


def test_capture_release_waits_until_native_read_returns():
    entered, release = threading.Event(), threading.Event()

    class BlockedCapture:
        released = 0

        def read(self):
            entered.set()
            assert release.wait(2)
            assert self.released == 0
            return False, None

        def release(self):
            self.released += 1

    cap = BlockedCapture()
    grab = _Grabber(cap, interval=0.1)
    grab.start()
    assert entered.wait(1)
    try:
        grab.stop(timeout=0.01)
        assert grab.is_alive() and cap.released == 0
    finally:
        release.set()
        grab.join(2)
    assert cap.released == 1


def test_grabber_drains_old_frames_but_decodes_only_at_sample_interval(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(camera.time, "monotonic", lambda: clock[0])
    grab = None

    class BufferedCapture:
        grabs = 0
        decodes = 0

        def grab(self):
            self.grabs += 1
            if self.grabs == 21:
                grab._join_evt.set()
                clock[0] = 101.0
            return True

        def retrieve(self):
            self.decodes += 1
            return True, np.full((3, 3, 3), self.grabs, np.uint8)

    cap = BufferedCapture()
    grab = _Grabber(cap, interval=0.5, drain=True)
    grab.run()
    assert cap.grabs == 21
    assert cap.decodes == 2
    assert float(grab.latest()[0, 0, 0]) == 21


def test_wmi_timeout_is_reported_as_brightness_error(monkeypatch):
    from camab import winutil

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("powershell", 6)

    monkeypatch.setattr(winutil, "run_hidden", timeout)
    be = WmiBackend()
    be._win32 = None
    with pytest.raises(BrightnessError, match="PowerShell"):
        be.get("internal:0")


def test_wmi_com_proxy_is_created_in_each_calling_thread(monkeypatch):
    calls = []
    pythoncom = SimpleNamespace(
        COINIT_APARTMENTTHREADED=2,
        com_error=RuntimeError,
        CoInitializeEx=lambda _mode: calls.append(("init", threading.get_ident())),
        CoUninitialize=lambda: calls.append(("end", threading.get_ident())),
    )
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)

    class ComClient:
        def GetObject(self, _path):
            owner = threading.get_ident()
            calls.append(("proxy", owner))

            class Wmi:
                def ExecQuery(self, _query):
                    assert threading.get_ident() == owner
                    return [SimpleNamespace(CurrentBrightness=65)]

            return Wmi()

    be = WmiBackend()
    be._win32 = ComClient()
    assert be._com_get() == 65
    result = []
    worker = threading.Thread(target=lambda: result.append(be._com_get()))
    worker.start()
    worker.join(2)
    assert result == [65]
    assert [action for action, _thread in calls] == ["init", "proxy", "end"] * 2
    assert calls[1][1] != calls[4][1]


def test_ddc_set_uses_the_resolved_handle_and_its_existing_readback(monkeypatch):
    be = brightness.DdcBackend.__new__(brightness.DdcBackend)
    handle = ctypes.c_void_p(123)
    calls = []
    be._dxva2 = SimpleNamespace(SetVCPFeature=lambda h, _code, _level: calls.append(h) or True)
    be._resolve = lambda _key: handle
    be._vcp = lambda _handle, _code: 72
    be._release = lambda handles: calls.extend(handles)
    monkeypatch.setattr(brightness.time, "sleep", lambda _duration: None)
    assert be.set("ddc:0", 75) == 72
    assert calls == [handle, handle]
    assert be.verifies_writes


def test_backend_factory_only_probes_requested_backend(monkeypatch):
    monkeypatch.setattr(brightness.sys, 'platform', 'win32')
    checked = []

    class Wmi:
        name = "wmi"

        def __init__(self, timeout=6):
            pass

        def available(self):
            checked.append("wmi")
            return True

    class Ddc:
        name = "ddc"

        def available(self):
            checked.append("ddc")
            return True

    monkeypatch.setattr(brightness, "WmiBackend", Wmi)
    monkeypatch.setattr(brightness, "DdcBackend", Ddc)
    assert brightness.open_backend(prefer="wmi").name == "wmi"
    assert checked == ["wmi"]
    checked.clear()
    assert brightness.open_backend().name == "wmi"
    assert checked == ["wmi"]
