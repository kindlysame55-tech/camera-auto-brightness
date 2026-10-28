"""引擎：主循环，把摄像头、测光、映射、亮度后端串成一个闭环。

线程模型：:class:`Engine` 在后台线程里跑 ``tick()``，UI/CLI 通过
:attr:`Engine.status` 读取不可变快照，通过 :meth:`pause` / :meth:`resume` /
:meth:`set_level` 下达指令。硬件操作串行执行；状态快照使用独立的短锁，
所以读取界面状态不需要等待摄像头或亮度驱动。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Callable, Optional

from .brightness import BrightnessBackend, BrightnessError, MonitorInfo, open_backend
from .camera import Camera
from .config import AppConfig
from .lightmeter import LightMeter, LuxEstimate
from .metrics import BrightnessMapper, BrightnessTransition, MappingDecision
from .status import Status

log = logging.getLogger(__name__)

#: 外部亮度改动能连续续期的暂停上限（秒）。防止"每拍都被判成外部改动"导致永不恢复。
EXTERNAL_HOLD_MAX_S = 90.0


class Engine:
    """自动亮度闭环。

    :param cfg: 应用配置
    :param on_status: 每次 tick 后的回调（在引擎线程里调用，GUI 需自行转线程）
    """

    def __init__(self, cfg: AppConfig, on_status: Optional[Callable[[Status], None]] = None,
                 backend: Optional[BrightnessBackend] = None) -> None:
        self.cfg = cfg.snapshot()
        self._on_status = on_status
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._trigger = threading.Event()      # 手动触发一次采样

        self._backend = backend
        self._camera: Optional[Camera] = None
        self._meter = LightMeter(
            analysis=replace(self.cfg.analysis, percentile=self.cfg.effective_percentile),
            calib=self.cfg.calib, lux_gamma=self.cfg.lux_gamma,
            compensation=self.cfg.reflection(),
        )
        self._mapper = BrightnessMapper(self.cfg.mapping)
        self._transition = BrightnessTransition()
        self._transition_decision: Optional[MappingDecision] = None
        self._status = Status()
        self._monitor: Optional[MonitorInfo] = None
        self._paused = False
        self._writes = 0
        self._errors = 0
        self._write_mismatch = 0
        self._t0 = 0.0
        self._warm_until = 0.0  # 仅 start() 设定；prepare()/直接 tick 诊断不等待预热。
        self._last_obs_level: Optional[int] = None
        self._started_level: Optional[int] = None
        self._next_tick = 0.0
        #: 干跑/模拟后端时"我们以为的亮度"（真实值不会被改，得自己记）
        self._dry_level: Optional[int] = None
        self._pre_dry_hold_until = 0.0
        self._external_hold_started: Optional[float] = None
        self._camera_fail_streak = 0
        self._reopen_at = 0.0

    def apply_config(self, cfg: AppConfig) -> None:
        """原子更新运行参数，保留测光窗口和映射平滑历史。"""
        snapshot = cfg.snapshot()
        with self._operation_lock:
            self._cancel_transition()
            if self.cfg.engine.dry_run != snapshot.engine.dry_run:
                if snapshot.engine.dry_run:
                    # 保留真实操作的暂停期限；模拟操作产生的暂停不带回真实模式。
                    self._pre_dry_hold_until = self._mapper.force_cool_until
                self._dry_level = None
                cur = self._read_level_locked()
                if snapshot.engine.dry_run:
                    self._dry_level = cur
                else:
                    self._mapper.force_cool_until = self._pre_dry_hold_until
                    self._pre_dry_hold_until = 0.0
                self._last_obs_level = cur
                self._external_hold_started = None
                self._mapper.adopted(cur)
                self._set_status(current=cur, applied=None)
            self.cfg = snapshot
            self._mapper.cfg = snapshot.mapping
            self._meter.acfg = replace(snapshot.analysis, percentile=snapshot.effective_percentile)
            self._meter.calib = snapshot.calib
            self._meter.lux_gamma = snapshot.lux_gamma
            self._meter.compensation = snapshot.reflection()
        self.trigger()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def prepare(self) -> bool:
        """打开亮度后端与摄像头；返回是否全部就绪。"""
        with self._operation_lock:
            return self._prepare_locked()

    def _prepare_locked(self) -> bool:
        if self._camera is not None:
            return True
        try:
            if self._backend is None:
                prefer = self.cfg.engine.backend
                if prefer == 'auto' and self.cfg.engine.monitor:
                    from .platform_support import BRIGHTNESS_BACKENDS
                    prefix = self.cfg.engine.monitor.split(':', 1)[0]
                    if prefix in BRIGHTNESS_BACKENDS and prefix != 'auto':
                        prefer = prefix
                self._backend = open_backend(
                    prefer=prefer,
                    allow_dryrun=self.cfg.engine.allow_dryrun_fallback or self.cfg.engine.dry_run,
                )
            mons = list(self._backend.monitors())
            if not mons:
                raise BrightnessError("未发现可控制的显示器")
            if self.cfg.engine.monitor:
                self._monitor = next((m for m in mons if m.key == self.cfg.engine.monitor), None)
                if self._monitor is None:
                    raise BrightnessError(f'指定显示器 {self.cfg.engine.monitor} 不可用；请在设置中重新选择，或留空自动选择。')
            else:
                self._monitor = mons[0]
            self._set_status(bright_backend=self._backend.name, monitor_key=self._monitor.key,
                             monitors=[str(m) for m in mons])
            log.info("亮度后端：%s，显示器：%s", self._backend.name,
                     self._monitor or "（未发现）")
        except BrightnessError as exc:
            self._set_status(state="error", message=f"亮度后端不可用：{exc}")
            log.error("亮度后端不可用：%s", exc)
            return False

        cam = Camera(index=self.cfg.camera.effective_source(),
                     size=(self.cfg.camera.width, self.cfg.camera.height),
                     fps=self.cfg.camera.fps,
                     lock_exposure=self.cfg.camera.lock_exposure,
                     backends=tuple(self.cfg.camera.backends),
                     black_frame_limit=self.cfg.camera.black_frame_limit)
        if not cam.open(timeout=self.cfg.camera.open_timeout_s):
            cam.close()
            self._set_status(camera_ok=False, camera_error=cam.stats.last_error,
                             state="error", message=f"摄像头打开失败：{cam.stats.last_error}")
            log.error("摄像头打开失败：%s", cam.stats.last_error)
            return False
        self._camera = cam
        self._set_status(camera_ok=True, camera_backend=cam.stats.backend)

        cur = self.read_level()
        self._started_level = cur
        self._last_obs_level = cur
        self._mapper.reset(current=cur)
        self._cancel_transition()
        self._meter.reset()
        self._meter.observe_level(cur)
        self._external_hold_started = None
        self._camera_fail_streak = 0
        self._reopen_at = 0.0
        self._set_status(current=cur)
        return True

    def start(self, block: bool = False) -> bool:
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                # 不允许旧线程仍在退出时启动第二条硬件控制循环。
                return not self._stop.is_set()
            self._stop.clear()
            if not self.prepare():
                return False
            with self._operation_lock:
                if self._stop.is_set():
                    self._close_camera()
                    self._set_status(state="idle", running=False, camera_ok=False, message="已停止")
                    return False
                self._trigger.clear()
                self._t0 = time.monotonic()
                self._warm_until = self._t0 + max(0.1, self.cfg.engine.warmup_s)
                self._next_tick = self._warm_until
                self._thread = threading.Thread(target=self._run, name="camab-engine", daemon=True)
                self._thread.start()
        log.info("引擎已启动（周期 %.1fs，后端 %s，干跑=%s）",
                 self.cfg.engine.interval_s, self._backend.name if self._backend else "?",
                 self.cfg.engine.dry_run)
        if block:
            self._thread.join()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        self._stop.set()
        self._trigger.set()
        th = self._thread
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=max(0.0, deadline - time.monotonic()))
        if th and th.is_alive():
            self._set_status(state="stopping", message="正在等待硬件操作结束")
            return
        # 连接阶段尚未有 _thread，prepare() 也可能被驱动阻塞；不能无界等操作锁。
        if not self._operation_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            self._set_status(state="stopping", message="正在等待设备连接结束")
            return
        try:
            th = self._thread
            if th and th.is_alive():
                # start() 可能在刚才等待锁期间创建线程，由该线程自己的 finally 释放硬件。
                self._set_status(state="stopping", message="正在等待硬件操作结束")
                return
            self._close_camera()
        finally:
            self._operation_lock.release()
        self._set_status(state="idle", running=False, camera_ok=False, message="已停止")
        log.info("引擎已停止")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # ------------------------------------------------------------------ #
    # 外部指令
    # ------------------------------------------------------------------ #
    def pause(self, reason: str = "手动暂停") -> None:
        with self._lock:
            self._paused = True
            self._set_status(state="paused", message=reason,
                             transitioning=False, transition_target=None)
        self._trigger.set()
        log.info("已暂停：%s", reason)

    def resume(self) -> None:
        with self._operation_lock:
            self._cancel_transition()
            cur = self.read_level()
            self._mapper.reset(current=cur)
            self._meter.reset()
            self._meter.observe_level(cur)
            self._external_hold_started = None
            with self._lock:
                self._paused = False
                self._set_status(state="running", message="已恢复")
        self.trigger()
        log.info("已恢复自动调光")

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def sample_once(self, timeout: float = 3.0) -> Optional[LuxEstimate]:
        """测一次光但**不做任何决策、不写亮度**（校准向导用）。

        为什么不复用 ``tick()``：向导打开时会 ``pause()`` 引擎以免自动调光跟用户
        抢亮度，但 ``tick()`` 在暂停状态下直接返回 —— 结果向导一直读到一个**冻结的
        旧照度**，用户换了光照程序也不知道，第二个点就永远记不下去（用户实测反馈）。

        这里用操作锁与引擎线程串行化，避免两处同时读摄像头。
        """
        with self._operation_lock:
            self._cancel_transition()
            cam = self._camera
            if cam is None:
                return None
            frame = cam.read(timeout=timeout)
            if frame is None or not cam.stats.signal_ok or cam.stats.black_streak > 0:
                return None
            est = self._meter.measure(frame.image, frame.exposure_s, frame.gain,
                                      level=self._last_obs_level)
            self._set_status(t=time.time(), lux=est.lux, ratio=est.ratio,
                             samples=est.samples, stabilized=est.stabilized,
                             screen_compensated=est.screen_compensated,
                             compensation_reliable=est.compensation_reliable,
                             camera_ok=True, camera_error=cam.stats.last_error,
                             camera_success=cam.stats.success_rate)
        return est

    def trigger(self) -> None:
        """立刻采样一次，不等下一个周期。"""
        self._trigger.set()

    def hold_remaining(self, now: Optional[float] = None) -> float:
        """手动接管暂停还剩多少秒（0 = 没在暂停）。

        界面用它解释"为什么现在不动"，否则用户很容易以为自动调光挂了。
        """
        now = time.monotonic() if now is None else now
        return max(0.0, self._mapper.force_cool_until - now)

    def set_level(self, level: int, manual: bool = True) -> int:
        """手动设定亮度。``manual=True`` 时会暂停自动调光一段时间。"""
        with self._operation_lock:
            # 手动命令必须先撤销排队的自动渐变，失败后也不再偷偷重试旧目标。
            self._cancel_transition()
            be = self._backend
            if be is None or self._monitor is None:
                exc = BrightnessError("亮度后端未就绪")
                self._record_brightness_error(exc)
                raise exc
            level = max(0, min(100, int(level)))
            if self.cfg.engine.dry_run:
                applied = level
                self._dry_level = applied
            else:
                try:
                    applied = be.set(self._monitor.key, level)
                except BrightnessError as exc:
                    self._record_brightness_error(exc)
                    raise
                if not be.functional:
                    self._dry_level = applied
                else:
                    self._set_status(bright_error="")
            self._mapper.adopted(applied)
            self._meter.observe_level(applied)
            self._last_obs_level = applied
            if manual:
                self._external_hold_started = None
                self._mapper.note_manual_override(hold_s=self.cfg.engine.manual_hold_s)
            self._set_status(current=applied, applied=applied,
                             message=f"手动设置为 {applied}%")
        return applied

    def read_level(self) -> int:
        with self._operation_lock:
            return self._read_level_locked()

    def _read_level_locked(self) -> int:
        be, mon = self._backend, self._monitor
        if be is None or mon is None:
            return 50
        # 干跑/模拟后端：返回"我们以为的亮度"，不然下一拍会把自己的写入当成外部改动
        if self._dry_level is not None and (self.cfg.engine.dry_run or not be.functional):
            return int(self._dry_level)
        try:
            return int(be.get(mon.key))
        except BrightnessError as exc:
            log.warning("读亮度失败：%s", exc)
            return self._status.current

    @property
    def status(self) -> Status:
        with self._lock:
            return replace(self._status, monitors=list(self._status.monitors))

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else ""

    @property
    def mapper(self) -> BrightnessMapper:
        return self._mapper

    @property
    def last_lux(self) -> float:
        """最近一次测得的照度（校准向导用）。"""
        return float(self.status.lux)

    def measure_now(self, timeout: float = 3.0) -> Optional[float]:
        """立刻测一次并返回照度（不等采样周期）。

        暂停状态下走 :meth:`sample_once`（暂停时 tick 不会测光），
        否则等一个 tick 的结果。失败或超时返回 None，防止把旧读数记成新校准点。
        """
        if self.paused:
            est = self.sample_once(timeout=timeout)
            return float(est.lux) if est else None
        before_t = self.status.t
        self.trigger()
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            status = self.status
            if status.t != before_t and status.camera_ok and status.state != "error":
                return float(status.lux)
            if not self.running:
                break
            self._stop.wait(min(0.05, max(0.0, deadline - time.monotonic())))
        return None

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        self._set_status(state="warming", running=True, message="预热中（等自动曝光稳定）")
        try:
            while not self._stop.is_set():
                if self.paused:
                    self._cancel_transition()
                    wait = None
                else:
                    due = min(self._next_tick, self._transition.next_due)
                    wait = max(0.01, due - time.monotonic())
                triggered = self._trigger.wait(timeout=wait)
                if triggered:
                    self._trigger.clear()
                if self._stop.is_set():
                    break
                st: Optional[Status] = None
                try:
                    if triggered or time.monotonic() >= self._next_tick:
                        st = self.tick()
                        now = time.monotonic()
                        self._next_tick = (self._warm_until if now < self._warm_until
                                           else now + self._interval_after(st))
                    else:
                        # 与采样共用同一条硬件线程；不为了动画重复取帧/测光。
                        with self._operation_lock:
                            st = self._advance_transition_locked()
                        self._publish_status(st)
                except Exception as exc:  # noqa: BLE001
                    self._cancel_transition()
                    self._errors += 1
                    log.exception("tick 异常：%s", exc)
                    self._set_status(errors=self._errors, message=f"内部错误：{exc}")
                    self._next_tick = time.monotonic() + self._interval_after(None)
        finally:
            with self._operation_lock:
                self._close_camera()
            self._set_status(state="idle", running=False, camera_ok=False, message="已停止")

    def _close_camera(self) -> None:
        self._cancel_transition()
        if self._camera is not None:
            self._camera.close()
            self._camera = None

    def _interval_after(self, st: Optional[Status]) -> float:
        """下一拍等多久。

        自适应：刚写过亮度就快点回来看结果（响应快），稳定时放慢（省 CPU）。
        关闭自适应时固定用配置周期。
        """
        base = max(0.5, float(self.cfg.engine.interval_s))
        if self._transition.active:
            return base
        if not self.cfg.engine.adaptive_interval or st is None:
            return base
        if st.applied is not None:
            return max(1.0, base * 0.5)
        if abs(st.ideal - st.current) >= max(1, self.cfg.mapping.deadband) or st.feedback_suspect:
            return base
        return min(30.0, base * 1.5)

    def tick(self) -> Status:
        """一次「取帧 → 测光 → 决策 → 写入」。可单独调用，便于测试。"""
        with self._operation_lock:
            st = self._tick_locked()
        self._publish_status(st)
        return st

    def _publish_status(self, st: Status) -> None:
        if self._on_status:
            try:
                self._on_status(st)
            except Exception:  # noqa: BLE001
                log.debug("on_status 回调异常", exc_info=True)

    def _tick_locked(self) -> Status:
        if self.paused or self._stop.is_set():
            self._cancel_transition()
            return self.status
        if time.monotonic() < self._warm_until:
            return self._warming_status()
        cam = self._camera
        if cam is None:
            self._cancel_transition()
            return self.status

        frame = cam.read(timeout=self.cfg.camera.read_timeout_s)
        if self.paused or self._stop.is_set():
            self._cancel_transition()
            return self.status
        if frame is None:
            self._cancel_transition()
            self._camera_fail_streak += 1
            self._errors += 1
            msg = f"取帧失败：{cam.stats.last_error}"
            log.warning(msg)
            now = time.monotonic()
            if (self._camera_fail_streak >= 3 or not cam.stats.opened) and now >= self._reopen_at:
                log.warning("成功率过低（%.0f%%），尝试重开摄像头", cam.stats.success_rate * 100)
                cam.close()
                if self._stop.is_set():
                    return self.status
                if not cam.open(timeout=self.cfg.camera.open_timeout_s):
                    self._reopen_at = time.monotonic() + max(5.0, self.cfg.engine.interval_s)
                    self._set_status(state="error", camera_ok=False,
                                     camera_error=cam.stats.last_error, message=msg,
                                     errors=self._errors)
                    return self.status
                self._camera_fail_streak = 0
                self._reopen_at = 0.0
                self._meter.reset()
                if self._last_obs_level is not None:
                    self._meter.observe_level(self._last_obs_level)
            self._set_status(camera_success=cam.stats.success_rate, camera_error=cam.stats.last_error,
                             camera_ok=False, applied=None, errors=self._errors, message=msg)
            return self.status
        self._camera_fail_streak = 0

        # 画面不可信（连续黑帧）时**保持亮度不动**，不要把"摄像头掉线"当成"天黑了"。
        # 实测踩到过：摄像头间歇掉线 → 引擎一路压到 20% 且再也升不回来。
        if not cam.stats.signal_ok or cam.stats.black_streak > 0:
            self._cancel_transition()
            streak = cam.stats.black_streak
            # 无源虚拟设备也会成功返回黑帧；真实设备重新上线后须重新枚举索引。
            # 短暂暗帧先保持，长期无信号才重开，避免每几帧重启驱动。
            now = time.monotonic()
            limit = max(3, self.cfg.camera.black_frame_limit)
            if streak >= limit and now >= self._reopen_at:
                self._reopen_at = now + max(5.0, self.cfg.engine.interval_s)
                cam.close()
                if self._stop.is_set():
                    return self.status
                cam.open(timeout=self.cfg.camera.open_timeout_s)
                self._reopen_at = time.monotonic() + max(5.0, self.cfg.engine.interval_s)
                self._meter.reset()
                if self._last_obs_level is not None:
                    self._meter.observe_level(self._last_obs_level)
            self._set_status(
                t=time.time(), state="running", running=True,
                camera_ok=False, applied=None,
                message=f"画面全黑（连续 {streak} 帧），"
                        f"已保持亮度 {self._status.current}% 不动",
                camera_success=cam.stats.success_rate,
                camera_error=cam.stats.last_error,
            )
            log.warning("画面全黑（连续 %d 帧），保持亮度 %d%% 不动",
                        streak, self._status.current)
            return self.status
        self._reopen_at = 0.0
        # 不把无效黑帧喂进滚动窗口，否则摄像头恢复后仍会持续压低亮度。
        est = self._meter.measure(frame.image, frame.exposure_s, frame.gain,
                                  level=self._last_obs_level)
        # 先读当前亮度并识别「外部改动」；_write 之后不会碰这两个变量
        cur = self.read_level()
        # 关键顺序：把「测这一帧时屏幕的亮度」在 measure 之后立刻配对记录，
        # 回流检测靠这两支序列的同步性判断（见 lightmeter.feedback_suspect）。
        # 放到 measure 之前会晚一拍、放到 _write 之后会把因果方向记反。
        self._meter.observe_level(cur)
        hold = self._detect_manual(cur)

        dec = self._mapper.decide(est.lux, cur, damped=est.feedback_suspect,
                                  allow_write=not self._transition.active)
        applied: Optional[int] = None
        if hold:
            self._cancel_transition()
            # 刚检测到外部改动，本次不写（让用户的手动操作生效）
            applied = None
            dec = MappingDecision(dec.target, None, "检测到外部改动，暂停自动", dec.lux_smooth)
        elif self._transition.active:
            endpoint = self._transition.target
            assert endpoint is not None
            pending = self._transition_decision
            # 新光照已经反向或进入迟滞带时，不能继续沿过时轨迹调屏幕。
            if (self._mapper.force_cool_until > time.monotonic()
                    or abs(dec.target - cur) < self.cfg.mapping.deadband
                    or (dec.target - cur) * (endpoint - cur) < 0
                    or (est.feedback_suspect and pending and not pending.damped)):
                self._cancel_transition()
            else:
                applied = self._advance_transition_locked(current=cur).applied
        elif dec.applied is not None:
            if self.cfg.mapping.ramp_per_s > 0:
                self._transition_decision = dec
                self._transition.start(cur, dec.applied, time.monotonic(),
                                       cooldown_s=self.cfg.mapping.cooldown_s,
                                       ramp_per_s=self.cfg.mapping.ramp_per_s,
                                       min_interval_s=self.cfg.mapping.min_write_interval_s)
            else:
                applied = self._write(dec)

        st = self._set_status(
            t=time.time(), state="running", running=True,
            lux=est.lux, ratio=est.ratio, samples=est.samples,
            stabilized=est.stabilized, auto_exposure_drift=est.auto_exposure_drift,
            feedback_suspect=est.feedback_suspect,
            screen_compensated=est.screen_compensated,
            compensation_reliable=est.compensation_reliable,
            ideal=dec.target, applied=applied, current=applied if applied is not None else cur,
            reason=(f"平滑调整至 {self._transition.target}%（光照目标 {dec.target}%）"
                    if self._transition.active else dec.reason),
            same_dir_streak=dec.same_dir_streak,
            cooling=dec.applied is None and ("冷却" in dec.reason),
            damped=dec.damped,
            transitioning=self._transition.active, transition_target=self._transition.target,
            camera_ok=True, camera_backend=cam.stats.backend,
            camera_success=cam.stats.success_rate, camera_error=cam.stats.last_error,
            writes=self._writes, write_mismatch=self._write_mismatch, errors=self._errors,
            uptime_s=time.monotonic() - self._t0,
            interval_s=self.cfg.engine.interval_s,
            message="",
        )
        return st

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _warming_status(self) -> Status:
        self._cancel_transition()
        return self._set_status(state="warming", running=True, applied=None,
                                message="预热中（等自动曝光稳定）")

    def _cancel_transition(self) -> None:
        self._transition.cancel()
        self._transition_decision = None
        self._set_status(transitioning=False, transition_target=None)

    def _advance_transition_locked(self, current: Optional[int] = None) -> Status:
        """输出一个渐变步；调用者持有操作锁，任何时候最多一个驱动调用。"""
        now = time.monotonic()
        if not self.paused and not self._stop.is_set() and now < self._warm_until:
            return self._warming_status()
        dec = self._transition_decision
        if (not self._transition.active or dec is None or self.paused
                or self._stop.is_set() or self._mapper.force_cool_until > now
                or not self._status.camera_ok):
            self._cancel_transition()
            return self._set_status(applied=None)
        if now < self._transition.next_due:
            return self._set_status(applied=None)
        started = time.monotonic()
        cur = self.read_level() if current is None else current
        if self._detect_manual(cur):
            self._cancel_transition()
            return self._set_status(current=cur, applied=None,
                                    reason="检测到外部改动，暂停自动")
        level = self._transition.next_level(cur, time.monotonic())
        if level is None:
            return self._set_status(current=cur, applied=None,
                                    transitioning=self._transition.active,
                                    transition_target=self._transition.target)
        step = replace(dec, applied=level,
                       reason=f"平滑调整 {cur}%→{level}%（本段目标 {self._transition.target}%）")
        applied = self._write(step)
        finished = time.monotonic()
        if applied is None or applied == cur or self.paused or self._stop.is_set():
            # 失败 / 面板忽略 / 暂停：保持真实当前值，避免反复空写。
            self._cancel_transition()
        else:
            self._transition.written(applied, finished, finished - started)
        return self._set_status(current=applied if applied is not None else cur,
                                applied=applied, writes=self._writes,
                                write_mismatch=self._write_mismatch, errors=self._errors,
                                transitioning=self._transition.active,
                                transition_target=self._transition.target,
                                reason=step.reason)

    def _record_brightness_error(self, exc: BrightnessError) -> None:
        """保留写入失败，不能被下一次测光或短暂界面提示清掉。"""
        self._errors += 1
        self._set_status(bright_error=str(exc), applied=None, errors=self._errors)
        log.error("写亮度失败：%s", exc)

    def _write(self, dec: MappingDecision) -> Optional[int]:
        assert dec.applied is not None
        if self.paused or self._stop.is_set():
            return None
        be, mon = self._backend, self._monitor
        if be is None or mon is None:
            return None
        level = int(dec.applied)
        if self.cfg.engine.dry_run or not be.functional:
            log.info("[干跑] 亮度 %d%% → %d%%（%s，lux=%.1f）",
                     self._status.current, level, dec.reason, dec.lux_smooth)
            self._writes += 1
            self._last_obs_level = level
            # 干跑也要把"我们以为的亮度"记进状态：否则下一拍 read_level() 读到真实值，
            # 会被 _detect_manual 误判成"用户改了亮度"然后暂停（实测踩到）
            self._dry_level = level
            return level
        try:
            applied = int(be.set(mon.key, level))
        except BrightnessError as exc:
            self._record_brightness_error(exc)
            return None
        self._writes += 1
        self._set_status(bright_error="")
        # 回读校验：某些驱动/显示器会静默忽略写入。校验失败就以**真实值**为准，
        # 否则引擎会以为已经调好了，下一拍又按错误的基准算，来回空转。
        back = applied
        if not getattr(be, "verifies_writes", False):
            try:
                back = int(be.get(mon.key))
            except BrightnessError:
                pass
        if abs(back - level) > 2:
            self._write_mismatch += 1
            log.warning("写入亮度 %d%% 但回读为 %d%%（第 %d 次不一致，驱动或显示器可能不支持写）",
                        level, back, self._write_mismatch)
        self._last_obs_level = back
        applied = back
        # 注意：这里**不要**再 observe_level —— 屏幕亮度已经变了，但这一帧是变之前
        # 拍的；提前记录会让回流检测的因果配对错位。下一拍 tick 会自然记录新亮度。
        log.info("亮度 %d%% → %d%%（%s，lux=%.1f%s）", self._status.current, applied,
                 dec.reason, dec.lux_smooth, "，疑似屏幕回流" if self._status.feedback_suspect else "")
        return applied

    def _detect_manual(self, cur: int) -> bool:
        """发现亮度被外部改动（用户按了 Fn 键 / 电源计划 / 屏幕自己变暗）→ 短暂让出控制权。

        这里有个**很容易被当成"自动调光失灵"**的坑：引擎自己的写入如果没被硬件认，
        或者显示器因为省电自己降亮度，下一拍就会看到"值变了"，然后误判成用户接管、
        暂停很久 —— 而且暂停每拍刷新一次，永远恢复不了（实测踩到）。

        处置：
        * 先排除"画面里混进了屏幕自己的光"（那是我们造成的），这种不暂停只降敏；
        * 用**短冷却**而不是长暂停，让它在没人动的情况下能自己爬回来；
        * 暂停时间封顶（:data:`EXTERNAL_HOLD_MAX_S`），不会无限续期。

        :return: 本次是否刚检测到外部改动
        """
        if self._last_obs_level is None:
            self._last_obs_level = cur
            return False
        if cur == self._last_obs_level:
            return False
        expected = self._last_obs_level
        if abs(cur - expected) >= max(1, self.cfg.engine.resume_threshold):
            if self._status.feedback_suspect:
                log.debug("亮度变化疑似屏幕自身回流（%d → %d），不视为手动接管", expected, cur)
                self._last_obs_level = cur
                return False
            hold = max(0.0, float(self.cfg.engine.external_hold_s))
            if hold <= 0:
                self._last_obs_level = cur
                return False
            now = time.monotonic()
            if (self._external_hold_started is not None and now >= self._mapper.force_cool_until
                    and now - self._external_hold_started < EXTERNAL_HOLD_MAX_S):
                self._external_hold_started = None
            if self._external_hold_started is None:
                self._external_hold_started = now
            elapsed = now - self._external_hold_started
            if elapsed >= EXTERNAL_HOLD_MAX_S:
                # 已经暂停够久了：说明没人真的在手动调，别无限续期
                log.info("外部改动持续出现（%d → %d），但已暂停 %.0fs，恢复自动调光",
                         expected, cur, elapsed)
                self._mapper.force_cool_until = 0.0
                self._external_hold_started = None
                self._last_obs_level = cur
                return False
            log.info("检测到外部改亮度（%d → %d），暂停自动调光 %.0fs",
                     expected, cur, hold)
            self._mapper.note_manual_override(now=now, hold_s=min(hold, EXTERNAL_HOLD_MAX_S - elapsed))
            self._last_obs_level = cur
            return True
        # 1% 级别的漂移（驱动四舍五入）直接接受
        self._last_obs_level = cur
        return False

    def _set_status(self, **kw) -> Status:
        with self._lock:
            if self._paused and kw.get("state") in ("running", "warming"):
                # pause() 可在原生写入尚未返回时生效；完成该写入不能覆盖暂停状态。
                kw["state"] = "paused"
                kw["message"] = self._status.message or "自动调光已暂停"
            for k, v in kw.items():
                if hasattr(self._status, k):
                    setattr(self._status, k, v)
            return replace(self._status, monitors=list(self._status.monitors))
