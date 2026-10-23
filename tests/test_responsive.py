"""Calibration layout at enlarged fonts, with synthetic callbacks only."""

import tkinter as tk

import pytest

from camab.calib_dialog import CalibrationWizard


def settle(win):
    for _ in range(4):
        win.update_idletasks()
        win.update()


def descendants(parent):
    for child in parent.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize("factor", [1, 1.25, 1.5, 2])
@pytest.mark.parametrize("size", [(560, 480), (360, 360)])
def test_calibration_large_fonts_fit_narrow_viewport(tk_window, monkeypatch, factor, size):
    previous_scaling = float(tk_window.tk.call("tk", "scaling"))
    errors = []
    monkeypatch.setattr(tk_window._root(), "report_callback_exception", lambda *args: errors.append(args))
    monkeypatch.setattr(CalibrationWizard, "_kick_sample", lambda _self: None)
    tk_window.tk.call("tk", "scaling", factor * 96 / 72)
    tk_window.deiconify()
    settle(tk_window)
    wiz = CalibrationWizard(tk_window, lambda: 80.0, lambda _level: None,
                            lambda: 60, lambda _anchors: None)
    try:
        width, height = size
        wiz.win.geometry(f"{width}x{height}")
        wiz.lbl_hint.configure(text="参考亮度与范围应完整显示，拖动滑块或使用左右方向键选择舒适的亮度。")
        wiz._set_status("当前光照与已记录的采样点过于接近，请改变房间光线后重新记录。" * 3)
        settle(wiz.win)
        assert wiz.win.resizable() == (1, 1)
        assert wiz.win.winfo_width() == width
        assert wiz.win.winfo_height() == height
        assert not errors
        body = wiz._scroll.body
        canvas = wiz._scroll.canvas
        assert body.winfo_width() == canvas.winfo_width()
        assert body.winfo_height() > canvas.winfo_height()
        # A scroll body can extend vertically, but individual controls must fit
        # horizontally and may not lose text or button labels.
        for widget in descendants(body):
            parent = widget.master
            assert widget.winfo_x() >= 0
            assert widget.winfo_x() + widget.winfo_width() <= parent.winfo_width() + 1
            if isinstance(widget, (tk.Button, tk.Label)):
                assert widget.winfo_width() + 2 >= widget.winfo_reqwidth()
        for button in (wiz.btn_done, wiz.btn_remove, wiz.btn_cancel):
            assert button.winfo_viewable()
            assert button.winfo_width() >= button.winfo_reqwidth()
            bottom = button.winfo_rooty() - wiz.win.winfo_rooty() + button.winfo_height()
            assert bottom <= height
        # The long status remains reachable in the actual compact viewport.
        canvas.yview_moveto(1)
        settle(wiz.win)
        assert canvas.yview()[1] == pytest.approx(1)
        if wiz.lbl_status.winfo_height() <= canvas.winfo_height():
            assert wiz.lbl_status.winfo_rooty() >= canvas.winfo_rooty() - 1
        assert (wiz.lbl_status.winfo_rooty() + wiz.lbl_status.winfo_height()
                <= canvas.winfo_rooty() + canvas.winfo_height() + 1)
    finally:
        wiz.cancel()
        tk_window.tk.call("tk", "scaling", previous_scaling)


def test_calibration_action_row_reflows_when_measurement_label_grows(tk_window, monkeypatch):
    monkeypatch.setattr(CalibrationWizard, "_kick_sample", lambda _self: None)
    tk_window.deiconify()
    settle(tk_window)
    wiz = CalibrationWizard(tk_window, lambda: 80.0, lambda _level: None,
                            lambda: 60, lambda _anchors: None)
    try:
        wiz.win.geometry("420x480")
        settle(wiz.win)
        wiz.btn_record.configure(text="正在重新测量环境光…")
        settle(wiz.win)
        for button in (wiz.btn_record, wiz.btn_preview):
            assert button.winfo_x() + button.winfo_width() <= button.master.winfo_width()
            assert button.winfo_width() >= button.winfo_reqwidth()
    finally:
        wiz.cancel()
