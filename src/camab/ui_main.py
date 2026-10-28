"""Main control panel: readable at narrow widths and large system text sizes."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .metrics import speed_from_ramp
from .ui_theme import (ACCENT, BG, BORDER, DIM, FG, FONT, MONO, SURFACE, TINT,
                       ScrollFrame, apply_theme, bind_wrap, button, entry,
                       fit_window, pixels)


class FlowRow(tk.Frame):
    """Keep a row together when it fits; stack whole controls when it does not."""
    def __init__(self, parent, *, bg, gap=8, equal=True):
        super().__init__(parent, bg=bg)
        self.gap = pixels(self, gap)
        self.equal = equal
        self.controls = []
        self._pending = None
        self._layout_key = None
        self.bind("<Configure>", self._schedule)
        self.bind("<Destroy>", self._cleanup, add="+")

    def add(self, widget):
        self.controls.append(widget)
        widget.bind("<Configure>", self._schedule, add="+")
        self._schedule()
        return widget

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._layout)

    @staticmethod
    def _natural_width(widget):
        if isinstance(widget, tk.Label):
            font = tkfont.Font(font=widget.cget("font"))
            text = str(widget.cget("text"))
            return max((font.measure(line) for line in text.splitlines()), default=0) + 8
        if isinstance(widget, tk.Frame) and widget.winfo_children():
            children = widget.winfo_children()
            widths = [FlowRow._natural_width(child) for child in children]
            if all(child.winfo_manager() == "pack" and child.pack_info()["side"] in ("left", "right")
                   for child in children):
                return sum(widths) + pixels(widget, 24)
            return max(widths)
        return widget.winfo_reqwidth()

    def _layout(self):
        self._pending = None
        if not self.winfo_exists() or not self.controls:
            return
        widths = [self._natural_width(widget) for widget in self.controls]
        required = max(widths) * len(self.controls) if self.equal else sum(widths)
        required += self.gap * (len(self.controls) - 1)
        columns = len(self.controls) if required <= self.winfo_width() else 1
        layout_key = (columns, tuple(widths))
        if layout_key == self._layout_key:
            return
        self._layout_key = layout_key
        for column in range(len(self.controls)):
            weight = 1 if column < columns and (self.equal or column == 0) else 0
            minimum = widths[column] if not self.equal and columns > 1 else 0
            self.columnconfigure(column, weight=weight,
                                 uniform="flow" if self.equal and column < columns else "",
                                 minsize=minimum)
        for index, widget in enumerate(self.controls):
            widget.grid(row=index // columns, column=index % columns, sticky="ew",
                        padx=(0, self.gap if index % columns < columns - 1 else 0),
                        pady=(0, self.gap if columns == 1 and index < len(self.controls) - 1 else 0))

    def _cleanup(self, event):
        if event.widget is self and self._pending is not None:
            self.after_cancel(self._pending)


def build_main(app) -> None:
    root = app._win
    root.bind("<Destroy>", app._dispose_ui_variables, add="+")
    apply_theme(root)
    root.configure(bg=BG)
    root.resizable(True, True)
    modifier = 'Command' if root.tk.call('tk', 'windowingsystem') == 'aqua' else 'Control'
    root.bind(f'<{modifier}-comma>', lambda _e: app.open_settings())
    root.bind(f'<{modifier}-q>', lambda _e: app._on_quit())
    p = lambda value: pixels(root, value)
    scroll = ScrollFrame(root)
    scroll.pack(fill="both", expand=True)
    w = scroll.body
    app._main_scroll = scroll
    app._content = w
    app._scroll_canvas = scroll.canvas
    gutter, inset = p(20), p(18)

    def label(parent, text="", *, size=10, color=DIM, bold=False, bg=SURFACE):
        return bind_wrap(tk.Label(parent, text=text, bg=bg, fg=color,
                                  font=(FONT, size, "bold" if bold else "normal"),
                                  anchor="w", justify="left", padx=0, pady=0))

    def card():
        panel = tk.Frame(w, bg=SURFACE, highlightthickness=1,
                         highlightbackground=BORDER)
        panel.pack(fill="x", padx=gutter, pady=(0, p(12)))
        return panel

    head = FlowRow(w, bg=BG, gap=16, equal=False)
    head.pack(fill="x", padx=gutter, pady=(p(22), p(8)))
    title = tk.Label(head, text="自动亮度", bg=BG, fg=FG,
                     font=(FONT, 21, "bold"), anchor="w", padx=0)
    head.add(bind_wrap(title))
    head.add(button(head, "设置与校准", app.open_settings))
    label(w, "根据环境光调整屏幕，让变化更舒适。", bg=BG).pack(
        fill="x", padx=gutter, pady=(0, p(16)))

    state = tk.Frame(w, bg=TINT, highlightthickness=1,
                     highlightbackground=BORDER)
    state.pack(fill="x", padx=gutter, pady=(0, p(12)))
    app.lbl_state = label(state, "正在连接设备…", color=ACCENT, bold=True, bg=TINT)
    app.lbl_state.pack(fill="x", padx=inset, pady=p(12))

    brightness = card()
    top = FlowRow(brightness, bg=SURFACE, equal=False)
    top.pack(fill="x", padx=inset, pady=(p(16), p(2)))
    top.add(label(top, "屏幕亮度", color=FG, bold=True))
    app.lbl_target = top.add(label(top, "自动目标  —", color=ACCENT))
    app.lbl_target.configure(anchor="e")
    values = FlowRow(brightness, bg=SURFACE, gap=18, equal=False)
    values.pack(fill="x", padx=inset, pady=(0, p(4)))
    app.lbl_level = tk.Label(values, text="--%", bg=SURFACE, fg=FG,
                             font=(MONO, 38, "bold"), anchor="w", padx=0, pady=0)
    def fit_number(event):
        size = 38
        while size > 24 and tkfont.Font(family=MONO, size=size, weight="bold").measure("100%") > event.width - 4:
            size -= 1
        if tkfont.Font(font=app.lbl_level.cget("font")).cget("size") != size:
            app.lbl_level.configure(font=(MONO, size, "bold"))
    app.lbl_level.bind("<Configure>", fit_number)
    values.add(app.lbl_level)
    light = tk.Frame(values, bg=SURFACE)
    app.lbl_lux = label(light, "环境光  -- lx", size=12, color=FG, bold=True)
    app.lbl_lux.pack(fill="x", pady=(p(8), p(4)))
    app.lbl_mode = label(light, "等效照度 · 本地测光", size=9)
    app.lbl_mode.pack(fill="x")
    values.add(light)
    app.var_level = tk.IntVar(master=w, value=60)
    app.scale = ttk.Scale(brightness, from_=0, to=100, orient="horizontal",
                          variable=app.var_level, command=app._on_scale_drag,
                          style="Main.Horizontal.TScale", takefocus=True)
    app.scale.pack(fill="x", padx=inset, pady=(p(2), p(4)), ipady=p(5))
    app._scale_guard = app._scale_dragging = False
    app.scale.bind("<ButtonPress-1>", app._on_scale_press)
    app.scale.bind("<ButtonRelease-1>", app._on_scale_release)
    for key, delta in (("Left", -1), ("Right", 1), ("Down", -1), ("Up", 1)):
        app.scale.bind(f"<{key}>", lambda _e, d=delta: app._nudge_level(d))
    app.lbl_hold_hint = label(brightness, "拖动后临时接管；方向键可微调 1%。", size=9)
    app.lbl_hold_hint.pack(fill="x", padx=inset, pady=(0, p(16)))

    feel = card()
    speed_head = FlowRow(feel, bg=SURFACE, equal=False)
    speed_head.pack(fill="x", padx=inset, pady=(p(16), p(6)))
    speed_head.add(label(speed_head, "过渡速度", color=FG, bold=True))
    app.lbl_speed = speed_head.add(label(speed_head, color=ACCENT))
    app.lbl_speed.configure(anchor="e")
    app.var_speed = tk.IntVar(master=w, value=speed_from_ramp(app.cfg.mapping.ramp_per_s))
    app.scale_speed = ttk.Scale(feel, from_=0, to=100, orient="horizontal",
                                variable=app.var_speed, command=app._on_speed_drag,
                                style="Main.Horizontal.TScale", takefocus=True)
    app.scale_speed.pack(fill="x", padx=inset, ipady=p(5))
    app._speed_guard = app._speed_dragging = False
    app.scale_speed.bind("<ButtonPress-1>", lambda _e: app._set_speed_drag(True))
    app.scale_speed.bind("<ButtonRelease-1>", lambda _e: app._set_speed_drag(False))
    for key, delta in (("Left", -1), ("Right", 1)):
        app.scale_speed.bind(f"<{key}>", lambda _e, d=delta: app._nudge_speed(d))
    endpoints = tk.Frame(feel, bg=SURFACE)
    endpoints.pack(fill="x", padx=inset, pady=(0, p(12)))
    label(endpoints, "快速响应", size=9).pack(side="left")
    label(endpoints, "柔和过渡", size=9).pack(side="right")
    quick = FlowRow(feel, bg=SURFACE, gap=8)
    quick.pack(fill="x", padx=inset, pady=(0, p(12)))
    app._preset_buttons = {}
    for text, preset in (("灵敏", "sensitive"), ("均衡", "balanced"), ("平滑", "calm")):
        control = quick.add(button(quick, text, lambda name=preset: app.apply_preset(name)))
        app._preset_buttons[preset] = control
    custom = FlowRow(feel, bg=SURFACE, gap=12, equal=False)
    custom.pack(fill="x", padx=inset, pady=(0, p(16)))
    field = tk.Frame(custom, bg=SURFACE)
    label(field, "速度上限", size=9).pack(side="left", padx=(0, p(8)))
    app.var_speed_value = tk.StringVar(master=w, value=f"{app.cfg.mapping.ramp_per_s:g}")
    app.ent_speed = entry(field, app.var_speed_value, 6)
    app.ent_speed.pack(side="left", ipady=p(4))
    app.ent_speed.bind("<Return>", lambda _e: app._apply_custom_speed())
    app.ent_speed.bind("<FocusOut>", lambda _e: app._apply_custom_speed())
    label(field, "%/秒", size=10).pack(side="left", padx=(p(6), 0))
    custom.add(field)
    custom.add(label(custom, "数值越小，亮度变化越柔和。", size=9))
    app._refresh_speed_label(app.cfg.mapping.ramp_per_s)

    actions = FlowRow(w, bg=BG, gap=8)
    actions.pack(fill="x", padx=gutter, pady=(p(2), p(10)))
    app.btn_pause = actions.add(button(actions, "暂停自动", app.toggle_pause, primary=True))
    app.btn_sample = actions.add(button(actions, "立即采样", app._sample_now))
    app.btn_retry = actions.add(button(actions, "重试连接", app._retry_engine))
    app.lbl_feedback = label(w, bg=BG)
    app.lbl_feedback.pack(fill="x", padx=gutter, pady=(0, p(12)))

    foot = FlowRow(w, bg=BG, gap=8)
    foot.pack(fill="x", padx=gutter, pady=(0, p(18)))
    foot.add(button(foot, "设备详情", app._toggle_details))
    app.btn_hide = foot.add(button(foot, '最小化窗口', app._minimize_window))
    foot.add(button(foot, "退出", app._on_quit))
    app.details = tk.Frame(w, bg=SURFACE, highlightthickness=1,
                           highlightbackground=BORDER)
    app.lbl_sub = label(app.details, size=9)
    app.lbl_sub.pack(fill="x", padx=inset, pady=(p(12), p(6)))
    app.lbl_log = label(app.details, size=9)
    app.lbl_log.pack(fill="x", padx=inset, pady=(0, p(12)))
    # Set a realistic viewport before measuring wrapped content. Large text may
    # need scrolling; the window itself still fits the monitor work area.
    fit_window(root, p(600), p(780), minwidth=440, minheight=440)
    root.update_idletasks()
