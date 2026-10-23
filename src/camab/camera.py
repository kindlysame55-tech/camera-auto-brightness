"""摄像头采集层。

职责：把「一帧 BGR 图 + 曝光信息」可靠地取出来，并且**永不阻塞主循环太久**。

关键设计：
* 物理摄像头后台按间隔 `read()`；网络流持续 `grab()`，主循环只消费新帧。
* 多后端依次尝试（DSHOW → MSMF → ANY），重连时重新枚举设备。
* 统计成功率、取帧延迟、曝光/增益，供 preflight 与状态栏显示。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

#: 后端优先级：Windows 上 DSHOW 打开最快最稳，MSMF 兜底
BACKENDS = ("DSHOW", "MSMF", "ANY")


@dataclass
class CameraStats:
    opened: bool = False
    backend: str = ""
    index: int = 0
    width: int = 0
    height: int = 0
    fps_read: float = 0.0
    ok: int = 0
    fail: int = 0
    last_error: str = ""
    open_seconds: float = 0.0
    auto_exposure: Optional[bool] = None
    exposure: Optional[float] = None     # 秒（若可读）
    gain: Optional[float] = None
    black_streak: int = 0                # 连续黑帧计数
    last_peak: float = 0.0               # 最近一帧峰值亮度
    signal_ok: bool = True               # 画面是否可信（黑帧过多时置 False）

    def mark_signal(self, ok: bool) -> None:
        self.signal_ok = bool(ok)

    @property
    def success_rate(self) -> float:
        tot = self.ok + self.fail
        return 1.0 if tot == 0 else self.ok / tot


@dataclass
class Frame:
    image: object                     # numpy BGR
    t: float
    exposure_s: Optional[float] = None
    gain: Optional[float] = None
    index: int = 0


class CameraError(RuntimeError):
    pass


def frame_has_signal(img) -> bool:
    """8 位相机画面是否有可用于测光的空间信号。

    中央 90% 区域至少 0.5% 像素有非零亮度。忽略边缘伪影和稀疏热像素，
    同时不对画面均值设下限，保留亮度很低但有空间信号的真实暗场。
    """
    import numpy as np

    arr = np.asarray(img)
    if arr.ndim not in (2, 3) or not arr.size:
        return False
    height, width = arr.shape[:2]
    margin_y, margin_x = height // 20, width // 20
    core = arr[margin_y:height - margin_y, margin_x:width - margin_x]
    if core.ndim == 3:
        core = np.max(core[..., :3], axis=2)  # 忽略 BGRA 的不透明 alpha。
    signal = np.isfinite(core) & (core >= 1.0)
    return bool(np.count_nonzero(signal) / signal.size >= 0.005)


class _Grabber(threading.Thread):
    """后台抓帧线程：只保留最新一帧。"""

    def __init__(self, cap, interval: float, name: str = "camab-grab",
                 drain: bool = False) -> None:
        super().__init__(name=name, daemon=True)
        self.cap = cap
        self.interval = interval
        self.drain = drain
        self._capture_lock = threading.Lock()
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._frame = None
        self._frame_t = 0.0
        # 注意：不能叫 self.join —— 那是 Thread.join，会被属性遮蔽导致关不掉线程
        self._join_evt = threading.Event()
        self.ok = 0
        self.fail = 0
        self.last_error = ""

    def run(self) -> None:  # pragma: no cover - 线程
        try:
            self._capture_loop()
        finally:
            # VideoCapture 只由采集线程释放；不要与仍在驱动内部的 grab/read 并发 release。
            try:
                with self._capture_lock:
                    self.cap.release()
            except Exception:  # noqa: BLE001
                log.debug("释放摄像头失败", exc_info=True)

    def _capture_loop(self) -> None:
        next_sample = 0.0
        while not self._join_evt.is_set():
            try:
                with self._capture_lock:
                    if self.drain:
                        # 持续 grab 排空驱动缓存；只在需要测光时 retrieve/解码。
                        # 只降低 read() 频率可能越积越多旧帧，BUFFERSIZE 也常被驱动忽略。
                        got = self.cap.grab()
                        if got and time.monotonic() < next_sample:
                            continue
                        got, img = self.cap.retrieve() if got else (False, None)
                        next_sample = time.monotonic() + self.interval
                    else:
                        got, img = self.cap.read()
            except Exception as exc:  # noqa: BLE001
                got, img = False, None
                self.last_error = f"{type(exc).__name__}: {exc}"
            with self._ready:
                if got and img is not None:
                    self._frame = img
                    self._frame_t = time.time()
                    self.ok += 1
                    self.last_error = ""
                else:
                    # 失败时不继续向引擎提供掉线前的旧画面。
                    self._frame = None
                    self.fail += 1
                    if not self.last_error:
                        self.last_error = "cap.read() 返回 False"
                self._ready.notify_all()
            if not self.drain or not got:
                self._join_evt.wait(self.interval)

    def latest(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def wait_latest(self, after: int, timeout: float):
        """等一张未消费的新帧；关闭或采集失败时立即结束等待。"""
        with self._ready:
            failures = self.fail
            self._ready.wait_for(
                lambda: (self._frame is not None and self.ok > after)
                or self._join_evt.is_set() or self.fail != failures,
                timeout=max(0.0, timeout),
            )
            if self._join_evt.is_set() or self._frame is None or self.ok <= after:
                return None
            return self._frame.copy(), self._frame_t, self.ok

    def counts(self) -> tuple[int, int, str]:
        with self._lock:
            return self.ok, self.fail, self.last_error

    def stop(self, timeout: float = 2.0) -> None:
        self._join_evt.set()
        with self._ready:
            self._ready.notify_all()
        try:
            threading.Thread.join(self, timeout=timeout)
        except RuntimeError:  # pragma: no cover - 线程没启动
            pass


class Camera:
    """摄像头封装。

    :param index: 设备序号，默认 0；也可以是字符串源：
        * 本地视频文件 ``D:\\test.mp4``（离线自测用）
        * 网络流 ``http://192.168.1.5:8080/video``（手机 IP Webcam / DroidCam）
        * RTSP ``rtsp://user:pass@ip:554/stream``
    :param size: 采集分辨率（小一点 CPU 占用低，测光足够）
    :param fps: 采样频率上限，默认 2 fps（测光不需要高帧率）
    :param lock_exposure: 尝试锁定曝光（部分驱动不支持）
    :param black_frame_limit: 连续这么多帧全黑就报警（默认 40 ≈ 20s）
    """

    def __init__(self, index: int | str = 0, size: tuple[int, int] = (320, 240),
                 fps: float = 2.0, lock_exposure: bool = False,
                 backends: tuple[str, ...] = BACKENDS,
                 black_frame_limit: int = 40) -> None:
        self.source = index
        self.index = int(index) if isinstance(index, (int, float)) or str(index).isdigit() else -1
        self.is_stream = isinstance(index, str) and not str(index).isdigit()
        self.size = (int(size[0]), int(size[1]))
        self.interval = 1.0 / max(0.1, float(fps))
        self.lock_exposure = lock_exposure
        self.backends = backends
        self.black_frame_limit = int(black_frame_limit)
        self.stats = CameraStats(index=self.index)
        self._cap = None
        self._grab = None
        self._black_streak = 0
        self._looped = 0
        self._last_frame_index = 0

    # -- 打开 -----------------------------------------------------------
    def open(self, timeout: float = 12.0) -> bool:
        """打开摄像头/视频源，返回是否成功。"""
        import cv2

        if self._cap is not None:
            return True
        self._last_frame_index = 0
        self._black_streak = 0
        self.stats = CameraStats(index=self.index)

        if self.is_stream:
            return self._open_stream(timeout)

        t0 = time.monotonic()
        deadline = t0 + max(0.0, timeout)
        errors: list[str] = []
        for name in self.backends:
            if time.monotonic() >= deadline:
                break
            api = getattr(cv2, f"CAP_{name}", cv2.CAP_ANY)
            try:
                cap = cv2.VideoCapture(self.index, api)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            if not cap or not cap.isOpened():
                errors.append(f"{name}: isOpened()=False")
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
                continue
            # 设置参数（失败不致命）
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
                # fps 控制软件取样间隔；保留摄像头自身的受支持帧率。
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:  # noqa: BLE001
                pass
            ok, first = False, None
            read_deadline = min(deadline, time.monotonic() + 8.0)
            while time.monotonic() < read_deadline:
                got, img = cap.read()
                if got and img is not None:
                    ok, first = True, img
                    break
                time.sleep(0.15)
            if not ok:
                errors.append(f"{name}: 打开成功但读不到帧（可能被其它程序占用）")
                try:
                    cap.release()
                except Exception:  # noqa: BLE001
                    pass
                continue

            self._cap = cap
            self.stats.opened = True
            self.stats.backend = name
            self.stats.open_seconds = time.monotonic() - t0
            try:
                self.stats.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                self.stats.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                self.stats.fps_read = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            except Exception:  # noqa: BLE001
                if first is not None:
                    self.stats.height, self.stats.width = first.shape[:2]
            if self.lock_exposure:
                self._try_lock_exposure()
            # Windows 摄像头驱动对连续 grab/retrieve 的行为不一致，沿用已验证的 read 路径。
            self._grab = _Grabber(cap, self.interval, drain=False)
            self._grab.start()
            log.info("摄像头已打开：后端=%s 分辨率=%dx%d 打开耗时=%.2fs",
                     name, self.stats.width, self.stats.height, self.stats.open_seconds)
            return True

        self.stats.last_error = " | ".join(errors) or "未知错误"
        log.error("摄像头打开失败：%s", self.stats.last_error)
        return False

    def _open_stream(self, timeout: float = 12.0) -> bool:
        """打开视频文件 / 网络流（不用后端循环，交给 OpenCV 自己选）。"""
        import cv2

        t0 = time.monotonic()
        src = str(self.source)
        try:
            cap = cv2.VideoCapture(src)
        except Exception as exc:  # noqa: BLE001
            self.stats.last_error = f"打开 {src} 失败：{type(exc).__name__}: {exc}"
            log.error("%s", self.stats.last_error)
            return False
        if not cap or not cap.isOpened():
            if cap is not None:
                cap.release()
            self.stats.last_error = (f"打不开视频源 {src}（网络流请确认地址可访问，"
                                     f"例如手机 IP Webcam 的 http://IP:8080/video）")
            log.error("%s", self.stats.last_error)
            return False

        ok, first = False, None
        deadline = t0 + max(0.0, timeout)
        while time.monotonic() < deadline:
            got, img = cap.read()
            if got and img is not None:
                ok, first = True, img
                break
            time.sleep(0.1)
        if not ok:
            self.stats.last_error = f"{src} 能打开但读不到帧"
            cap.release()
            log.error("%s", self.stats.last_error)
            return False

        self._cap = cap
        self.stats.opened = True
        self.stats.backend = "FILE" if "://" not in src else "STREAM"
        self.stats.open_seconds = time.monotonic() - t0
        if first is not None:
            self.stats.height, self.stats.width = first.shape[:2]
        try:
            self.stats.fps_read = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        except Exception:  # noqa: BLE001
            pass
        interval = self.interval
        if self.stats.backend == "FILE" and self.stats.fps_read > 0:
            interval = max(self.interval, 1.0 / self.stats.fps_read)
        self._grab = _Grabber(cap, interval, drain=self.stats.backend == "STREAM")
        self._grab.start()
        log.info("视频源已打开：%s（%dx%d，%s）", src, self.stats.width, self.stats.height,
                 self.stats.backend)
        return True

    def _try_lock_exposure(self) -> None:  # pragma: no cover - 依赖驱动
        import cv2

        try:
            self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)  # DSHOW: 0.25=手动
            self._cap.set(cv2.CAP_PROP_EXPOSURE, -6.0)
            log.info("已尝试锁定曝光")
        except Exception as exc:  # noqa: BLE001
            log.debug("锁定曝光失败：%s", exc)

    # -- 读取 -----------------------------------------------------------
    def read(self, timeout: float = 3.0) -> Optional[Frame]:
        """取最新一帧；后台线程还没产出就等一会儿。"""
        grab = self._grab
        if grab is None:
            return None
        snapshot = grab.wait_latest(self._last_frame_index, timeout)
        self.stats.ok, self.stats.fail, self.stats.last_error = grab.counts()
        if snapshot is None:
            self.stats.last_error = self.stats.last_error or "等待新帧超时"
            return None
        img, captured_t, index = snapshot
        self._last_frame_index = index
        ex, gn = self.exposure()
        self.stats.exposure, self.stats.gain = ex, gn
        self._check_black(img)
        return Frame(img, captured_t, ex, gn, index)

    #: 连续这么多帧几乎全黑 → 判定"画面不可信"（摄像头掉线/被遮挡），
    #: 而不是"房间很暗"。实测踩到过：摄像头间歇掉线，引擎把黑帧当黑暗，
    #: 一路把屏幕压到 20% 再也不敢升回来。
    BLACK_SIGNAL_FRAMES = 3

    def _check_black(self, img) -> None:
        """连续黑帧检测 + 画面可信度标记。"""
        import numpy as np

        if self.black_frame_limit <= 0:
            self.stats.mark_signal(True)
            return
        try:
            arr = np.asarray(img)
            peak = float(arr.max())
            has_signal = frame_has_signal(arr)
        except Exception:  # noqa: BLE001
            return
        if not has_signal:
            self._black_streak += 1
            self.stats.black_streak = self._black_streak
        else:
            self._black_streak = 0
            self.stats.black_streak = 0
        self.stats.last_peak = peak

        # 快速标记不可信：几帧就够了，不必等 black_frame_limit（那可能是 40 帧 = 20 秒）
        self.stats.mark_signal(self._black_streak < self.BLACK_SIGNAL_FRAMES)

        if self._black_streak == self.black_frame_limit:
            self.stats.last_error = (
                f"连续 {self._black_streak} 帧全黑或无有效空间信号。"
                "请检查相机选择、遮挡或虚拟摄像头的视频源。"
            )
            log.error("%s", self.stats.last_error)

    def exposure(self) -> tuple[Optional[float], Optional[float]]:
        """读取曝光秒数与增益（尽力而为，读不到返回 (None, None)）。"""
        if self._cap is None:
            return None, None
        grab = self._grab
        capture_lock = grab._capture_lock if grab is not None else None
        if capture_lock is not None and not capture_lock.acquire(timeout=0.1):
            # 原生 grab 可能卡在驱动内；属性读取不能等待它无限结束。
            return self.stats.exposure, self.stats.gain
        try:
            return self._read_exposure()
        finally:
            if capture_lock is not None:
                capture_lock.release()

    def _read_exposure(self) -> tuple[Optional[float], Optional[float]]:
        import cv2

        ex = gn = None
        try:
            raw = self._cap.get(cv2.CAP_PROP_EXPOSURE)
            if raw and raw > 0:
                # DSHOW 返回 1/秒 形式的绝对值；MSMF 常返回 0.0
                ex = 1.0 / float(raw) if raw > 1.0 else float(raw)
        except Exception:  # noqa: BLE001
            ex = None
        try:
            raw_g = self._cap.get(cv2.CAP_PROP_GAIN)
            if raw_g and raw_g > 0:
                gn = float(raw_g)
        except Exception:  # noqa: BLE001
            gn = None
        return ex, gn

    def is_auto_exposure(self) -> Optional[bool]:
        import cv2

        if self._cap is None:
            return None
        grab = self._grab
        capture_lock = grab._capture_lock if grab is not None else None
        if capture_lock is not None and not capture_lock.acquire(timeout=0.1):
            return self.stats.auto_exposure
        try:
            v = self._cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
            if not v:
                return None
            self.stats.auto_exposure = v >= 0.5  # DSHOW: 0.75=auto, 0.25=manual
            return self.stats.auto_exposure
        except Exception:  # noqa: BLE001
            return None
        finally:
            if capture_lock is not None:
                capture_lock.release()

    # -- 关闭 -----------------------------------------------------------
    def close(self) -> None:
        grab = self._grab
        if grab is not None:
            grab.stop()
            self._grab = None
        if self._cap is not None:
            if grab is None or grab.ident is None:
                try:
                    self._cap.release()
                except Exception:  # noqa: BLE001
                    pass
            elif grab.is_alive():
                log.warning("摄像头驱动尚未结束读取，采集线程将在返回后自行释放设备")
            self._cap = None
        self.stats.opened = False

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# 子进程探活：某些驱动打开摄像头会长时间卡住，preflight 需要在超时后仍能给出结论
# --------------------------------------------------------------------------- #
_PROBE_SRC = r"""
import json, sys, time
import cv2
sys.path.insert(0, sys.argv[4])
from camab.camera import frame_has_signal
idx = int(sys.argv[1]); w = int(sys.argv[2]); h = int(sys.argv[3])
out = {"index": idx, "results": []}
for name in ("DSHOW", "MSMF", "ANY"):
    api = getattr(cv2, "CAP_" + name, cv2.CAP_ANY)
    t0 = time.time()
    r = {"backend": name, "opened": False, "frame": False, "shape": None, "seconds": 0.0,
         "error": None, "mean": None, "peak": None}
    cap = None
    try:
        cap = cv2.VideoCapture(idx, api)
        r["opened"] = bool(cap and cap.isOpened())
        if r["opened"]:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            deadline = time.time() + 5.0
            means = []; signals = []
            while time.time() < deadline:
                ok, img = cap.read()
                if ok and img is not None:
                    r["frame"] = True; r["shape"] = list(img.shape)
                    means.append(float(img.mean()))
                    signals.append(frame_has_signal(img))
                    r["peak"] = int(img.max()) if r["peak"] is None else max(r["peak"], int(img.max()))
                    if len(means) >= 5:
                        break
                time.sleep(0.1)
            if means:
                r["mean"] = round(sum(means) / len(means), 3)
                r["black"] = not any(signals)
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            if cap: cap.release()
        except Exception:
            pass
    r["seconds"] = round(time.time() - t0, 2)
    out["results"].append(r)
    if r["frame"]:
        break
print(json.dumps(out, ensure_ascii=False))
"""


def probe_in_subprocess(index: int = 0, size: tuple[int, int] = (320, 240),
                        timeout: float = 30.0) -> dict:
    """在子进程里探测摄像头，超时也不影响主进程。"""
    import json
    import subprocess
    import sys
    from pathlib import Path

    from .winutil import run_hidden

    try:
        # run_hidden：不然会闪一个控制台黑框（用户明确反馈过）
        proc = run_hidden(
            [sys.executable, "-c", _PROBE_SRC, str(index), str(size[0]), str(size[1]),
             str(Path(__file__).resolve().parent.parent)],
            timeout=timeout, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"index": index, "results": [], "error": f"探测超时（>{timeout:.0f}s），驱动可能卡住"}
    if proc.returncode != 0:
        return {"index": index, "results": [],
                "error": (proc.stderr or "").strip()[-400:] or f"退出码 {proc.returncode}"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {"index": index, "results": [], "error": f"解析探测结果失败：{exc}"}
