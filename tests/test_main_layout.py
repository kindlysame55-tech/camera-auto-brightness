"""Main-panel layout regressions; synthetic state, no physical devices."""

import tkinter as tk
import tkinter.font as tkfont

import pytest

from camab.config import AppConfig
from camab.status import Status
from camab.tray import TrayApp
from camab.ui_theme import ACCENT, BG, DIM, ERROR, FG, ON_PRIMARY, PRIMARY, SURFACE, TINT, WARN


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize("scale", [4 / 3, 5 / 3, 2, 8 / 3])
@pytest.mark.parametrize("size", ["440x480", "520x560", "900x760"])
def test_main_controls_fit_width_and_remain_reachable(tk_window, scale, size):
    before = tk_window.tk.call("tk", "scaling")
    tk_window.tk.call("tk", "scaling", scale)
    app = TrayApp(AppConfig())
    app._win = tk_window
    try:
        app._build_ui()
        app._render(Status(current=100, lux=9876.5, ideal=100, camera_ok=True, samples=5))
        tk_window.geometry(size)
        tk_window.deiconify()
        tk_window.update()
        body = app._content
        left, right = body.winfo_rootx(), body.winfo_rootx() + body.winfo_width()
        assert tkfont.Font(font=app.lbl_level.cget("font")).measure("100%") <= app.lbl_level.winfo_width()
        for control in descendants(body):
            if not control.winfo_ismapped() or not isinstance(control, (tk.Button, tk.Entry)):
                continue
            assert control.winfo_rootx() >= left, (size, scale, control)
            assert control.winfo_rootx() + control.winfo_width() <= right, (size, scale, control)
            assert control.winfo_width() >= control.winfo_reqwidth(), (size, scale, control)
        app._main_scroll.ensure_visible(app.ent_speed)
        tk_window.update()
        canvas = app._scroll_canvas
        assert canvas.winfo_rooty() <= app.ent_speed.winfo_rooty()
        assert app.ent_speed.winfo_rooty() + app.ent_speed.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()
        app._main_scroll.ensure_visible(app.btn_retry)
        tk_window.update()
        assert canvas.winfo_rooty() <= app.btn_retry.winfo_rooty()
        assert app.btn_retry.winfo_rooty() + app.btn_retry.winfo_height() <= canvas.winfo_rooty() + canvas.winfo_height()
    finally:
        app._shutdown()
        tk_window.tk.call("tk", "scaling", before)


def test_long_status_and_feedback_wrap_to_available_width(tk_window):
    app = TrayApp(AppConfig())
    app._win = tk_window
    try:
        app._build_ui()
        app._render(Status(state="error", current=50, camera_ok=False,
                           camera_error="摄像头连接失败，请检查视频源和设备权限。" * 8))
        app._set_status_text("保存失败：" + "无法写入指定配置文件，请重试。" * 10, ERROR, persistent=True)
        tk_window.geometry("440x480")
        tk_window.deiconify()
        tk_window.update()
        for label in (app.lbl_state, app.lbl_feedback):
            assert label.winfo_pixels(label.cget("wraplength")) <= label.winfo_width()
            assert label.winfo_height() >= tkfont.Font(font=label.cget("font")).metrics("linespace") * 2
        narrow = app.lbl_feedback.winfo_height()
        tk_window.geometry("900x760")
        tk_window.update()
        assert app.lbl_feedback.winfo_height() < narrow
    finally:
        app._shutdown()


def _luminance(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
              for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))


@pytest.mark.parametrize("foreground,background", [
    (FG, BG), (FG, SURFACE), (DIM, BG), (DIM, SURFACE), (ACCENT, TINT),
    (ERROR, SURFACE), (WARN, SURFACE), (ON_PRIMARY, PRIMARY),
])
def test_theme_text_contrast(foreground, background):
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    assert (light + .05) / (dark + .05) >= 4.5
