"""Frontend regressions using synthetic state and fake hardware only."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

import pytest

from camab.config import AppConfig
from camab.status import Status, default_log_path, setup_logging
from camab.tray import TrayApp
from camab.ui_settings import FieldError, parse_anchors
from camab.ui_theme import ERROR, SUCCESS, WARN


@pytest.fixture
def app(tk_window):
    application = TrayApp(AppConfig())
    application._win = tk_window
    application._build_ui()
    yield application
    if application._worker is not None:
        application._worker.shutdown(wait=True, cancel_futures=True)
    application._shutdown()


def pump(app, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app._poll()
        if app._poll_id is not None:
            app._win.after_cancel(app._poll_id)
            app._poll_id = None
        app._win.update()
        time.sleep(.005)
    assert predicate()


@pytest.mark.parametrize("which", ["brightness", "speed", "sampling"])
def test_keyboard_slider_increments_once_and_applies_once(app, monkeypatch, which):
    app._win.deiconify()
    if which == "sampling":
        app.open_settings()
        control = app.scale_sample
        method = "_apply_sample_speed"
    else:
        control = app.scale if which == "brightness" else app.scale_speed
        method = "_apply_scale_level" if which == "brightness" else "_apply_speed"
    calls = []
    monkeypatch.setattr(app, method, lambda: calls.append(control.get()))
    control.set(50)
    app._win.update()
    control.focus_force()
    control.event_generate("<KeyPress-Right>")
    app._win.update()
    assert control.get() == 51 and calls == [51]


def test_enter_activates_a_button_once(app):
    from camab.ui_theme import button
    calls = []
    control = button(app._content, "确认", lambda: calls.append(True))
    control.pack()
    app._win.deiconify()
    app._win.update()
    control.focus_force()
    control.event_generate("<KeyPress-Return>")
    app._win.update()
    assert calls == [True]


def test_settings_invalid_late_field_does_not_partially_apply(app):
    app.open_settings()
    panel = app._settings_panel
    before = app.cfg.to_dict()
    panel.vars["mapping.floor"].set("30")
    panel.vars["mapping.cooldown_s"].set("invalid")
    panel.save()
    assert app.cfg.to_dict() == before
    assert app.engine.cfg.to_dict() == before
    assert not app._config_path.exists()
    assert "冷却" in panel.error.cget("text")
    assert panel.tabs.index("current") == 2


def test_settings_disk_failure_keeps_running_config(app, monkeypatch):
    app.open_settings()
    panel = app._settings_panel
    panel.vars["mapping.floor"].set("30")
    before = app.cfg.to_dict()
    monkeypatch.setattr(AppConfig, "save", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("locked")))
    panel.save()
    assert app.cfg.to_dict() == before
    assert app.engine.cfg.to_dict() == before
    assert panel.win.winfo_exists()
    assert "保存失败" in panel.error.cget("text")


def test_saved_draft_and_engine_do_not_share_configuration(app):
    app.open_settings()
    panel = app._settings_panel
    panel.vars["mapping.floor"].set("30")
    panel.save()
    assert AppConfig.load(app._config_path).mapping.floor == 30
    assert app.engine.cfg.mapping.floor == 30
    assert app.cfg is not app.engine.cfg
    app.cfg.analysis.percentile = 80
    assert app.engine.cfg.analysis.percentile != 80


def test_preset_updates_untouched_draft_but_keeps_user_edits(app):
    app.open_settings()
    panel = app._settings_panel
    panel.vars["mapping.floor"].set("31")
    app.apply_preset("calm")
    assert panel.vars["mapping.cooldown_s"].get() == str(app.cfg.mapping.cooldown_s)
    assert panel.vars["mapping.floor"].get() == "31"
    panel.save()
    assert app.cfg.mapping.floor == 31
    assert app.cfg.mapping.cooldown_s == 8
    assert app.cfg.mapping.ramp_per_s == 4


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_nonfinite_custom_speeds_rejected(app, raw):
    before = app.cfg.to_dict()
    app.var_speed_value.set(raw)
    app._apply_custom_speed()
    app.open_settings()
    app.var_interval.set(raw)
    app._apply_custom_interval()
    assert app.cfg.to_dict() == before


@pytest.mark.parametrize("raw", ["2:20, nan:60", "2:20, 3:inf", "2:20, 2:40", "1:40, 20:30", "2:20", "0:20, 10:60", "2:20, 2.1:60"])
def test_invalid_curve_rejected(raw):
    with pytest.raises(FieldError):
        parse_anchors(raw)


def test_tray_icon_only_changes_when_needed(app):
    class Icon:
        icon_writes = 0
        title_writes = 0
        @property
        def icon(self):
            return None
        @icon.setter
        def icon(self, _value):
            self.icon_writes += 1
        @property
        def title(self):
            return ""
        @title.setter
        def title(self, _value):
            self.title_writes += 1
        def stop(self):
            pass
    icon = app._icon = Icon()
    status = Status(current=50, lux=123)
    for _ in range(20):
        app._update_tray(status)
    assert (icon.icon_writes, icon.title_writes) == (1, 1)
    status.lux = 140
    app._update_tray(status)
    assert (icon.icon_writes, icon.title_writes) == (1, 2)


def test_manual_commands_do_not_block_and_keep_latest_request(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    writes = []
    def slow_set(level, manual=True):
        writes.append(level)
        entered.set()
        assert release.wait(2)
        return level
    monkeypatch.setattr(app.engine, "set_level", slow_set)
    app._worker = ThreadPoolExecutor(max_workers=1)
    try:
        app._apply_scale_level(30)
        assert entered.wait(1)
        app._apply_scale_level(50)
        app._apply_scale_level(80)
        app._on_scale_drag("80")
        assert app.lbl_level.cget("text") == "80%"
        assert writes == [30]
        release.set()
        pump(app, lambda: not app._manual_busy and app._manual_pending is None)
        assert writes == [30, 80]
    finally:
        release.set()


def test_closing_settings_does_not_break_later_updates(app):
    s = app.open_settings()
    s.destroy()
    app._refresh_sample_label()
    app._refresh_interval_state()
    app.apply_preset("sensitive")
    assert app.open_settings().winfo_exists()


def test_failed_manual_write_restores_displayed_level(app, monkeypatch):
    status = Status(current=60)
    app._render(status)
    app._on_scale_drag("72")
    def fail(*_a, **_kw):
        raise RuntimeError("write failed")
    monkeypatch.setattr(app.engine, "set_level", fail)
    app._apply_scale_level(72)
    app._render(status)
    assert app.lbl_level.cget("text") == "60%"
    assert app.var_level.get() == 60


def test_camera_read_failure_cannot_display_automatic_success(app):
    app._render(Status(state="running", current=60, camera_ok=False,
                       camera_error="cap.read 返回 False"))
    assert "摄像头无法取得画面" in app.lbl_state.cget("text")
    assert "自动调光中" not in app.lbl_state.cget("text")
    assert app.lbl_state.cget("fg") == WARN


def test_brightness_permission_error_is_clear_and_persists_until_recovery(app):
    status = Status(state="running", current=60, camera_ok=True,
                    bright_error="Invoke-CimMethod: Access is denied\nCategoryInfo : PermissionDenied")
    for _ in range(3):
        app._render(status)
        assert "无法调节屏幕亮度" in app.lbl_state.cget("text")
        assert "系统拒绝访问" in app.lbl_state.cget("text")
        assert "CategoryInfo" not in app.lbl_state.cget("text")
        assert app.lbl_state.cget("fg") == ERROR
    status.bright_error = ""
    app._render(status)
    assert app.lbl_state.cget("text") == "自动调光中"
    assert app.lbl_state.cget("fg") == SUCCESS


@pytest.mark.parametrize("explicit", [False, True])
def test_simulation_is_explicit_in_main_state(app, explicit):
    app.cfg.engine.dry_run = explicit
    app._render(Status(state="running", camera_ok=True, bright_backend="dryrun"))
    text = app.lbl_state.cget("text")
    assert "模拟" in text and "屏幕亮度不会变化" in text
    assert ("未连接到可控制的显示器" in text) is (not explicit)
    assert app.lbl_state.cget("fg") == WARN


def test_failed_manual_write_feedback_remains_until_success(app, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("camab.tray.time.monotonic", lambda: now[0])
    app._manual_done(None, "WMI: 拒绝访问\n+ CategoryInfo: PermissionDenied\n+ FullyQualifiedErrorId: HRESULT")
    message = app.lbl_feedback.cget("text")
    assert "系统拒绝访问" in message and "\n" not in message
    now[0] = 1000.0
    app._render(Status(state="running", current=60, camera_ok=True))
    assert app.lbl_feedback.cget("text") == message
    app._manual_done(60, None)
    assert "已手动设为 60%" in app.lbl_feedback.cget("text")
    now[0] += 9.0
    app._render(Status(state="running", current=60, camera_ok=True))
    assert app.lbl_feedback.cget("text") == ""


def test_settings_save_button_visible_at_minimum_size(app):
    from tkinter import Button
    win = app.open_settings()
    win.geometry("660x560")
    win.deiconify()
    win.update()
    def walk(widget):
        for child in widget.winfo_children():
            yield child
            yield from walk(child)
    save = next(w for w in walk(win) if isinstance(w, Button) and w.cget("text") == "保存")
    assert save.winfo_y() >= 0
    assert save.winfo_rooty() + save.winfo_height() <= win.winfo_rooty() + win.winfo_height()


def test_small_main_window_keeps_controls_accessible_by_scrolling(app):
    app._win.geometry("520x560")
    app._win.deiconify()
    app._win.update()
    assert app._content.winfo_reqheight() > app._scroll_canvas.winfo_height()
    app._scroll_canvas.yview_moveto(1)
    app._win.update()
    assert app._scroll_canvas.yview()[1] == pytest.approx(1)


def test_explicit_config_path_used_for_ui_save(tmp_path, tk_window):
    path = tmp_path/"custom-profile.json"
    application = TrayApp(AppConfig(), config_path=path)
    application._win = tk_window
    application._build_ui()
    try:
        application.var_speed_value.set("7.5")
        application._apply_custom_speed()
        assert AppConfig.load(path).mapping.ramp_per_s == 7.5
    finally:
        application._shutdown()


def test_quit_waits_for_calibration_restore(app):
    class Wizard:
        cancelled = False
        def cancel(self):
            self.cancelled = True
    wizard = app._calib = Wizard()
    app._on_quit()
    assert wizard.cancelled
    assert app._quit_requested and not app._quitting
    app._after_calibration()
    assert app._quitting


def test_log_permission_failure_does_not_prevent_startup(tmp_path, monkeypatch):
    import camab.status as status
    monkeypatch.setattr(status, "RotatingFileHandler", lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("locked")))
    assert setup_logging(log_file=str(tmp_path/"readonly.log"), to_stdout=False) is logging.getLogger()


def test_logs_follow_isolated_config_and_rotate(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path/"config.json"))
    assert default_log_path() == str(tmp_path/"camab.log")
    root = setup_logging(log_file=default_log_path(), to_stdout=False)
    handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler) and h.baseFilename == default_log_path())
    assert handler.maxBytes == 2 * 1024 * 1024
    assert handler.backupCount == 3
    root.removeHandler(handler)
    handler.close()
