"""Tabbed settings with a validated draft and transactional save."""

import math
import tkinter as tk
from tkinter import ttk

from .metrics import INTERVAL_FAST, INTERVAL_SLOW, speed_from_interval
from .platform_support import BRIGHTNESS_BACKENDS, brightness_help
from .ui_theme import (ACCENT, BG, DIM, ERROR, FG, FONT, SURFACE,
                       ScrollFrame, bind_wrap, button, entry, fit_window, pixels,
                       update_widget)


def flow_buttons(parent, widgets):
    """Let action rows wrap using the actual font-scaled button widths."""
    def layout(event=None):
        width = event.width if event is not None else parent.winfo_width()
        gap = pixels(parent, 8)
        row = col = used = 0
        for widget in widgets:
            needed = widget.winfo_reqwidth() + gap
            if col and used + needed > width:
                row, col, used = row + 1, 0, 0
            widget.grid(row=row, column=col, sticky="w", padx=(0, gap), pady=(0, gap))
            used += needed
            col += 1
    parent.bind("<Configure>", layout, add="+")
    parent.after_idle(layout)


class FieldError(ValueError):
    def __init__(self, key, message):
        self.key = key
        super().__init__(message)


def number(raw, key, label, low, high, *, integer=False):
    try:
        value = float(str(raw).strip())
    except (ValueError, TypeError):
        raise FieldError(key, f"{label}需要填写数字。") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise FieldError(key, f"{label}范围是 {low:g}–{high:g}。")
    if integer and not value.is_integer():
        raise FieldError(key, f"{label}需要填写整数。")
    return int(value) if integer else value


def parse_anchors(raw):
    points = []
    chunks = str(raw).replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    for chunk in chunks:
        if not chunk.strip():
            continue
        pair = chunk.replace("→", ":").replace("：", ":").split(":")
        if len(pair) != 2:
            raise FieldError("__anchors__", "曲线格式：照度:亮度，例如 2:20, 60:60, 300:90。")
        lux = number(pair[0], "__anchors__", "折点照度", 0.01, 1e7)
        level = number(pair[1], "__anchors__", "折点亮度", 0, 100, integer=True)
        points.append((lux, level))
    points.sort()
    if len(points) < 2 or any(a[0] == b[0] for a, b in zip(points, points[1:])):
        raise FieldError("__anchors__", "至少填写两个不同照度的折点。")
    if any(a[1] > b[1] for a, b in zip(points, points[1:])):
        raise FieldError("__anchors__", "光线变亮时，曲线亮度应保持或增加。")
    if any(b[0] < a[0]*1.15 for a, b in zip(points, points[1:])):
        raise FieldError("__anchors__", "相邻折点的照度至少相差 15%，避免保存时合并。")
    return points


# key, label, minimum, maximum, integer, explanation
FIELDS = (
    ("mapping.floor", "最低亮度 %", 0, 99, True, "暗环境下的最低自动亮度"),
    ("mapping.ceil", "最高亮度 %", 1, 100, True, "亮环境下的最高自动亮度"),
    ("engine.manual_hold_s", "手动接管秒数", 0, 3600, False, "手动调节后暂停多久；0 表示立即自动接管"),
    ("mapping.deadband", "防抖范围 %", 0, 30, True, "小于这个差值时保持亮度"),
    ("mapping.cooldown_s", "调整冷却秒数", 0, 60, False, "两次自动调整的最小间隔"),
    ("mapping.max_step", "自动跟随幅度 %", 1, 100, True, "一次跟随的范围；会分成小步柔和过渡"),
    ("analysis.percentile", "测光分位 %", 0, 100, False, "镜头朝向人或屏幕时，建议 10–25"),
)


