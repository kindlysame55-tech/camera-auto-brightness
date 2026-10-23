"""托盘常驻 + 状态窗口 + 设置面板。

结构：
* 主线程跑 tkinter（Windows 上 Tk 必须在主线程）。
* pystray 托盘图标用 ``run_detached()`` 挂在后台。
* 引擎在独立线程，UI 每 0.5s 读一次 :class:`~camab.status.Status` 快照。
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

from .calib_dialog import CalibrationWizard
from .config import AppConfig, default_config_path, stop_flag_path
from .metrics import (INTERVAL_FAST, INTERVAL_SLOW, RAMP_FAST, RAMP_SLOW, PRESETS,
                      interval_from_speed, ramp_from_speed, speed_from_interval,
                      speed_from_ramp)
from .reflection import ReflectionCompensation
from .engine import Engine
from .status import Status
from .ui_theme import (BG, FG, DIM, ACCENT, WARN, ERROR, PRIMARY, ON_PRIMARY, SUCCESS, FIELD,
                       update_widget)

log = logging.getLogger(__name__)


def _compact_error(error: str) -> str:
    """Keep the main error readable; full driver details remain in the log."""
    text = " ".join(str(error).split())
    lower = text.lower()
    if any(token in lower for token in (
        "access denied", "access is denied", "0x80070005", "-2147024891",
        "拒绝访问", "访问被拒绝", "权限不足",
    )):
        return "系统拒绝访问。请关闭程序后重新启动。"
    return text if len(text) <= 80 else text[:77] + "…"


def make_icon_image(level: int, paused: bool = False):
    """按当前亮度生成托盘图标（一个渐亮的小太阳）。"""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    frac = max(0.0, min(1.0, level / 100.0))
    if paused:
        fill = (150, 150, 150, 255)
    else:
        fill = (int(120 + 135 * frac), int(150 + 90 * frac), int(60 + 40 * frac), 255)
    d.ellipse((14, 14, 50, 50), fill=fill)
    for i in range(8):
        import math

        a = math.radians(i * 45)
        x0, y0 = 32 + 13 * math.cos(a), 32 + 13 * math.sin(a)
        x1, y1 = 32 + (20 + 8 * frac) * math.cos(a), 32 + (20 + 8 * frac) * math.sin(a)
        d.line((x0, y0, x1, y1), fill=fill, width=3)
    return img


def ensure_window_visible(win: tk.Misc, timeout: float = 3.0) -> bool:
    """确保 Tk 窗口真的被映射出来，并且**尺寸够放内容**。

    只在 ``update()`` 里打转是不够的：窗口管理器可能还没给它分配位置，
    这时 ``winfo_width()`` 还是 1、``winfo_rootx()`` 还是 0，
    截图会抓到屏幕左上角的一块（实测踩到过：截到了浏览器和终端）。

    另外：Toplevel 的 ``winfo_reqwidth()`` 只反映"请求尺寸"，不保证放得下所有子控件
    （实测窗口内容右边被切掉）。这里按子控件实际右边缘重算一次。

    **注意**：Toplevel 的父窗口如果是 withdraw 状态，它永远不会被映射。
    调用方要保证父窗口是显示的。
    """
    try:
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.update_idletasks()
        win.update()

        # 按子控件真实右/下边缘算内容尺寸（winfo_reqwidth 会偏小）
        need_w, need_h = 0, 0
        for child in win.winfo_children():
            try:
                need_w = max(need_w, child.winfo_x() + child.winfo_width())
                need_h = max(need_h, child.winfo_y() + child.winfo_height())
            except Exception:  # noqa: BLE001
                continue
        want_w = max(200, need_w + 24, win.winfo_reqwidth())
        want_h = max(150, need_h + 24, win.winfo_reqheight())
        # 显式给一个不贴边的位置：某些会话里窗口管理器会把窗口放在 (0,0)，
        # 标题栏跑到屏幕外，截图会缺一条边（实测踩到）。
        win.geometry(f"{want_w}x{want_h}+120+120")
        deadline = time.time() + timeout
        while time.time() < deadline:
            win.update()
            if win.winfo_width() > 50:
                return True
            time.sleep(0.1)
        return win.winfo_width() > 50
    except Exception:  # noqa: BLE001
        log.debug("窗口显示检查失败", exc_info=True)
        return False


def window_scale(win: tk.Misc) -> float:
    """窗口的 DPI 缩放系数（Tk 坐标是逻辑像素，抓屏得到的是物理像素）。"""
    try:
        import ctypes

        hwnd = win.winfo_id()
        # GetDpiForWindow：Win10 1607+
        dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
        if dpi:
            return max(1.0, float(dpi) / 96.0)
    except Exception:  # noqa: BLE001
        pass
    try:
        return max(1.0, float(win.tk.call("tk", "scaling")) / 1.3333)
    except Exception:  # noqa: BLE001
        return 1.0


def grab_window(win: tk.Misc, pad: int = 0):
    """把窗口区域截图成 PIL Image。

    Tk 的 ``winfo_width()`` 是**逻辑像素**，而 ``ImageGrab`` 按**物理像素**截屏，
    在 125%/150% 缩放的系统上直接用逻辑尺寸会截缺一条边（实测踩到）。
    这里按 DPI 缩放换算物理区域，抓完再缩回逻辑尺寸，保证图完整且比例正确。
    """
    from PIL import ImageGrab

    win.update_idletasks()
    win.update()
    s = window_scale(win)
    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    px, py = int(x * s), int(y * s)
    pw, ph = int((w + 2 * pad) * s), int((h + 2 * pad) * s)
    img = ImageGrab.grab(bbox=(px - pad, py - pad, px + pw - pad, py + ph - pad))
    if s > 1.02 and img.width > w:
        img = img.resize((w + 2 * pad, h + 2 * pad), 1)   # 1 = LANCZOS 之外的快速抗锯齿
    return img


def _print_window(hwnd: int):
    """用 PrintWindow 抓某个窗口的位图，返回 PIL Image（Windows 专用）。"""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    user32 = ctypes.WinDLL("user32.dll")
    gdi32 = ctypes.WinDLL("gdi32.dll")

    rect = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise OSError("GetClientRect 失败")
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise OSError(f"窗口尺寸异常：{w}x{h}")

    hdc = user32.GetDC(wintypes.HWND(hwnd))
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    try:
        # PW_RENDERFULLCONTENT = 2，没这个标志抓不到 DWM 合成的窗口
        ok = user32.PrintWindow(wintypes.HWND(hwnd), memdc, 2)
        if not ok:
            raise OSError("PrintWindow 返回 0")

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h            # 负值 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0        # BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(wintypes.HWND(hwnd), hdc)


class TrayApp:
    """托盘 + 窗口应用。"""

    def __init__(self, cfg: AppConfig, screenshot: Optional[str] = None,
                 calib_screenshot: Optional[str] = None,
                 settings_screenshot: Optional[str] = None,
                 config_path: Optional[Path] = None) -> None:
        self.cfg = cfg.snapshot()
        self.engine = Engine(self.cfg.snapshot())
        self._config_path = Path(config_path) if config_path else default_config_path()
        self._icon = None
        self._win: Optional[tk.Tk] = None
        self._settings: Optional[tk.Toplevel] = None
        self._last_status: Optional[Status] = None
        self._startup_error = ""
        self._quitting = False
        self._screenshot = screenshot
        self._calib_screenshot = calib_screenshot
        self._settings_screenshot = settings_screenshot
        self._calib = None
        self._health_warned = False
        self._ui_queue = queue.SimpleQueue()
        self._worker = None
        self._connecting = False
        self._poll_id = None
        self._tray_key = None
        self._tray_title = None
        self._feedback_until = 0.0
        self._manual_pending = None
        self._manual_busy = False
        self._screen_calibrating = False
        self._details_visible = False
        self._quit_requested = False
        self._sample_busy = False

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #
    def start(self, selftest: bool = False) -> int:
        from .display import configure_dpi
        configure_dpi()
        self._win = tk.Tk()
        self._win.title("摄像头自动亮度")
        self._win.configure(bg=BG)
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        if selftest:
            # UI smoke tests deliberately avoid acquiring any real hardware.
            self._render(self.engine.status)
            return self._selftest()
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="camab-ui-work")
        self._start_tray()
        self._retry_engine()
        self._poll_id = self._win.after(100, self._poll)
        try:
            self._win.mainloop()
        finally:
            self._shutdown()
        return 0

    def _camera_health_check(self) -> None:
        """启动后检查摄像头有没有画面；全黑就明说，别让用户以为自动调光失灵。"""
        st = self.engine.status
        if not st.camera_ok and st.state == "error":
            return                      # 启动就失败的情况已经在状态栏里了
        if (st.ratio or 0) > 0.0015 and not (st.message and "全黑" in st.message):
            return                      # 有画面，正常
        if self._health_warned:
            return
        self._health_warned = True
        self._set_status_text("摄像头没有有效画面。请打开隐私快门，或关闭占用摄像头的应用后重试。", WARN)

    def _selftest(self) -> int:
        """渲染若干帧、点一遍按钮，然后退出（CI/无人环境用）。"""
        assert self._win is not None
        try:
            for i in range(6):
                self._win.update()
                self._poll()
                time.sleep(0.1)
            self._render(self.engine.status)
            self._win.update()
            if self._screenshot:
                self._save_screenshot(self._screenshot)
            if self._calib_screenshot:
                # 校准向导是 Toplevel：父窗口如果是 withdraw 状态，它永远不会被映射
                # （实测 winfo_width() 恒为 1、截图抓不到）。先让主窗口显示出来。
                self._show_window()
                self._win.update()
                time.sleep(0.3)
                cal = CalibrationWizard(
                    self._win,
                    read_lux=lambda: max(1.0, float(self.engine.status.lux)),
                    apply_level=lambda lv: None,
                    get_level=lambda: int(self.engine.status.current),
                    on_done=lambda _a: None,
                    floor=int(self.cfg.mapping.floor), ceil=int(self.cfg.mapping.ceil))
                cal.selftest(self._calib_screenshot)
                self._on_close()
            opens = self.open_settings()
            if opens is not None and opens.winfo_exists():
                opens.update()
                if self._settings_screenshot:
                    from .tray import ensure_window_visible as _ensure  # noqa: PLC0415

                    _ensure(opens)
                    img = grab_window(opens, pad=6)
                    p = Path(self._settings_screenshot)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    img.save(p)
                    print(f"设置面板截图已保存：{p}（{img.width}x{img.height}）")
                opens.destroy()
            self.toggle_pause()
            self.toggle_pause()
            print("界面自检通过：窗口/状态渲染/设置面板/暂停恢复 均正常")
            if self._startup_error:
                print(f"（注意：{self._startup_error}）")
            return 0
        except Exception as exc:  # noqa: BLE001
            log.exception("界面自检失败")
            print(f"界面自检失败：{type(exc).__name__}: {exc}")
            return 1
        finally:
            self._shutdown()

    def _save_screenshot(self, path: str) -> None:
        """把主窗口截下来（写文档用）。

        用 Windows 的 ``PrintWindow`` 按窗口句柄直接抓，不依赖窗口是否在最前、
        也不受 DPI 缩放影响（用屏幕坐标抓会被缩放和遮挡坑到，实测抓到过浏览器）。
        """
        from pathlib import Path

        assert self._win is not None
        w = self._win
        w.deiconify()
        w.lift()
        for _ in range(4):          # 多跑几帧，让 Tk 完成布局与绘制
            w.update_idletasks()
            w.update()
            time.sleep(0.15)

        img = None
        err = None
        try:
            img = _print_window(w.winfo_id())
        except Exception as exc:  # noqa: BLE001
            err = exc
        if img is None:
            try:                    # 退路：屏幕坐标抓图
                img = grab_window(w, pad=4)
            except Exception as exc:  # noqa: BLE001
                print(f"截图失败（不影响使用）：{err or exc}")
                return

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p)
        print(f"截图已保存：{p}（{img.width}x{img.height}）")

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        from .ui_main import build_main
        build_main(self)

    # -- 亮度滑块 ---------------------------------------------------------
    def _on_scale_press(self, _evt) -> None:
        self._scale_dragging = True

    def _on_scale_drag(self, val) -> None:
        """拖动/键盘改值时只更新数字；程序自己改值时（_scale_guard）不响应。

        注意：参数就用 ttk 回调传来的值，**不要去读 var_level** ——
        实测在窗口刚建好、变量还没被写过时，`var_level.get()` 会返回旧值（60），
        导致界面显示和滑块位置对不上。
        """
        if self._scale_guard:
            return
        try:
            level = int(float(val))
        except (TypeError, ValueError):
            level = int(float(self.var_level.get()))
        update_widget(self.lbl_level, text=f"{level}%")

    def _nudge_level(self, delta: int) -> str:
        """方向键微调 ±1%，并把结果立刻应用。

        取值统一用 ``self.scale.get()``（控件自己的值），不读 ``var_level``：
        实测变量在某些时序下会返回旧值，导致界面与实际不一致。
        """
        lv = max(0, min(100, int(float(self.scale.get())) + delta))
        self.scale.set(lv)
        self._on_scale_drag(lv)
        self._apply_scale_level()
        return "break"  # 不再执行 ttk 自带的第二次方向键增量。

    def _on_scale_release(self, _evt) -> None:
        """松手才真正写亮度（拖动过程中不写，否则会疯狂调屏幕）。"""
        self._scale_dragging = False
        self._apply_scale_level()

    # -- 响应速度 ---------------------------------------------------------
    def _set_speed_drag(self, dragging: bool) -> None:
        self._speed_dragging = dragging
        if not dragging:
            self._apply_speed()

    def _on_speed_drag(self, _val=None) -> None:
        """拖动中只更新文字，松手才写配置（避免每像素都存一次盘）。"""
        if self._speed_guard:
            return
        self._refresh_speed_label()

    def _refresh_speed_label(self, ramp: Optional[float] = None) -> None:
        """速度文字：数字 + 人话标签（滑块越靠右越平滑）。"""
        if ramp is None:
            ramp = ramp_from_speed(self.var_speed.get())
        if ramp <= 8:
            tag = "平滑"
        elif ramp <= 18:
            tag = "均衡"
        elif ramp <= 30:
            tag = "灵敏"
        else:
            tag = "很快"
        update_widget(self.lbl_speed, text=f"{ramp:.1f}%/s {tag}")

    def _apply_speed(self) -> None:
        ramp = ramp_from_speed(self.var_speed.get())
        draft = self.cfg.snapshot()
        draft.mapping.ramp_per_s = ramp
        if not self._commit_config(draft):
            return
        self.var_speed_value.set(f"{ramp:g}")
        self._refresh_speed_label(ramp)
        self._set_status_text(f"响应速度已设为 {ramp:.1f}%/秒（越慢过渡越平滑）", WARN)

    def _apply_custom_speed(self) -> None:
        """「自定义 %/秒」输入框：填多少就是多少（钳到合理范围）。"""
        raw = (self.var_speed_value.get() or "").strip()
        try:
            ramp = float(raw)
            if not math.isfinite(ramp):
                raise ValueError("non-finite speed")
        except ValueError:
            self.var_speed_value.set(f"{self.cfg.mapping.ramp_per_s:g}")
            self._set_status_text(f"「{raw}」不是数字，已还原", WARN)
            return
        ramp = round(max(RAMP_SLOW, min(RAMP_FAST, ramp)), 1)
        if abs(ramp - float(self.cfg.mapping.ramp_per_s)) < 1e-9:
            self.var_speed_value.set(f"{ramp:g}")
            return
        draft = self.cfg.snapshot()
        draft.mapping.ramp_per_s = ramp
        if not self._commit_config(draft):
            self.var_speed_value.set(f"{self.cfg.mapping.ramp_per_s:g}")
            return
        self._speed_guard = True          # 滑块跟着动，但别再触发一次保存
        try:
            self.var_speed.set(speed_from_ramp(ramp))
        finally:
            self._speed_guard = False
        self.var_speed_value.set(f"{ramp:g}")
        self._refresh_speed_label(ramp)
        self._set_status_text(f"响应速度已自定义为 {ramp:g}%/秒", ACCENT)

    def apply_preset(self, name: str) -> None:
        """套用手感预设（采样周期 + 迟滞/冷却/步进/速度），立刻生效并存盘。"""
        draft = self.cfg.snapshot()
        if not draft.engine.apply_preset(name):
            return
        draft.apply_mapping_preset(name)
        if not self._commit_config(draft):
            return
        self._speed_guard = True
        try:
            self.var_speed.set(speed_from_ramp(self.cfg.mapping.ramp_per_s))
        finally:
            self._speed_guard = False
        self._refresh_speed_label()
        self.var_speed_value.set(f"{self.cfg.mapping.ramp_per_s:g}")
        if getattr(self, "_settings", None) is not None and self._settings.winfo_exists():
            self.var_interval.set(f"{self.cfg.engine.interval_s:g}")
            self.var_sample_speed.set(speed_from_interval(self.cfg.engine.interval_s))
            self._refresh_sample_label(self.cfg.engine.interval_s)
            self._refresh_interval_state()
        self._set_status_text(
            f"已套用「{dict(sensitive='灵敏', balanced='均衡', calm='平滑')[name]}」：周期 {self.cfg.engine.interval_s}s，"
            f"变化速度 {self.cfg.mapping.ramp_per_s:g}%/s", ACCENT)

    def _apply_scale_level(self, level: Optional[int] = None) -> None:
        if self._calib is not None or self._screen_calibrating:
            self._set_status_text("校准期间请在校准窗口中调整亮度。", WARN)
            return
        if level is None:
            try:
                level = int(float(self.scale.get()))
            except Exception:  # noqa: BLE001
                level = int(float(self.var_level.get()))
        level = max(0, min(100, int(level)))
        if self._worker is not None:
            self._manual_pending = level
            self._flush_manual_level()
            return
        try:
            applied = self.engine.set_level(level, manual=True)
            self.var_level.set(applied)
            self._manual_done(applied, None)
        except Exception as exc:  # noqa: BLE001
            self._manual_done(None, str(exc))

    def _flush_manual_level(self) -> None:
        if self._manual_busy or self._manual_pending is None:
            return
        level, self._manual_pending = self._manual_pending, None
        self._manual_busy = True
        self._submit(lambda: self.engine.set_level(level, manual=True), self._manual_done)

    def _manual_done(self, applied, error) -> None:
        self._manual_busy = False
        if error:
            self._set_status_text(f"设置亮度失败：{_compact_error(error)}", ERROR,
                                  persistent=True)
        else:
            if not self._scale_dragging and self._manual_pending is None:
                self.var_level.set(applied)
                update_widget(self.lbl_level, text=f"{applied}%")
            hold = self.cfg.engine.manual_hold_s
            suffix = f"{hold:.0f} 秒后恢复自动。" if hold > 0 else "自动调光将继续接管。"
            self._set_status_text(f"已{'模拟' if self.cfg.engine.dry_run else '手动'}设为 {applied}%。{suffix}", WARN)
        self._flush_manual_level()

    def _submit(self, work, done) -> None:
        """Workers exchange Python data; only the Tk thread executes callbacks."""
        if self._quitting or self._worker is None:
            return
        def job():
            try:
                result = work()
                error = None
            except Exception as exc:  # noqa: BLE001
                log.exception("后台操作失败")
                result, error = None, str(exc)
            self._ui_queue.put(lambda: done(result, error))
        self._worker.submit(job)

    def _retry_engine(self) -> None:
        if self._connecting or self._screen_calibrating or self._calib is not None or self._worker is None:
            return
        self._connecting = True
        self._startup_error = ""
        self._set_status_text("正在连接摄像头与显示器…", ACCENT)
        def connect():
            self.engine.stop()
            if self._quitting:
                return False
            if self.engine.status.state == "stopping":
                raise RuntimeError("旧设备操作尚未结束，请稍后再重试")
            self.engine = Engine(self.cfg.snapshot())
            ok = self.engine.start()
            if self._quitting:
                self.engine.stop()
            return ok
        def done(ok, error):
            self._connecting = False
            if not ok:
                self._startup_error = error or self.engine.status.message or "设备连接失败"
                self._set_status_text(self._startup_error + "；排除占用或遮挡后点击重试连接。", ERROR)
            else:
                self._health_warned = False
                self._set_status_text("设备已连接，正在等待环境光稳定。", SUCCESS)
                self._win.after(6000, self._camera_health_check)
        self._submit(connect, done)

    def _sample_now(self) -> None:
        if self._sample_busy:
            return
        if self._connecting or not self.engine.running:
            self._set_status_text("请先连接设备。", WARN)
            return
        if not self.engine.paused:
            self.engine.trigger()
            self._set_status_text("已请求立即采样。", ACCENT)
            return
        self._sample_busy = True
        def done(value, error):
            self._sample_busy = False
            self._set_status_text(
                f"采样失败：{error or '未取得有效画面'}" if error or value is None
                else f"当前环境光 {value:,.1f} lx；保持暂停。",
                WARN if error or value is None else ACCENT)
        self._submit(lambda: self.engine.measure_now(timeout=2.5), done)

    def _nudge_speed(self, delta: int) -> str:
        self.var_speed.set(max(0, min(100, self.var_speed.get() + delta)))
        self._on_speed_drag()
        self._apply_speed()
        return "break"

    def _toggle_details(self) -> None:
        self._details_visible = not self._details_visible
        if self._details_visible:
            self.details.pack(fill="x", padx=24, pady=(0, 16))
            self._render(self.engine.status)
        else:
            self.details.pack_forget()
        self._win.update_idletasks()

    def _commit_config(self, draft: AppConfig) -> bool:
        try:
            candidate = draft.snapshot()
            candidate.save(self._config_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("设置保存失败：%s", exc)
            self._set_status_text(f"设置保存失败：{exc}。当前设置仍保持原值。", ERROR)
            return False
        self.cfg = candidate
        panel = getattr(self, "_settings_panel", None)
        if panel is not None:
            panel.sync_config()
        self._apply_cfg()
        return True

    # ------------------------------------------------------------------ #
    # 状态刷新
    # ------------------------------------------------------------------ #
    def _poll(self) -> None:
        self._poll_id = None
        if self._quitting:
            return
        # 优雅退出：外部写了 stop.flag 就自己收尾退出，
        # 这样通知区域的图标会被正常注销（强杀会留幽灵图标）。
        if self._check_stop_flag() and self._quitting:
            return
        for _ in range(50):
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("界面操作回调失败")
            if self._quitting:
                return
        st = self.engine.status
        self._last_status = st
        try:
            if self._win is not None and self._win.state() != "withdrawn":
                self._render(st)
            else:
                self._update_tray(st)
        except Exception:  # noqa: BLE001
            log.debug("渲染状态失败", exc_info=True)
        if not self._quitting and self._win is not None and self._worker is not None:
            delay = 500 if self._win.state() != "withdrawn" else 1000
            self._poll_id = self._win.after(delay, self._poll)

    def _render(self, st: Status) -> None:
        update_widget(self.lbl_lux, text=f"环境光  {st.lux:,.1f} lx" if st.camera_ok else "环境光  -- lx")
        update_widget(self.lbl_target, text=f"自动目标  {st.ideal}%" if st.samples else "自动目标  —")
        simulated = self.cfg.engine.dry_run or st.bright_backend == "dryrun"
        update_widget(self.lbl_mode, text="模拟调光 · 不调整屏幕" if simulated else "等效照度 · 本地测光",
                      fg=WARN if simulated else DIM)
        for name, button in self._preset_buttons.items():
            preset = PRESETS[name]
            selected = (abs(self.cfg.engine.interval_s-preset["interval_s"]) < 1e-6
                        and all(abs(getattr(self.cfg.mapping, key)-value) < 1e-6
                                for key, value in preset.items() if key != "interval_s"))
            update_widget(button, bg=PRIMARY if selected else FIELD,
                          fg=ON_PRIMARY if selected else FG,
                          activeforeground=ON_PRIMARY if selected else FG)
        states = {
            "running": ("自动调光中", SUCCESS), "paused": ("自动调光已暂停", WARN),
            "warming": ("正在等待测光稳定", ACCENT), "error": ("设备异常", ERROR),
            "idle": ("设备尚未连接", DIM), "stopping": ("正在等待硬件操作结束", WARN),
        }
        text, color = states.get(st.state, (st.state, DIM))
        hold = self.engine.hold_remaining()
        blackout = bool(st.message and "全黑" in st.message)
        if self._connecting:
            text, color = "正在连接摄像头与显示器…", ACCENT
        elif self._startup_error:
            text, color = "设备连接失败；排除占用或遮挡后点击重试连接。", ERROR
        elif st.bright_error:
            text, color = f"无法调节屏幕亮度 · {_compact_error(st.bright_error)}", ERROR
        elif blackout:
            text, color = f"没有有效画面，保持 {st.current}% · 请检查视频源、摄像头连接或遮挡。", WARN
        elif not st.camera_ok and st.camera_error:
            text, color = f"摄像头无法取得画面 · {_compact_error(st.camera_error)}", WARN
        elif simulated:
            text, color = ("模拟调光中 · 屏幕亮度不会变化。" if self.cfg.engine.dry_run
                           else "未连接到可控制的显示器 · 当前仅模拟，屏幕亮度不会变化。"), WARN
        elif self.engine.paused:
            text, color = "自动调光已暂停 · 可以手动调节或立即采样。", WARN
        elif hold > 0.5:
            text, color = f"手动接管中 · {hold:.0f} 秒后恢复自动。", WARN
        elif st.transitioning:
            text, color = "自动调光中 · 正在柔和过渡", SUCCESS
        elif st.state == "error" and st.message:
            text = st.message
        update_widget(self.lbl_state, text=text, fg=color)
        self._scale_guard = True
        try:
            if not self._scale_dragging and not self._manual_busy and self._manual_pending is None:
                if self.var_level.get() != st.current:
                    self.var_level.set(st.current)
                update_widget(self.lbl_level, text=f"{st.current}%")
        finally:
            self._scale_guard = False
        update_widget(self.lbl_hold_hint,
                      text=f"手动调节后暂停自动 {self.cfg.engine.manual_hold_s:g} 秒；方向键微调 1%。")
        calibration = self._calib is not None or self._screen_calibrating
        self.scale.state(["disabled"] if calibration or self._connecting else ["!disabled"])
        pause_label = "恢复自动" if self.engine.paused else "立即恢复自动" if hold > 0.5 else "暂停自动"
        update_widget(self.btn_pause, text=pause_label,
                      state="disabled" if self._connecting or calibration or not self.engine.running else "normal")
        update_widget(self.btn_sample,
                      state="disabled" if self._connecting or calibration or self._sample_busy or not self.engine.running else "normal")
        update_widget(self.btn_retry, text="连接中…" if self._connecting else "重试连接",
                      state="disabled" if self._connecting or calibration else "normal")
        if time.monotonic() > self._feedback_until:
            update_widget(self.lbl_feedback, text="")
        if self._details_visible:
            info = [f"画面亮度 {st.ratio:.4f} · 有效样本 {st.samples}",
                    f"相机 {st.camera_backend or '未连接'} · 成功率 {st.camera_success*100:.0f}%",
                    f"亮度后端 {st.bright_backend or '未连接'} · 当前周期 {st.interval_s:g}s"]
            if st.feedback_suspect:
                info.append("疑似屏幕反光；已降低灵敏度。")
            if st.auto_exposure_drift:
                info.append(f"相机曝光漂移 {st.auto_exposure_drift:+.2f}")
            if st.camera_error:
                info.append(st.camera_error)
            if st.bright_error:
                info.append(st.bright_error)
            if self._startup_error:
                info.append(self._startup_error)
            update_widget(self.lbl_sub, text="\n".join(info))
            update_widget(self.lbl_log,
                          text=f"调整 {st.writes} 次 · 错误 {st.errors} · 运行 {st.uptime_s/60:.1f} 分钟\n最近：{st.reason or '等待测光'}")
        self._update_tray(st)

    def _update_tray(self, st: Status) -> None:
        if self._icon is None:
            return
        paused = self.engine.paused or self.engine.hold_remaining() > 0.5
        key = (int(st.current), paused)
        title = f"自动亮度 {st.current}%（{st.lux:.0f} lx）"
        try:
            if key != self._tray_key:
                self._icon.icon = make_icon_image(*key)
                self._tray_key = key
            if title != self._tray_title:
                self._icon.title = title
                self._tray_title = title
        except Exception:  # noqa: BLE001
            log.debug("托盘状态更新失败", exc_info=True)

    def _set_status_text(self, text: str, color: str = FG, *, persistent: bool = False) -> None:
        self._feedback_until = math.inf if persistent else time.monotonic() + 8.0
        if self._win is not None:
            update_widget(getattr(self, "lbl_feedback", None), text=text, fg=color)

    # ------------------------------------------------------------------ #
    # 托盘
    # ------------------------------------------------------------------ #
    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001
            log.warning("pystray 不可用，跳过托盘图标：%s", exc)
            return
        st = self.engine.status
        menu = pystray.Menu(
            pystray.MenuItem(lambda _i: f"自动亮度 {self.engine.status.current}%", None, enabled=False),
            pystray.MenuItem(lambda _i: f"环境光 {self.engine.status.lux:.1f} lx", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("显示窗口", lambda *_: self._ui_queue.put(self._show_window),
                             default=True),
            pystray.MenuItem("自动校准亮度曲线…",
                             lambda *_: self._ui_queue.put(self.open_calibration)),
            pystray.MenuItem("暂停 / 恢复", lambda *_: self._ui_queue.put(self.toggle_pause)),
            pystray.MenuItem("立即采样", lambda *_: self._ui_queue.put(self._sample_now)),
            pystray.MenuItem("设置…", lambda *_: self._ui_queue.put(self.open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda *_: self._ui_queue.put(self._on_quit)),
        )
        self._icon = pystray.Icon("camab", make_icon_image(st.current or 50),
                                  "摄像头自动亮度", menu)
        try:
            self._icon.run_detached()
        except Exception as exc:  # noqa: BLE001
            log.warning("托盘图标启动失败：%s", exc)
            self._icon = None

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def toggle_pause(self) -> None:
        if self._calib is not None or self._screen_calibrating:
            self._set_status_text("校准结束后才能切换自动调光。", WARN)
            return
        if self.engine.paused or self.engine.hold_remaining() > 0.5:
            if self._worker is None:
                self.engine.resume()
            else:
                self._submit(self.engine.resume,
                             lambda _result, error: self._set_status_text(
                                 f"恢复失败：{error}" if error else "自动调光已恢复。",
                                 ERROR if error else SUCCESS))
        else:
            self.engine.pause("用户手动暂停")

    # ------------------------------------------------------------------ #
    # 自动校准
    # ------------------------------------------------------------------ #
    def open_calibration(self) -> None:
        """打开三点校准向导：用户拖滑块定亮度，软件反推曲线。"""
        if self._win is None or self._screen_calibrating or self._connecting:
            return
        if self._calib is not None and self._calib.win.winfo_exists():
            self._calib.win.lift()
            return
        if self.engine.status.lux <= 0.6:
            messagebox.showwarning(
                "先把摄像头搞定",
                "现在测到的照度接近 0 —— 摄像头可能没画面、被挡住，或还没稳定。\n\n"
                "自动校准需要能测到环境光。可以先：\n"
                "· 跑 `scripts\\start.cmd devices` 看摄像头状态\n"
                "· 确认镜头对着房间（不是贴着墙或盖着）",
                parent=self._win)
            return

        self._calib_was_paused = self.engine.paused
        self.engine.pause("校准中（自动调光已暂停）")
        self._calib = CalibrationWizard(
            self._win,
            # 必须用 measure_now：暂停状态下 tick 不测光，用 status.lux 会一直读到
            # 冻结的旧值，用户换了光照也记不了第二个点（用户实测反馈）
            read_lux=self._read_lux_fresh,
            apply_level=lambda lv: self.engine.set_level(lv, manual=False),
            get_level=lambda: int(self.engine.status.current),
            on_done=self._on_calibrated,
            floor=int(self.cfg.mapping.floor),
            ceil=int(self.cfg.mapping.ceil),
        )
        # 向导关掉后恢复自动（成功保存时也会走到这里）
        wizard = self._calib
        wizard.win.bind("<Destroy>", lambda event: self._after_calibration()
                        if event.widget is wizard.win else None, add="+")

    def _read_lux_fresh(self) -> float:
        """向导用的"此刻"照度：暂停时也能真的去测一次。"""
        try:
            val = self.engine.measure_now(timeout=2.5)
            if val is not None:
                return float(val)
        except Exception:  # noqa: BLE001
            log.debug("向导取样失败", exc_info=True)
        raise RuntimeError("没有取得新画面，请检查摄像头后再试。")

    def _after_calibration(self) -> None:
        self._calib = None
        if self._quit_requested:
            self._on_quit()
            return
        if not self._quitting and self.engine.paused and not getattr(self, "_calib_was_paused", False):
            if self._worker is None:
                self.engine.resume()
            else:
                self._submit(self.engine.resume, lambda _value, _error: None)

    def _on_calibrated(self, anchors: list) -> None:
        """向导算出的折点写进配置并立刻生效。"""
        draft = self.cfg.snapshot()
        draft.mapping.anchors = [(float(lux), int(lv)) for lux, lv in anchors]
        if not self._commit_config(draft):
            raise OSError("曲线保存失败，请检查配置目录的写入权限后重试。")
        self._refresh_curve_label()
        log.info("校准曲线已保存：%s", self.cfg.mapping.clamped().describe_curve())

    def _show_window(self) -> None:
        if self._win is not None:
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
            self._render(self.engine.status)

    def _on_close(self) -> None:
        """关窗口 = 收起到托盘，不是退出。"""
        if self._icon is None:
            self._on_quit()
            return
        if self._calib is not None or self._screen_calibrating:
            self._set_status_text("请先完成或取消校准，再收起窗口。", WARN)
            return
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.destroy()
        if self._win is not None:
            self._win.withdraw()
            if self._icon is not None:
                try:
                    self._icon.notify("仍在后台运行，右键托盘图标可退出", "摄像头自动亮度")
                except Exception:  # noqa: BLE001
                    pass

    def _check_stop_flag(self) -> bool:
        """看到停止信号就优雅退出；返回 True 表示已经在退出。"""
        try:
            flag = stop_flag_path()
            if not flag.exists():
                return False
            flag.unlink(missing_ok=True)
            log.info("收到停止信号，正在优雅退出")
            self._on_quit()
            return True
        except Exception:  # noqa: BLE001
            log.debug("处理停止信号失败", exc_info=True)
            return False

    def _on_quit(self) -> None:
        """托盘菜单「退出」/ 停止信号 共用：收图标 → 停引擎 → 关窗口。"""
        if self._calib is not None or self._screen_calibrating:
            self._quit_requested = True
            self._set_status_text("正在恢复校准前的亮度，完成后退出…", WARN)
            if self._calib is not None:
                self._calib.cancel()
            if getattr(self, "_screen_cancel", None) is not None:
                self._screen_cancel.set()
            return
        self._quitting = True
        try:
            if self._icon is not None:
                self._icon.stop()          # 关键：让通知区域注销这个图标
        except Exception:  # noqa: BLE001
            log.debug("注销托盘图标失败", exc_info=True)
        if self._win is not None:
            try:
                self._win.quit()
            except Exception:  # noqa: BLE001
                pass

    def _dispose_ui_variables(self, event) -> None:
        """Tk variables must be destroyed on the same thread as their widgets."""
        if event.widget is not self._win:
            return
        for key, value in tuple(vars(self).items()):
            if isinstance(value, tk.Variable):
                setattr(self, key, None)
        panel = getattr(self, "_settings_panel", None)
        if panel is not None:
            panel.vars.clear()
            panel.var_dry = panel.var_avoid = None

    def _shutdown(self) -> None:
        self._quitting = True
        if getattr(self, "_screen_cancel", None) is not None:
            self._screen_cancel.set()
        if self._worker is not None:
            self._worker.shutdown(wait=False, cancel_futures=True)
        if self._win is not None:
            try:
                if self._poll_id is not None:
                    self._win.after_cancel(self._poll_id)
                self._win.destroy()
            except tk.TclError:
                pass
        try:
            self.engine.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
        log.info("已退出")

    # ------------------------------------------------------------------ #
    # 设置面板
    # ------------------------------------------------------------------ #
    def open_settings(self) -> Optional[tk.Toplevel]:
        from .ui_settings import SettingsPanel
        if self._win is None:
            return None
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.deiconify()
            self._settings.lift()
            return self._settings
        self._settings_panel = SettingsPanel(self)
        return self._settings

    # -- 采样速度（在设置面板的"自动校准"组里）--------------------------
    def _refresh_sample_label(self, interval: Optional[float] = None) -> None:
        var = getattr(self, "var_sample_speed", None)
        lbl = getattr(self, "lbl_sample", None)
        if var is None or lbl is None:
            return None                       # 设置面板还没打开过
        if interval is None:
            interval = interval_from_speed(var.get())
        if interval <= 1.2:
            tag = "很灵敏"
        elif interval <= 3.0:
            tag = "均衡"
        elif interval <= 6.0:
            tag = "省电"
        else:
            tag = "很省电"
        update_widget(lbl, text=f"{interval:g}s {tag}")
        return None

    def _on_sample_speed_drag(self, _val=None) -> None:
        """拖动中只更新文字，不动配置（松手才写，见 `_apply_sample_speed`）。"""
        if getattr(self, "_speed_guard", False):
            return
        var = getattr(self, "var_sample_speed", None)
        if var is None:
            return
        iv = interval_from_speed(var.get())
        self.var_interval.set(f"{iv:g}")
        self._refresh_sample_label(iv)

    def _nudge_interval(self, delta: int) -> str:
        """滑块聚焦时用左右方向键微调（±1 档），立刻生效。"""
        var = getattr(self, "var_sample_speed", None)
        if var is None:
            return "break"
        self.var_sample_speed.set(max(0, min(100, int(var.get()) + delta)))
        self._on_sample_speed_drag()
        self._apply_sample_speed()
        return "break"

    def _apply_sample_speed(self) -> None:
        """滑块松手 → 把当前滑块位置的采样周期写进配置并立刻生效。

        早先只绑了 ``command``（拖动中触发），而它只更新文字不写配置 ——
        拖完滑块什么都没发生，用户看到的就是"采样速度设置不生效"。
        """
        var = getattr(self, "var_sample_speed", None)
        if var is None:
            return
        self._apply_interval(interval_from_speed(var.get()))

    def _refresh_interval_state(self, applied=None) -> None:
        """在滑块下面写明"现在到底多久测一次"，让生效与否一眼可见。"""
        lbl = getattr(self, "lbl_interval_state", None)
        if lbl is None:
            return
        iv = self.cfg.engine.interval_s if applied is None else applied
        live = self.engine.cfg.engine.interval_s
        text = (f"每 {iv:g} 秒采样一次（引擎侧 {live:g}s）" if abs(iv-live) < 0.01
                else f"正在应用每 {iv:g} 秒采样（引擎侧当前 {live:g}s）")
        update_widget(lbl, text=text)

    def _apply_interval(self, iv: float) -> None:
        """统一的落地点：钳位 → 写配置 → 存盘 → 灌进运行中的引擎 → 刷新文字。"""
        iv = round(max(INTERVAL_FAST, min(INTERVAL_SLOW, float(iv))), 2)
        draft = self.cfg.snapshot()
        draft.engine.interval_s = iv
        if not self._commit_config(draft):
            self.var_interval.set(f"{self.cfg.engine.interval_s:g}")
            return
        self._speed_guard = True
        try:
            self.var_sample_speed.set(speed_from_interval(iv))
        finally:
            self._speed_guard = False
        self.var_interval.set(f"{iv:g}")
        self._refresh_sample_label(iv)
        update_widget(self.lbl_sample, fg=ACCENT)
        self._refresh_interval_state(iv)
        self._set_status_text(f"采样速度已设为每 {iv:g} 秒一次（立即生效）", ACCENT)

    def _apply_custom_interval(self) -> None:
        """「自定义 秒/次」输入框：填多少就是多少（钳到合理范围）。"""
        raw = (self.var_interval.get() or "").strip()
        try:
            iv = float(raw)
            if not math.isfinite(iv):
                raise ValueError("non-finite interval")
        except ValueError:
            self.var_interval.set(f"{self.cfg.engine.interval_s:g}")
            self._set_status_text(f"「{raw}」不是数字，已还原", WARN)
            return
        self._apply_interval(iv)

    def _refresh_curve_label(self) -> None:
        try:
            update_widget(getattr(self, "lbl_curve", None),
                text="当前曲线：" + self.cfg.mapping.clamped().describe_curve()
                     + "\n换房间 / 挪摄像头 / 觉得偏亮偏暗时，重跑一次向导即可。")
            panel = getattr(self, "_settings_panel", None)
            if panel is not None and panel.win.winfo_exists():
                panel.draw_curve()
        except Exception:  # noqa: BLE001
            log.debug("刷新曲线文字失败", exc_info=True)

    def calibrate_screen(self) -> None:
        """Measure reflection in a worker and always restore the starting level."""
        if self._win is None or self._screen_calibrating or self._calib is not None or self._connecting:
            return
        if not self.engine.running or not self.engine.status.camera_ok:
            self._set_status_text("请先连接摄像头，再进行屏幕反光标定。", WARN)
            return
        from .ui_theme import button
        was_paused = self.engine.paused
        self._screen_calibrating = True
        self.engine.pause("屏幕反光标定中")
        cancelled = threading.Event()
        self._screen_cancel = cancelled
        box = tk.Toplevel(self._win)
        box.title("屏幕反光标定")
        box.configure(bg=BG)
        box.resizable(False, False)
        tk.Label(box, text="正在测量屏幕反光", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(padx=20, pady=(18, 6))
        progress = tk.Label(box, text="屏幕将短暂切换两档亮度，请保持摄像头和房间光线不变。\n完成或取消后，会恢复开始时的屏幕亮度。",
                            bg=BG, fg=DIM, justify="left", font=("Microsoft YaHei UI", 10))
        progress.pack(padx=20, pady=8)
        def cancel():
            cancelled.set()
            update_widget(progress, text="正在取消并恢复屏幕亮度，请稍候…")
        box.protocol("WM_DELETE_WINDOW", cancel)
        box.bind("<Escape>", lambda _e: cancel())
        button(box, "取消标定", cancel).pack(pady=(0, 18))

        def work():
            pairs = []
            original = None
            error = ""
            try:
                original = self.engine.read_level()
                for level in (90, 30):
                    if cancelled.is_set() or self._quitting:
                        break
                    actual = self.engine.set_level(level, manual=False)
                    if cancelled.wait(1.8) or self._quitting:
                        break
                    for _ in range(3):
                        if cancelled.is_set() or self._quitting:
                            break
                        estimate = self.engine.sample_once(timeout=3.0)
                        if estimate is not None:
                            pairs.append((actual, estimate.raw_ratio))
                            break
                    else:
                        raise RuntimeError("没有取得有效画面")
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
            finally:
                if original is not None:
                    try:
                        self.engine.set_level(original, manual=False)
                    except Exception as exc:  # noqa: BLE001
                        error = f"恢复亮度失败：{exc}"
                if not was_paused and not self._quitting:
                    self.engine.resume()
            result = ReflectionCompensation.estimate(pairs) if len(pairs) == 2 else None
            return result, error

        def done(payload, error):
            self._screen_calibrating = False
            if box.winfo_exists():
                box.destroy()
            if self._quit_requested:
                self._on_quit()
                return
            if error:
                self._set_status_text(f"标定失败：{error}", ERROR)
                return
            result, failure = payload
            if cancelled.is_set():
                self._set_status_text(f"已取消标定。{failure}" if failure else "已取消标定并恢复亮度。", WARN)
            elif failure or result is None or not result.calibrated:
                self._set_status_text(f"标定未成功：{failure or (result.note if result else '没有足够的有效观测点')}。请检查画面后重试。", WARN)
            else:
                draft = self.cfg.snapshot()
                draft.screen_reflection_k = result.k
                draft.screen_calibrated = True
                if self._commit_config(draft):
                    self._set_status_text(f"屏幕反光标定已保存（系数 {result.k:.5f}），并恢复原亮度。", SUCCESS)
                    self._refresh_curve_label()
        if self._worker is not None:
            self._submit(work, done)
        else:
            def standalone():
                try:
                    payload, error = work(), None
                except Exception as exc:  # noqa: BLE001
                    payload, error = None, str(exc)
                self._ui_queue.put(lambda: done(payload, error))
            threading.Thread(target=standalone, name="camab-screen-calib", daemon=True).start()

    def _apply_cfg(self) -> None:
        """把新配置灌进正在跑的引擎（不需要重启）。"""
        if self._worker is None:
            self.engine.apply_config(self.cfg)
        else:
            snapshot = self.cfg.snapshot()
            self._submit(lambda: self.engine.apply_config(snapshot),
                         lambda _result, error: self._set_status_text(f"应用设置失败：{error}", ERROR)
                         if error else self._refresh_interval_state())


def run_tray(args) -> int:
    from .config import AppConfig
    from .__main__ import _apply_overrides

    cfg = AppConfig.load(getattr(args, "config", None))
    _apply_overrides(cfg, args)          # 让 --source / --index / --dry-run 生效
    app = TrayApp(cfg, screenshot=getattr(args, "screenshot", None),
                  calib_screenshot=getattr(args, "calib_screenshot", None),
                  settings_screenshot=getattr(args, "settings_screenshot", None),
                  config_path=getattr(args, "config", None))
    return app.start(selftest=bool(getattr(args, "selftest", False)))
