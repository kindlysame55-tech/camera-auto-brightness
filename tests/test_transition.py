"""过渡轨迹和硬件输出调度；可控时钟，覆盖取消与实际写入负载。"""

from types import SimpleNamespace
import queue
import threading
import time

import pytest

from camab import engine
from camab.brightness import BrightnessError
from camab.metrics import BrightnessMapper, BrightnessTransition, MappingConfig, MappingDecision
from test_engine import CountingBackend, FakeCamera, make_engine


@pytest.mark.parametrize("origin,target", [(40, 56), (80, 64)])
def test_transition_moves_in_small_monotonic_steps_and_reaches_endpoint(origin, target):
    transition = BrightnessTransition()
    transition.start(origin, target, 100, cooldown_s=4, ramp_per_s=40, min_interval_s=.6)
    assert transition.next_level(origin, 100.5) is None
    current, levels = origin, [origin]
    for i in range(1, 16):
        now = 100 + .6 * i
        level = transition.next_level(current, now)
        if level is not None:
            transition.written(level, now, 0)
            levels.append(level)
            current = level
    assert current == target and not transition.active
    assert 8 <= len(levels) - 1 <= 10
    assert max(abs(b - a) for a, b in zip(levels, levels[1:])) <= 2
    assert levels == sorted(levels, reverse=origin > target)


def test_delayed_output_cannot_skip_to_endpoint():
    transition = BrightnessTransition()
    transition.start(40, 56, 100, cooldown_s=4, ramp_per_s=40, min_interval_s=.6)
    assert transition.next_level(40, 120) == 42
    assert transition.active


def test_slow_driver_adds_rest_instead_of_spinning_another_write():
    transition = BrightnessTransition()
    transition.start(40, 56, 100, cooldown_s=4, ramp_per_s=40, min_interval_s=.6)
    level = transition.next_level(40, 100.6)
    transition.written(level, 101.8, 1.2)
    assert transition.next_due == pytest.approx(103.0)
    assert transition.next_level(level, 102.9) is None


def test_slow_speed_respects_peak_rate_before_quantization():
    transition = BrightnessTransition()
    transition.start(20, 36, 100, cooldown_s=0, ramp_per_s=2, min_interval_s=.6)
    assert transition.duration == 12  # 16 / 2 * 1.5；smoothstep 的峰值为 1.5。
    current = 20
    for i in range(1, 21):
        level = transition.next_level(current, 100 + .6 * i)
        if level is not None:
            assert level - current <= 2  # 1.2 个百分点 + 整数舍入。
            transition.written(level, 100 + .6 * i, 0)
            current = level
    assert current == 36


def test_tracking_target_does_not_count_more_approved_segments():
    mapper = BrightnessMapper(MappingConfig(deadband=0, cooldown_s=4, max_step=16,
                                             ramp_per_s=40, min_write_interval_s=.6))
    first = mapper.decide(1000, 20, now=100)
    for i in range(20):
        mapper.decide(800, 22, now=101 + i, allow_write=False)
    assert first.applied is not None
    assert mapper.last_write_t == 100
    assert len(mapper.history) == mapper.same_dir_streak == 1
    assert mapper.lux_smooth < 1000