def settings_draft(cfg, values, interval, dry_run, avoid_screen):
    """All validation precedes mutation, persistence, and engine updates."""
    draft = cfg.snapshot()
    for key, label, low, high, integer, _tip in FIELDS:
        section, name = key.split(".")
        value = number(values[key], key, label, low, high, integer=integer)
        setattr(getattr(draft, section), name, value)
    if draft.mapping.floor >= draft.mapping.ceil:
        raise FieldError("mapping.ceil", "最高亮度必须高于最低亮度。")
    draft.mapping.anchors = parse_anchors(values["__anchors__"])
    draft.engine.interval_s = number(interval, "interval", "采样周期", INTERVAL_FAST, INTERVAL_SLOW)
    draft.engine.dry_run = bool(dry_run)
    draft.avoid_screen = bool(avoid_screen)
    draft.camera.index = number(values.get('camera.index', cfg.camera.index), 'camera.index', '摄像头序号', 0, 9, integer=True)
    draft.camera.source = str(values.get('camera.source', cfg.camera.source)).strip()
    backend = str(values.get('engine.backend', cfg.engine.backend)).strip()
    if backend not in BRIGHTNESS_BACKENDS:
        raise FieldError('engine.backend', '请选择一个有效的亮度接口。')
    draft.engine.backend = backend
    draft.engine.monitor = str(values.get('engine.monitor', cfg.engine.monitor)).strip()
    return draft.clamped()


