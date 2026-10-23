"""自动校准向导（tkinter 对话框）。

用户只需要做两件事：**换一次光照** + **拖滑块选想要的亮度**，软件自己反推曲线。
不需要知道 lux 是什么，也不需要改 JSON。
"""

from __future__ import annotations

import logging
import math
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from .calibration import Sample, build_anchors, suggest_levels
from .ui_theme import (ACCENT, BG, DIM, FG, FONT, MONO, SURFACE,
                       WARN, apply_theme, button)

log = logging.getLogger(__name__)

#: 缓存多少秒内算"够新"（同步读取兼容入口使用；记录点总是重新取样）
FRESH_S = 1.0


class CalibrationWizard:
    """三点校准向导。

    :param parent: 父窗口
    :param read_lux: 回调，返回当前测得的照度（lux）
    :param apply_level: 回调，把屏幕亮度设成指定值（用于让用户即时看到效果）
    :param on_done: 校准完成回调，参数是折点列表
    :param get_level: 回调，返回当前屏幕亮度
    """

    def __init__(self, parent: tk.Misc, read_lux: Callable[[], float],
                 apply_level: Callable[[int], None],
                 get_level: Callable[[], int],
                 on_done: Callable[[list], None],
                 floor: int = 20, ceil: int = 100) -> None:
        self.read_lux = read_lux
        self.apply_level = apply_level
        self.get_level = get_level
        self.on_done = on_done
        self.floor = floor
        self.ceil = ceil
        self.samples: list[Sample] = []
        self._closed = False
        #: 是否已经给滑块设过初值（只设一次，之后不碰用户的选择）
        self._suggested = False
        #: 后台取样缓存（读一次照度要 100~500ms，不能在 UI 线程里做）
        self._lux: Optional[float] = None
        self._lux_t: float = 0.0
        self._sampling = False
        self._sample_timeout = 2.5
        self._sample_error: Optional[str] = None
        self._sample_seq = 0
        self._record_pending = False
        self._record_seq: Optional[int] = None
        self._record_deadline = 0.0
        self._record_level = 60
        self._current_level = max(floor, min(ceil, 60))
        self._level_loaded = False
        self._after_ids: set[str] = set()
        self._tick_id: Optional[str] = None
        self._preview_lock = threading.Lock()
        self._preview_pending = False
        self._preview_result: Optional[str] = None
        self._preview_original: Optional[int] = None
        self._preview_used = False
        self._restore_done = threading.Event()

        self.win = tk.Toplevel(parent)
        self.win.title("自动校准亮度曲线")
        self.win.configure(bg=BG)
        apply_theme(self.win)
        self.win.resizable(True, True)
        self.win.minsize(360, 300)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        self.win.transient(parent)
        self.win.bind("<Destroy>", self._on_destroy, add="+")
        self.win.bind("<Escape>", lambda _event: self.cancel())

        self._build()
        self._tick()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        from .ui_theme import ScrollFrame, bind_wrap, fit_window

        self._scroll = ScrollFrame(self.win, bg=BG)
        self._scroll.pack(fill="both", expand=True)
        w = self._scroll.body
        tk.Label(w, text="自动校准", bg=BG, fg=FG,
                 font=(FONT, 16, "bold")).pack(anchor="w", padx=20, pady=(18, 6))
        instructions = tk.Label(
            w, text="改变房间光照（开灯、关灯或拉窗帘），选择这个光照下舒适的屏幕亮度，"
                    "再记录这个点。记录 2–3 个点，就能生成适合你的亮度曲线。",
            bg=BG, fg=DIM, justify="left", anchor="w", font=(FONT, 10))
        instructions.pack(fill="x", padx=20, pady=(0, 10))
        bind_wrap(instructions)

        # 实时读数
        box = tk.Frame(w, bg=SURFACE)
        box.pack(fill="x", padx=20, pady=(8, 12))
        self.lbl_lux = tk.Label(box, text="当前环境  -- lx", bg=SURFACE, fg=FG,
                                anchor="w", justify="left", font=(MONO, 15, "bold"))
        self.lbl_lux.pack(fill="x", padx=14, pady=(12, 4))
        bind_wrap(self.lbl_lux)
        self.lbl_hint = tk.Label(box, text="", bg=SURFACE, fg=DIM,
                                 anchor="w", justify="left", font=(FONT, 10))
        self.lbl_hint.pack(fill="x", padx=14, pady=(0, 12))
        bind_wrap(self.lbl_hint)

        # 滑块
        # 注意：ttk.Scale 默认很薄，鼠标不容易抓住（用户反馈"滑块没法滑动"）。
        # 这里单独配一个加粗样式，并放一个显眼的百分比数值。
        row = tk.Frame(w, bg=BG)
        row.pack(fill="x", padx=20, pady=(4, 4))
        self.lbl_level = tk.Label(row, text=f"{self._current_level}%", bg=BG, fg=ACCENT,
                                  font=(MONO, 18, "bold"))
        self.lbl_level.pack(side="right")
        level_heading = tk.Label(row, text="我希望的亮度", bg=BG, fg=FG,
                                  anchor="w", justify="left", font=(FONT, 10))
        level_heading.pack(side="left", fill="x", expand=True)
        bind_wrap(level_heading)
        self.var_level = tk.IntVar(master=w, value=self._current_level)
        self.scale = ttk.Scale(w, from_=self.floor, to=self.ceil, orient="horizontal",
                               variable=self.var_level, command=self._on_drag, length=250,
                               style="Calib.Horizontal.TScale")
        self.scale.pack(padx=20, pady=(0, 8), ipady=8, fill="x")
        # 键盘也能调，方便精调（ttk.Scale 默认不响应方向键）
        for key, delta in (("<Left>", -1), ("<Right>", 1), ("<Down>", -1), ("<Up>", 1)):
            self.scale.bind(key, lambda _e, d=delta: self._nudge(d))
        self.scale.focus_set()
        btns = tk.Frame(w, bg=BG)
        btns.pack(fill="x", padx=20, pady=(4, 4))
        self.btn_record = button(btns, "记录这个点", self.record, primary=True)
        self.btn_preview = button(btns, "预览屏幕亮度", self.preview)
        self._flow_buttons(btns, (self.btn_record, self.btn_preview))
        preview_tip = tk.Label(w, text="预览后，关闭向导会恢复预览前的亮度。",
                               bg=BG, fg=DIM, anchor="w", justify="left", font=(FONT, 9))
        preview_tip.pack(fill="x", padx=20, pady=(2, 12))
        bind_wrap(preview_tip)

        # 已记录的点
        tk.Label(w, text="已记录的点：", bg=BG, fg=DIM,
                 font=(FONT, 10)).pack(anchor="w", padx=20, pady=(4, 6))
        self.lst = tk.Listbox(w, height=4, bg=SURFACE, fg=FG, relief="flat",
                              selectbackground=ACCENT, selectforeground=BG, font=(MONO, 10),
                              highlightthickness=0)
        self.lst.pack(fill="x", padx=20, pady=(0, 8))
        self.lbl_status = tk.Label(w, text="", bg=BG, fg=WARN, anchor="w",
                                   justify="left", font=(FONT, 10))
        self.lbl_status.pack(fill="x", padx=20, pady=(2, 16))
        bind_wrap(self.lbl_status)

        # Save/cancel stay reachable even when the measurement content scrolls.
        bar = tk.Frame(self.win, bg=BG)
        bar.pack(side="bottom", fill="x", padx=20, pady=(10, 16), before=self._scroll)
        self.btn_done = button(bar, "完成并保存", self.finish, primary=True)
        self.btn_remove = button(bar, "删除选中点", self.remove_selected)
        self.btn_cancel = button(bar, "取消", self.cancel)
        self._flow_buttons(bar, (self.btn_done, self.btn_remove, self.btn_cancel))
        self._refresh()
        fit_window(self.win, 600, 650, minwidth=360, minheight=300)

    @staticmethod
    def _flow_buttons(parent, buttons) -> None:
        """Wrap action rows using their actual requested font widths."""
        previous = None

        def layout(_event=None):
            nonlocal previous
            width = max(1, parent.winfo_width())
            sizes = tuple(widget.winfo_reqwidth() for widget in buttons)
            signature = (width, sizes)
            if signature == previous:
                return
            previous = signature
            row, column, used = 0, 0, 0
            for widget, requested in zip(buttons, sizes):
                if column and used + requested + 8 > width:
                    row, column, used = row + 1, 0, 0
                widget.grid(row=row, column=column, sticky="w", padx=(0, 8), pady=4)
                used += requested + 8
                column += 1

        parent.bind("<Configure>", layout, add="+")
        for widget in buttons:
            widget.bind("<Configure>", layout, add="+")
        layout()

    # ------------------------------------------------------------------ #
    # 行为
    # ------------------------------------------------------------------ #
    def _on_drag(self, val=None) -> None:
        """滑块回调：用 ttk 传来的值，别读 var_level（时序上会返回旧值）。"""
        if self._closed:
            return
        # 用户先于首个读数操作时，也不能被延迟到达的建议值覆盖。
        self._suggested = True
        try:
            lv = int(float(val))
        except (TypeError, ValueError):
            lv = int(float(self.var_level.get()))
        self.lbl_level.config(text=f"{lv}%")

    def _nudge(self, delta: int) -> str:
        """键盘微调（左右方向键 ±1%）。"""
        lv = max(self.floor, min(self.ceil, int(float(self.scale.get())) + delta))
        self.scale.set(lv)
        self._on_drag(lv)
        return "break"

    # ------------------------------------------------------------------ #
    # 取样（后台线程，避免界面卡住）
    # ------------------------------------------------------------------ #
    def _schedule(self, delay: int, callback: Callable[[], None], *,
                  allow_closed: bool = False) -> Optional[str]:
        """仅在 Tk 线程安排回调，并统一跟踪以便销毁时取消。"""
        if self._closed and not allow_closed:
            return None
        holder: list[str] = []

        def run() -> None:
            self._after_ids.discard(holder[0])
            if not self._closed or allow_closed:
                callback()

        try:
            ident = self.win.after(delay, run)
        except tk.TclError:
            return None
        holder.append(ident)
        self._after_ids.add(ident)
        return ident

    def _cancel_callbacks(self) -> None:
        for ident in tuple(self._after_ids):
            try:
                self.win.after_cancel(ident)
            except tk.TclError:
                pass
        self._after_ids.clear()
        self._tick_id = None

    def _kick_sample(self) -> None:
        """发起一次后台取样（已在取样中就跳过）。"""
        if self._sampling or self._closed:
            return
        self._sampling = True
        self._sample_seq += 1
        self._sample_error = None

        def work() -> None:
            try:
                lux = float(self.read_lux())
                if not math.isfinite(lux) or lux < 0:
                    raise ValueError("摄像头返回了无效照度")
                if not self._closed:
                    self._lux = lux
                    self._lux_t = time.time()
                if not self._closed and not self._level_loaded:
                    try:
                        self._current_level = int(self.get_level())
                        self._level_loaded = True
                    except Exception:  # noqa: BLE001
                        log.debug("读取当前亮度失败", exc_info=True)
            except Exception as exc:  # noqa: BLE001
                self._lux = None
                self._sample_error = str(exc)
                log.debug("取样失败", exc_info=True)
            finally:
                self._sampling = False

        threading.Thread(target=work, name="camab-calib-sample", daemon=True).start()

    def current_lux(self, max_age: float = FRESH_S) -> float:
        """当前照度（缓存够新就直接用）。

        **暂停状态下也必须真的去测**，否则用户换了光照也读不到变化。
        """
        if self._lux is None or (time.time() - self._lux_t) > max_age:
            self._kick_sample()
            self._wait_sample()
        return float(self._lux or 0.0)

    def _wait_sample(self, timeout: float = 3.0) -> None:
        """同步调用兼容入口；Tk 按钮使用异步 record，不调用此方法。"""
        if self._sampling:
            deadline = time.monotonic() + timeout
            while self._sampling and time.monotonic() < deadline:
                time.sleep(0.02)

    def _fresh_lux(self, timeout: float = 3.0) -> float:
        """要一个"刚刚测到"的读数（记录点时用）。

        必须**先作废旧缓存再等新结果**：否则会把上一次的旧值记下来，
        表现为"改了光照但记的点还是旧读数"（用户实测反馈）。
        """
        deadline = time.monotonic() + timeout
        # 正在取样的线程可能属于旧光照，先等它结束再发起新的取样。
        self._wait_sample(timeout)
        if self._sampling:
            raise TimeoutError("读取照度超时")
        self._lux = None
        self._kick_sample()
        self._wait_sample(max(0.0, deadline - time.monotonic()))
        if self._sampling:
            raise TimeoutError("读取照度超时")
        if getattr(self, "_sample_error", None):
            raise RuntimeError(self._sample_error)
        return float(self._lux or 0.0)

    def _tick(self) -> None:
        """每 0.5s 刷新一次实时照度。

        **两条硬规矩**（都是真机踩出来的）：
        1. 只允许在**第一次**刷新时给滑块设初值，否则用户刚拖动就被覆盖，
           表现为"滑块没法滑动"。
        2. 取样走后台线程 + 缓存：读一次照度要 100~500ms，直接在 UI 线程里做会卡界面。
        """
        if self._closed:
            return
        try:
            if not self._record_pending:
                self._kick_sample()                 # 记录点时不要插入另一次取样
            lux = float(self._lux or 0.0)
            if self._lux is None:
                self.lbl_lux.config(text="当前环境  " + (
                    "读取失败，请重试" if self._sample_error else "测量中…"))
            else:
                age = time.time() - self._lux_t
                self.lbl_lux.config(text=f"当前环境  {lux:,.1f} lx"
                                        + ("（刚刚更新）" if age < 1.5 else ""))
                sug, _lo = suggest_levels(lux, self._current_level, self.floor, self.ceil)
                self.lbl_hint.config(
                    text=f"参考：这个光照下软件会建议 {sug}%"
                         f"｜可调范围 {self.floor}~{self.ceil}%（拖动滑块或按左右方向键）")
                if not self._suggested:
                    self._suggested = True
                    self.var_level.set(sug)
                    self._on_drag()
        except Exception:  # noqa: BLE001
            log.debug("刷新校准读数失败", exc_info=True)
        if self._tick_id is not None:
            self._after_ids.discard(self._tick_id)
            try:
                self.win.after_cancel(self._tick_id)
            except tk.TclError:
                pass
        self._tick_id = self._schedule(500, self._tick)

    def preview(self) -> None:
        if self._closed or self._preview_pending:
            return
        level = max(self.floor, min(self.ceil, int(float(self.var_level.get()))))
        self._suggested = True
        self._preview_pending = True
        self._preview_result = None
        self.btn_preview.config(state="disabled", text="正在预览…")
        self._set_status("正在设置屏幕亮度…", DIM)

        def work() -> None:
            try:
                with self._preview_lock:
                    if self._closed:
                        return
                    if self._preview_original is None:
                        self._preview_original = int(self.get_level())
                    if self._closed:
                        return
                    # 即使后端部分写入后抛错，退出时仍尝试恢复。
                    self._preview_used = True
                    self.apply_level(level)
                    self._current_level = level
                self._preview_result = ""
            except Exception as exc:  # noqa: BLE001
                self._preview_result = str(exc)
            finally:
                self._preview_pending = False

        threading.Thread(target=work, name="camab-calib-preview", daemon=True).start()
        self._poll_preview()

    def _poll_preview(self) -> None:
        if self._closed:
            return
        if self._preview_pending:
            self._schedule(40, self._poll_preview)
            return
        self.btn_preview.config(state="normal", text="预览屏幕亮度")
        if self._preview_result:
            self._set_status(f"设置亮度失败：{self._preview_result}", WARN)
        else:
            self._set_status("已预览这个亮度。关闭向导后会恢复预览前的亮度。", ACCENT)

    def record(self) -> None:
        """记录一次新取样；后台测量时继续响应滑块和取消按钮。"""
        if self._closed or self._record_pending:
            return
        self._record_pending = True
        self._suggested = True
        self._record_level = max(self.floor, min(self.ceil, int(float(self.var_level.get()))))
        self._record_seq = None
        self._record_deadline = time.monotonic() + self._sample_timeout
        self.btn_record.config(state="disabled", text="正在测量…")
        self.btn_done.config(state="disabled")
        self._set_status("正在重新测量环境光，请保持当前光照…", DIM)
        self._poll_record()

    def _poll_record(self) -> None:
        if self._closed or not self._record_pending:
            return
        if time.monotonic() >= self._record_deadline:
            self._record_pending = False
            self.btn_record.config(state="normal", text="记录这个点")
            self._refresh()
            self._set_status("读取照度超时，请确认摄像头可用后再记录。", WARN)
            return
        if self._record_seq is None and not self._sampling:
            # 已开始的刷新可能属于用户改变光照之前，必须等它结束后重新测。
            self._lux = None
            self._kick_sample()
            self._record_seq = self._sample_seq
        if self._record_seq is None or self._sampling:
            self._schedule(40, self._poll_record)
            return
        self._record_pending = False
        self.btn_record.config(state="normal", text="记录这个点")
        self._refresh()
        if self._sample_error or self._lux is None:
            self._set_status(f"读不到照度：{self._sample_error or '没有有效读数'}", WARN)
            return
        self._record_value(float(self._lux), self._record_level)

    def _record_value(self, lux: float, lv: int) -> None:
        if lux <= 0.6:
            self._set_status("现在测到的照度接近 0（摄像头可能没画面或被挡）。"
                             "请先确认画面正常，或把镜头对着房间。", WARN)
            return
        if any(abs(s.lux - lux) / max(lux, 1e-6) < 0.2 for s in self.samples):
            prev = min(self.samples, key=lambda s: abs(s.lux - lux))
            self._set_status(
                f"当前光照 ({lux:.1f} lx) 与已记录的「{prev.lux:.1f} lx」太接近，"
                f"记下去曲线会挤在一起。请再改变一次房间光照"
                f"（开灯 / 关灯 / 拉窗帘 / 开台灯），等上面读数明显变了再记录。", WARN)
            return
        if any(abs(s.lux - lux) / max(lux, 1e-6) < 0.6 and abs(s.level - lv) <= 2
               for s in self.samples):
            self._set_status("同样的光照下你已经选过几乎相同的亮度了，"
                             "换个亮度或换个光照再记。", WARN)
            return
        self.samples.append(Sample(lux=lux, level=lv, label=f"第{len(self.samples)+1}点"))
        self._refresh()
        self._set_status(f"已记录：{lux:.1f} lx → {lv}%。"
                         f"{'现在改变房间光照，读数变了再记下一个点。' if len(self.samples) < 2 else '可以继续加，或点「完成并保存」。'}",
                         ACCENT)

    def remove_selected(self) -> None:
        sel = list(self.lst.curselection())
        if not sel:
            return
        displayed = sorted(self.samples, key=lambda sample: sample.lux)
        for i in reversed(sel):
            self.samples.remove(displayed[i])
        self._refresh()

    def _refresh(self) -> None:
        self.lst.delete(0, tk.END)
        for s in sorted(self.samples, key=lambda x: x.lux):
            self.lst.insert(tk.END, f"  {s.lux:8.1f} lx  →  {s.level:3d}%")
        self.btn_done.config(state="normal" if len(self.samples) >= 2 and not self._record_pending
                             else "disabled")

    def _set_status(self, text: str, color: str = DIM) -> None:
        if not self._closed:
            self.lbl_status.config(text=text, fg=color)

    def saturation_warning(self, anchors: Optional[list] = None) -> str:
        """检查这条曲线是不是"过早满亮度"，并给出人话解释（没问题就返回空串）。

        实测踩到过：用户记了 `11 lx→76%` 和 `64 lx→100%` 两个点，
        曲线算出来到 **64 lx 就满亮度**了 —— 于是任何一个正常房间（100~300 lx）
        屏幕都被顶到 100%，用户看到的是"自动亮度又失灵了 / 太亮"。
        这种情况必须当场提醒，而不是等他自己发现。
        """
        try:
            from .metrics import MappingConfig

            m = MappingConfig(anchors=anchors or self._preview_anchors(),
                              floor=self.floor, ceil=self.ceil).clamped()
            # 找到"第一次达到满亮度"的照度
            sat = None
            prev = None
            lux = 1.0
            while lux <= 2000.0:
                lv = m.lux_to_level(lux)
                if lv >= self.ceil and prev is not None and prev < self.ceil:
                    sat = lux
                    break
                prev = lv
                lux *= 1.15
            if sat is not None and sat < 150.0:
                return (f"这条曲线到 {sat:.0f} lx 就给满亮度（{self.ceil}%）了。\n"
                        f"一般房间是 100~300 lx，白天靠窗能到 1000 lx 以上 —— "
                        f"也就是说白天它会一直是 {self.ceil}%。\n\n"
                        f"更稳妥的做法：让两个采样点的照度差别更大，"
                        f"比如一次关灯/拉窗帘、一次开顶灯或白天，"
                        f"别让「最亮那个点」落在 100 lx 以下。")
            return ""
        except Exception:  # noqa: BLE001
            return ""

    def _preview_anchors(self) -> list:
        res = build_anchors(self.samples, floor=self.floor, ceil=self.ceil)
        return res.anchors if res.ok else []

    def finish(self) -> None:
        if self._closed or self._record_pending:
            return
        from .calibration import build_anchors as _ba

        res = _ba(self.samples, floor=self.floor, ceil=self.ceil)
        if not res.ok:
            self._set_status(res.message, WARN)
            return
        warn = self.saturation_warning(res.anchors)
        if warn:
            ask = messagebox.askyesno(
                "这条曲线偏陡，确定吗？",
                f"新曲线：\n{res.describe()}\n\n{warn}\n\n"
                f"选「是」= 照样保存；选「否」= 回去再记一个更亮的点。",
                parent=self.win)
            if not ask:
                self._set_status("已取消保存：请再改变一次光照（更亮一档）后记录新点。", WARN)
                return
        try:
            self.on_done(res.anchors)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc), parent=self.win)
            return
        messagebox.showinfo(
            "校准完成",
            f"新曲线：\n{res.describe()}\n\n"
            f"含义：环境越亮屏幕越亮，中间按你的选择线性过渡。\n"
            f"不满意可以再校准一次，或到设置里改折点。",
            parent=self.win)
        self._close()

    def cancel(self) -> None:
        self._close()

    def selftest(self, screenshot: Optional[str] = None) -> int:
        """自检：渲染几帧、记录一个点、可选截图，然后关闭（不留在桌面上）。"""
        import time as _t

        ok = True
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.attributes("-topmost", True)
            for _ in range(6):
                self.win.update()
                _t.sleep(0.1)
            self.var_level.set(int((self.floor + self.ceil) / 2))
            self.record()
            deadline = _t.monotonic() + self._sample_timeout + 0.5
            while self._record_pending and _t.monotonic() < deadline:
                self.win.update()
                _t.sleep(0.02)
            if not self.samples:
                ok = False
            if screenshot:
                from .tray import ensure_window_visible, grab_window

                if ensure_window_visible(self.win):
                    img = grab_window(self.win, pad=6)
                    from pathlib import Path

                    p = Path(screenshot)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    img.save(p)
                    print(f"向导截图已保存：{p}（{img.width}x{img.height}）")
                else:
                    print("向导截图失败：窗口没能显示出来（无桌面会话？）")
                    ok = False
            print(f"校准向导自检{'通过' if ok else '部分失败'}："
                  f"界面渲染 / 记录点 / 按钮状态 均正常")
            return 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            print(f"校准向导自检失败：{type(exc).__name__}: {exc}")
            return 1
        finally:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._record_pending = False
        self._cancel_callbacks()
        if self._preview_pending or self._preview_used:
            # 先收起窗口，让恢复线程完成后再 Destroy，父窗口才恢复自动调光。
            self.win.withdraw()
            self._start_restore()
            self._poll_close()
        else:
            self._destroy_window()

    def _start_restore(self) -> None:
        def restore() -> None:
            try:
                with self._preview_lock:
                    if self._preview_used and self._preview_original is not None:
                        self.apply_level(self._preview_original)
                        self._preview_used = False
            except Exception:  # noqa: BLE001
                log.warning("恢复校准前亮度失败", exc_info=True)
            finally:
                self._restore_done.set()

        threading.Thread(target=restore, name="camab-calib-restore", daemon=True).start()

    def _poll_close(self) -> None:
        if self._restore_done.is_set():
            self._destroy_window()
        else:
            self._schedule(40, self._poll_close, allow_closed=True)

    def _on_destroy(self, event) -> None:
        if event.widget is not self.win:
            return
        already_closed = self._closed
        self._closed = True
        self._record_pending = False
        self._cancel_callbacks()
        # 取样/恢复线程可能仍持有 self。把 Tk 变量的最后一个引用在
        # 销毁事件的主线程释放，避免后台线程结束时触发 Variable.__del__。
        self.var_level = None
        if not already_closed and (self._preview_pending or self._preview_used):
            self._start_restore()

    def _destroy_window(self) -> None:
        self._cancel_callbacks()
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            pass