@pytest.fixture
def smooth_engine(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    be = CountingBackend(initial=20)
    cam = FakeCamera(.3)
    eng = make_engine(cam, be, max_step=16, cooldown_s=4, ramp_per_s=40,
                      min_write_interval_s=.6)
    eng.cfg.engine.interval_s = .5
    st = eng.tick()
    assert st.transitioning and st.current == 20 and st.applied is None
    assert not be.writes
    return SimpleNamespace(eng=eng, be=be, cam=cam, clock=clock)


def advance(case, seconds=.6):
    case.clock[0] += seconds
    with case.eng._operation_lock:
        return case.eng._advance_transition_locked()


def test_output_ticks_do_not_capture_or_smooth_more_frames(smooth_engine):
    case = smooth_engine
    series = len(case.eng._meter._series)
    for _ in range(10):
        advance(case)
    assert len(case.eng._meter._series) == series
    assert len(case.be.writes) >= 4
    assert not case.eng.status.transitioning
    assert case.eng.status.current == case.be.level == 36


@pytest.mark.parametrize("operation", ["pause", "stop", "manual", "calibration", "config", "black", "failure"])
def test_pending_transition_is_cancelled_by_control_or_invalid_input(smooth_engine, operation):
    case = smooth_engine
    assert advance(case).applied is not None
    eng = case.eng
    if operation == "pause":
        eng.pause()
    elif operation == "stop":
        eng._stop.set()
    elif operation == "manual":
        eng.set_level(72)
    elif operation == "calibration":
        assert eng.sample_once() is not None
    elif operation == "config":
        eng.apply_config(eng.cfg.snapshot())
    elif operation == "black":
        case.cam.ratio = 0
        eng.tick()
    else:
        case.cam.read = lambda **_: None
        eng.tick()
    before = list(case.be.writes)
    for _ in range(4):
        advance(case)
    assert case.be.writes == before
    assert not eng.status.transitioning


def test_external_fn_brightness_change_cancels_pending_transition(smooth_engine):
    case = smooth_engine
    advance(case)
    case.be.level = 72
    before = list(case.be.writes)
    st = advance(case)
    assert case.be.writes == before
    assert st.current == 72 and case.eng.hold_remaining() > 0
    assert not st.transitioning


def test_changed_light_direction_cancels_old_transition(smooth_engine):
    case = smooth_engine
    advance(case)
    # 去掉测光窗口延迟，单独证明过期的已批准目标不会继续写。
    case.eng._meter.reset()
    case.cam.ratio = .0009
    before = list(case.be.writes)
    case.eng.tick()
    for _ in range(3):
        advance(case)
    assert case.be.writes == before
    assert not case.eng.status.transitioning


def test_failed_output_keeps_true_level_and_persistent_error(smooth_engine):
    case = smooth_engine
    def failed_set(*_):
        raise BrightnessError("AccessDenied")
    case.be.set = failed_set
    st = advance(case)
    assert st.current == 20 and st.applied is None and not st.transitioning
    assert st.bright_error == "AccessDenied" and st.errors == 1
    for _ in range(3):
        advance(case)
    assert case.eng.status.bright_error == "AccessDenied" and case.eng.status.errors == 1


def test_smooth_dry_run_never_writes_the_real_backend(smooth_engine):
    case = smooth_engine
    cfg = case.eng.cfg.snapshot()
    cfg.engine.dry_run = True
    case.eng.apply_config(cfg)
    case.eng.mapper.reset(current=20)
    case.eng.tick()
    for _ in range(10):
        advance(case)
    assert case.be.level == 20 and not case.be.writes
    assert case.eng.status.current == 36 and case.eng.hold_remaining() == 0


@pytest.mark.parametrize("write_s,total_s", [(.02, 5.58), (.35, 8.55)])
def test_realistic_backends_use_bounded_serial_small_steps(smooth_engine, write_s, total_s):
    case = smooth_engine
    starts = []
    old_set = case.be.set
    def slow_set(key, level):
        starts.append(case.clock[0])
        case.clock[0] += write_s
        return old_set(key, level)
    case.be.set = slow_set
    while case.eng._transition.active:
        case.clock[0] = case.eng._transition.next_due
        with case.eng._operation_lock:
            case.eng._advance_transition_locked()
    trajectory = [20] + case.be.writes
    assert trajectory == [20, 21, 23, 25, 27, 29, 31, 33, 35, 36]
    assert len(case.be.writes) == 9
    assert max(b - a for a, b in zip(trajectory, trajectory[1:])) == 2
    assert min(b - a for a, b in zip(starts, starts[1:])) >= .59 + write_s
    assert case.clock[0] - 100 == pytest.approx(total_s)
    for _ in range(10):
        advance(case)
    assert len(case.be.writes) == 9


@pytest.mark.parametrize("operation", ["pause", "stop"])
def test_cancel_during_output_read_prevents_pending_native_write(smooth_engine, operation):
    case = smooth_engine
    entered, release = threading.Event(), threading.Event()
    def blocked_get(_key):
        entered.set()
        assert release.wait(2)
        return case.be.level
    case.be.get = blocked_get
    case.clock[0] += .6
    worker = threading.Thread(target=lambda: advance(case, seconds=0))
    worker.start()
    assert entered.wait(1)
    try:
        if operation == "pause":
            case.eng.pause("用户暂停")
        else:
            case.eng._stop.set()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert case.be.writes == [] and not case.eng.status.transitioning
    if operation == "pause":
        assert case.eng.status.state == "paused" and case.eng.status.message == "用户暂停"


def test_pause_during_uninterruptible_write_keeps_paused_state(smooth_engine):
    case = smooth_engine
    entered, release = threading.Event(), threading.Event()
    old_set = case.be.set
    def blocked_set(key, level):
        entered.set()
        assert release.wait(2)
        return old_set(key, level)
    case.be.set = blocked_set
    worker = threading.Thread(target=lambda: advance(case))
    worker.start()
    assert entered.wait(1)
    try:
        case.eng.pause("暂停保留")
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert case.eng.status.state == "paused" and case.eng.status.message == "暂停保留"
    assert not case.eng.status.transitioning
    before = list(case.be.writes)
    for _ in range(4):
        advance(case)
    assert case.be.writes == before


def test_drift_cannot_reverse_a_forward_transition():
    transition = BrightnessTransition()
    transition.start(40, 56, 100, cooldown_s=4, ramp_per_s=40, min_interval_s=.6)
    # 硬件舍入后当前值比轨迹领先，下一步应等待而不是退回 41%。
    assert transition.next_level(43, 100.6) is None
    assert transition.next_level(56, 101.2) is None
    assert not transition.active


def test_background_worker_outputs_without_waiting_for_the_next_sample():
    entered = threading.Event()
    class ObservedBackend(CountingBackend):
        def set(self, key, level):
            result = super().set(key, level)
            entered.set()
            return result
    cam = FakeCamera(.3)
    cam.read = lambda **_: pytest.fail("output timer must not capture a new frame")
    be = ObservedBackend(initial=20)
    eng = make_engine(cam, be, ramp_per_s=40, min_write_interval_s=.6)
    eng._set_status(camera_ok=True, current=20)
    eng._next_tick = time.monotonic() + 30
    eng._transition.start(20, 36, time.monotonic() - 5,
                          cooldown_s=4, ramp_per_s=40, min_interval_s=.6)
    eng._transition_decision = MappingDecision(100, 36, "test", 400)
    statuses = []
    eng._on_status = statuses.append
    eng._thread = threading.Thread(target=eng._run)
    eng._thread.start()
    try:
        assert entered.wait(2)
    finally:
        eng.stop(timeout=2)
    assert not eng._thread.is_alive()
    assert be.writes == [22]
    assert not eng._meter._series
    assert any(st.applied == 22 for st in statuses)


@pytest.mark.parametrize("wake", ["trigger", "config"])
def test_started_warmup_cannot_be_bypassed_by_wakeups(monkeypatch, wake):
    clock = [100.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    be = CountingBackend(initial=20)
    eng = make_engine(FakeCamera(.3), be, max_step=16, cooldown_s=4,
                      ramp_per_s=40, min_write_interval_s=.6)
    eng.cfg.engine.warmup_s = 5
    eng.cfg.engine.interval_s = 30
    statuses = queue.Queue()
    eng._on_status = statuses.put
    assert eng.start()
    try:
        if wake == "trigger":
            eng.trigger()
        else:
            cfg = eng.cfg.snapshot()
            cfg.engine.warmup_s = 0  # 运行设置变化不能缩短已开始的设备预热。
            eng.apply_config(cfg)
        assert statuses.get(timeout=1).state == "warming"
        assert not be.writes and not eng._meter._series
        clock[0] = 104.9
        eng.trigger()
        assert statuses.get(timeout=1).state == "warming"
        assert not be.writes
        clock[0] = 105
        eng.trigger()
        assert statuses.get(timeout=1).transitioning
        clock[0] = 105.6
        eng.trigger()
        assert statuses.get(timeout=1).applied == 21
        assert be.writes == [21]
    finally:
        eng.stop(timeout=2)
    assert not eng._thread.is_alive()


def test_warmup_blocks_pending_output_but_preserves_immediate_manual_control(smooth_engine):
    case = smooth_engine
    case.eng._warm_until = 105
    st = advance(case)
    assert st.state == "warming" and not st.transitioning
    assert not case.be.writes
    assert case.eng.set_level(72) == case.be.level == 72
    assert case.be.writes == [72]


def test_prepare_only_diagnostic_tick_does_not_start_warmup():
    be = CountingBackend(initial=20)
    eng = make_engine(FakeCamera(.3), be)
    eng.cfg.engine.warmup_s = 5
    assert eng.prepare()
    assert eng.tick().applied == be.level > 20