class SettingsPanel:
    def __init__(self, app):
        self.app = app
        self.win = s = tk.Toplevel(app._win)
        app._settings = s
        s.title("设置与校准 · 摄像头自动亮度")
        s.configure(bg=BG)
        s.resizable(True, True)
        s.protocol("WM_DELETE_WINDOW", s.destroy)
        s.bind("<Escape>", lambda _e: s.destroy())
        pad = pixels(s, 20)
        self._pad = pad
        tk.Label(s, text="设置与校准", bg=BG, fg=FG,
                 font=(FONT, 16, "bold")).pack(anchor="w", padx=pad, pady=(pixels(s, 18), 4))
        self.tabs = ttk.Notebook(s, style="Settings.TNotebook")
        self.tabs.enable_traversal()
        def tab_layout(event):
            compact = event.width < pixels(s, 520)
            ttk.Style(s).configure("Settings.TNotebook.Tab",
                                   font=(FONT, 9 if compact else 10),
                                   padding=(pixels(s, 2 if compact else 12), pixels(s, 7)))
        self.tabs.bind("<Configure>", tab_layout, add="+")
        frames = [tk.Frame(self.tabs, bg=BG) for _ in range(3)]
        for frame, label in zip(frames, ("日常", "校准", "高级")):
            self.tabs.add(frame, text=label)
        self.scrolls = [ScrollFrame(frame) for frame in frames]
        for scroll in self.scrolls:
            scroll.pack(fill="both", expand=True)
            scroll.body.configure(padx=pixels(s, 12), pady=pixels(s, 12))
        general, calib, advanced = [scroll.body for scroll in self.scrolls]
        bind_wrap(tk.Label(general, text="采样速度立即生效；其余参数点击保存后生效。",
                 bg=BG, fg=DIM, font=(FONT, 10))).pack(fill="x", pady=(0, pixels(s, 12)))
        self.vars = {}
        self.entries = {}
        self.pages = {}
        self.errors = {}
        self._sampling(general)
        self._fields(general, FIELDS[:3])
        self._calibration(calib)
        self._devices(advanced)
        self._fields(advanced, FIELDS[3:])
        self._anchors(advanced)
        self.var_dry = tk.BooleanVar(master=s, value=app.cfg.engine.dry_run)
        self.var_avoid = tk.BooleanVar(master=s, value=app.cfg.avoid_screen)
        for var, label in ((self.var_avoid, "减少画面中人脸与屏幕反光的影响"),
                           (self.var_dry, "仅模拟调光（记录结果，不调整屏幕）")):
            bind_wrap(tk.Checkbutton(advanced, text=label, variable=var, bg=BG, fg=FG,
                           selectcolor=SURFACE, activebackground=BG, activeforeground=FG,
                           font=(FONT, 10))).pack(fill="x", pady=4)
        self.error = bind_wrap(tk.Label(s, text="", bg=BG, fg=ERROR,
                                       font=(FONT, 10)))
        bar = tk.Frame(s, bg=BG)
        bar.pack(side="bottom", fill="x", padx=pixels(s, 12), pady=(pixels(s, 8), pixels(s, 12)))
        self.action_buttons = [button(bar, "保存", self.save, primary=True, padx=pixels(s, 8)),
                               button(bar, "撤销", self.reset, padx=pixels(s, 8)),
                               button(bar, "取消", s.destroy, padx=pixels(s, 8))]
        flow_buttons(bar, self.action_buttons)
        self.tabs.pack(fill="both", expand=True, padx=pad)
        self.baseline = {key: var.get() for key, var in self.vars.items()}
        self._dry_baseline = self.var_dry.get()
        self._avoid_baseline = self.var_avoid.get()
        fit_window(s, pixels(s, 680), pixels(s, 650), minwidth=440, minheight=420)
        modifier = 'Command' if s.tk.call('tk', 'windowingsystem') == 'aqua' else 'Control'
        s.bind(f'<{modifier}-Return>', lambda _e: self.save())

    def _devices(self, parent):
        bind_wrap(tk.Label(parent, text='设备连接', bg=BG, fg=FG,
                           font=(FONT, 12, 'bold'))).pack(fill='x', pady=(0, 6))
        bind_wrap(tk.Label(parent, text='更换设备后会重新连接。' + brightness_help(),
                           bg=BG, fg=DIM, font=(FONT, 9))).pack(fill='x', pady=(0, 8))
        specs = (
            ('engine.backend', '亮度接口', 'auto 自动选择；dryrun 只模拟，不改变屏幕。'),
            ('engine.monitor', '显示器标识（可选）', '留空使用默认显示器；可从设备详情或自检中复制标识。'),
            ('camera.index', '摄像头序号', '默认 0；画面不正确时，可尝试 1、2 等其他摄像头。'),
            ('camera.source', '视频源（可选）', '留空使用摄像头；也可填 /dev/video0、视频文件路径或视频流地址。'),
        )
        for key, title, tip in specs:
            section, name = key.split('.')
            var = self.vars[key] = tk.StringVar(master=self.win, value=str(getattr(getattr(self.app.cfg, section), name)))
            row = tk.Frame(parent, bg=BG)
            row.pack(fill='x', pady=(8, 0))
            row.columnconfigure(0, weight=1)
            bind_wrap(tk.Label(row, text=title, bg=BG, fg=FG, font=(FONT, 10))).grid(row=0, column=0, sticky='ew')
            if key == 'engine.backend':
                control = ttk.Combobox(row, textvariable=var, values=BRIGHTNESS_BACKENDS, state='readonly', font=(FONT, 10))
            else:
                control = entry(row, var, 16)
            self.entries[key] = control
            control.grid(row=1, column=0, sticky='ew', pady=4, ipady=3)
            bind_wrap(tk.Label(row, text=tip, bg=BG, fg=DIM, font=(FONT, 9))).grid(row=2, column=0, sticky='ew')
            error = self.errors[key] = bind_wrap(tk.Label(row, text='', bg=BG, fg=ERROR, font=(FONT, 9)))
            error.grid(row=3, column=0, sticky='ew')
            error.grid_remove()
            self.pages[key] = parent

    def sync_config(self):
        """Refresh unchanged draft fields after a preset / calibration save."""
        if not self.win.winfo_exists() or not hasattr(self, "baseline"):
            return
        for key, var in self.vars.items():
            if key == "__anchors__":
                value = ", ".join(f"{lux:g}:{lv}" for lux, lv in self.app.cfg.mapping.curve_points())
            else:
                section, name = key.split(".")
                value = str(getattr(getattr(self.app.cfg, section), name))
            if var.get() == self.baseline[key]:
                var.set(value)
                self.baseline[key] = value
        for var, baseline, value in (
            (self.var_dry, "_dry_baseline", self.app.cfg.engine.dry_run),
            (self.var_avoid, "_avoid_baseline", self.app.cfg.avoid_screen),
        ):
            if var.get() == getattr(self, baseline):
                var.set(value)
                setattr(self, baseline, value)
        self.draw_curve()

    def _sampling(self, parent):
        app = self.app
        tk.Label(parent, text="采样速度", bg=BG, fg=FG,
                 font=(FONT, 11, "bold")).pack(anchor="w")
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(6, 2))
        app.var_sample_speed = tk.IntVar(master=self.win, value=speed_from_interval(app.cfg.engine.interval_s))
        app.scale_sample = ttk.Scale(row, from_=0, to=100, orient="horizontal",
                                     variable=app.var_sample_speed, command=app._on_sample_speed_drag,
                                     style="Set.Horizontal.TScale")
        app.lbl_sample = bind_wrap(tk.Label(row, text="", bg=BG, fg=ACCENT, font=(FONT, 10)))
        app.lbl_sample.pack(fill="x")
        app.scale_sample.pack(fill="x", ipady=8)
        app.scale_sample.bind("<ButtonRelease-1>", lambda _e: app._apply_sample_speed())
        for key, delta in (("Left", -1), ("Right", 1)):
            app.scale_sample.bind(f"<{key}>", lambda _e, d=delta: app._nudge_interval(d))
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=6)
        tk.Label(row, text="精确周期（秒）", bg=BG, fg=DIM, font=(FONT, 10)).pack(anchor="w")
        app.var_interval = tk.StringVar(master=self.win, value=f"{app.cfg.engine.interval_s:g}")
        app.ent_interval = entry(row, app.var_interval, 7)
        app.ent_interval.pack(fill="x", pady=4, ipady=4)
        app.ent_interval.bind("<Return>", lambda _e: app._apply_custom_interval())
        app.ent_interval.bind("<FocusOut>", lambda _e: app._apply_custom_interval())
        bind_wrap(tk.Label(row, text="每次间隔 0.5–10 秒 · 滑块右侧采样更快", bg=BG, fg=DIM,
                 font=(FONT, 9))).pack(fill="x")
        app.lbl_interval_state = bind_wrap(tk.Label(parent, text="", bg=BG, fg=DIM,
                                          font=(FONT, 9)))
        app.lbl_interval_state.pack(fill="x", pady=(0, 14))
        app._refresh_sample_label()
        app._refresh_interval_state()
        self.entries["interval"] = app.ent_interval
        self.pages["interval"] = parent

    def _scrollable(self, parent):
        scroll = ScrollFrame(parent)
        scroll.pack(fill="both", expand=True)
        return scroll.body

    def _fields(self, parent, fields):
        for key, label, _low, _high, _integer, tip in fields:
            grid = tk.Frame(parent, bg=BG)
            grid.pack(fill="x", pady=(pixels(parent, 12), 0))
            grid.columnconfigure(0, weight=1)
            section, name = key.split(".")
            value = getattr(getattr(self.app.cfg, section), name)
            var = self.vars[key] = tk.StringVar(master=self.win, value=str(value))
            title = bind_wrap(tk.Label(grid, text=label, bg=BG, fg=FG, font=(FONT, 10)))
            title.grid(row=0, column=0, sticky="ew")
            e = self.entries[key] = entry(grid, var, 9)
            e.grid(row=0, column=1, sticky="e", ipady=5)
            bind_wrap(tk.Label(grid, text=tip, bg=BG, fg=DIM,
                     font=(FONT, 9))).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
            err = self.errors[key] = bind_wrap(tk.Label(grid, text="", bg=BG, fg=ERROR,
                                             font=(FONT, 9)))
            err.grid(row=3, column=0, columnspan=2, sticky="ew")
            err.grid_remove()
            def layout(event, control=e):
                compact = event.width < pixels(control, 370)
                control.grid_configure(row=1 if compact else 0, column=0 if compact else 1,
                                       columnspan=2 if compact else 1,
                                       sticky="ew" if compact else "e", pady=(4, 0) if compact else 0)
            grid.bind("<Configure>", layout)
            self.pages[key] = parent

    def _anchors(self, parent):
        bind_wrap(tk.Label(parent, text="曲线折点（照度:亮度）", bg=BG, fg=FG,
                 font=(FONT, 10))).pack(fill="x", pady=(12, 4))
        value = ", ".join(f"{lux:g}:{lv}" for lux, lv in self.app.cfg.mapping.curve_points())
        self.vars["__anchors__"] = tk.StringVar(master=self.win, value=value)
        self.entries["__anchors__"] = entry(parent, self.vars["__anchors__"], 50)
        self.entries["__anchors__"].pack(fill="x", ipady=5)
        self.pages["__anchors__"] = parent
        bind_wrap(tk.Label(parent, text="例如 2:20, 60:60, 300:90；不确定时，优先使用校准向导。",
                 bg=BG, fg=DIM, font=(FONT, 9))).pack(fill="x", pady=6)

    def _calibration(self, parent):
        bind_wrap(tk.Label(parent, text="适配你的房间与摄像头", bg=BG, fg=FG,
                 font=(FONT, 12, "bold"))).pack(fill="x")
        bind_wrap(tk.Label(parent, text="改变一次房间光线，再选择你喜欢的屏幕亮度。记录 2–3 个点，就能生成适合自己的亮度曲线。",
                 bg=BG, fg=DIM, font=(FONT, 10))).pack(fill="x", pady=10)
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(2, 16))
        flow_buttons(row, [button(row, "亮度曲线校准", self.app.open_calibration, primary=True),
                           button(row, "屏幕反光标定", self.app.calibrate_screen)])
        self.curve = tk.Canvas(parent, height=pixels(parent, 155), bg=SURFACE, highlightthickness=0)
        self.curve.pack(fill="x")
        self.curve.bind("<Configure>", lambda _e: self.draw_curve())
        self.app.lbl_curve = bind_wrap(tk.Label(parent, text="", bg=BG, fg=DIM, font=(FONT, 10)))
        self.app.lbl_curve.pack(fill="x", pady=12)
        self.app._refresh_curve_label()

    def draw_curve(self):
        if not self.curve.winfo_exists():
            return
        c = self.curve
        c.delete("all")
        width = max(200, c.winfo_width())
        points = self.app.cfg.mapping.curve_points()
        xmax = max(1, points[-1][0])
        height = max(pixels(c, 100), c.winfo_height())
        left, right = pixels(c, 28), width-pixels(c, 20)
        top, bottom = pixels(c, 30), height-pixels(c, 30)
        c.create_line(left, bottom, right, bottom, fill=DIM)
        coords = []
        for lux, level in points:
            x, y = left+(right-left)*lux/xmax, bottom-(bottom-top)*level/100
            coords.extend((x, y))
            radius = pixels(c, 3)
            c.create_oval(x-radius, y-radius, x+radius, y+radius, fill=ACCENT, outline="")
        c.create_line(*coords, fill=ACCENT, width=2)
        c.create_text(left, height-pixels(c, 14), text="0 lx", fill=DIM, anchor="w", font=(FONT, 9))
        c.create_text(right, height-pixels(c, 14), text=f"{xmax:g} lx", fill=DIM, anchor="e", font=(FONT, 9))
        c.create_text(left, pixels(c, 14), text="自动目标亮度 %", fill=DIM, anchor="w", font=(FONT, 9))

    def save(self):
        for err in self.errors.values():
            update_widget(err, text="")
            err.grid_remove()
        try:
            draft = settings_draft(self.app.cfg, {k: v.get() for k, v in self.vars.items()},
                                   self.app.var_interval.get(), self.var_dry.get(), self.var_avoid.get())
            if not self.app._commit_config(draft):
                self._show_error("保存失败，请检查配置目录的写入权限；当前设置仍保持原值。")
                return
        except FieldError as exc:
            self._show_error(str(exc))
            if exc.key in self.errors:
                update_widget(self.errors[exc.key], text=str(exc))
                self.errors[exc.key].grid()
            page = self.pages[exc.key]
            while page.master is not self.tabs:
                page = page.master
            self.tabs.select(page)
            self.entries[exc.key].focus_set()
            return
        self.app._set_status_text('设置已保存，正在重新连接设备…' if self.app._connecting else "设置已保存并生效。", ACCENT)
        self.win.destroy()

    def _show_error(self, message):
        update_widget(self.error, text=message)
        self.error.pack(side="bottom", before=self.tabs, fill="x", padx=self._pad, pady=(8, 4))

    def reset(self):
        for key, var in self.vars.items():
            if key == "__anchors__":
                var.set(", ".join(f"{lux:g}:{lv}" for lux, lv in self.app.cfg.mapping.curve_points()))
            else:
                section, name = key.split(".")
                var.set(str(getattr(getattr(self.app.cfg, section), name)))
        self.var_dry.set(self.app.cfg.engine.dry_run)
        self.var_avoid.set(self.app.cfg.avoid_screen)
        self.baseline = {key: var.get() for key, var in self.vars.items()}
        self._dry_baseline = self.var_dry.get()
        self._avoid_baseline = self.var_avoid.get()
        self.app.var_interval.set(f"{self.app.cfg.engine.interval_s:g}")
        self.app.var_sample_speed.set(speed_from_interval(self.app.cfg.engine.interval_s))
        self.app._refresh_sample_label(self.app.cfg.engine.interval_s)
        self._show_error("已恢复到当前保存的设置。")
        for err in self.errors.values():
            update_widget(err, text="")
            err.grid_remove()
