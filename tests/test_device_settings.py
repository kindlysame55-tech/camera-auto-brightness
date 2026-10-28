"""Device-setting edits stay transactional and only reconnect when necessary."""
from concurrent.futures import ThreadPoolExecutor
import os

import pytest

from camab.config import AppConfig
from camab.tray import TrayApp


@pytest.fixture
def app(tk_window):
    instance = TrayApp(AppConfig())
    instance._win = tk_window
    instance._build_ui()
    yield instance
    instance._shutdown()


def test_invalid_camera_index_never_persists_a_partial_device_change(app):
    app.open_settings()
    panel = app._settings_panel
    panel.vars['engine.backend'].set('ddcutil')
    panel.vars['camera.index'].set('1.5')
    panel.save()
    assert app.cfg.engine.backend == 'auto'
    assert not app._config_path.exists()
    assert '整数' in panel.error.cget('text')


def test_device_changes_reconnect_but_mapping_edits_do_not(app, monkeypatch):
    app._worker = ThreadPoolExecutor(max_workers=1)
    calls = []
    monkeypatch.setattr(app, '_retry_engine', lambda: calls.append('reconnect'))
    monkeypatch.setattr(app, '_apply_cfg', lambda: calls.append('apply'))
    draft = app.cfg.snapshot()
    draft.camera.index = 1
    assert app._commit_config(draft)
    assert calls == ['reconnect'] and AppConfig.load(app._config_path).camera.index == 1
    draft.mapping.floor = 30
    assert app._commit_config(draft)
    assert calls == ['reconnect', 'apply']


def test_connecting_device_edit_leaves_saved_and_live_config_unchanged(app):
    app._connecting = True
    before = app.cfg.to_dict()
    draft = app.cfg.snapshot()
    draft.camera.index = 2
    assert not app._commit_config(draft)
    assert app.cfg.to_dict() == before and not app._config_path.exists()


def test_minimize_without_a_tray_does_not_quit(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app._win, 'iconify', lambda: calls.append('minimize'))
    monkeypatch.setattr(app, '_on_quit', lambda: calls.append('quit'))
    app._minimize_window()
    assert calls == ['minimize']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX flock contract')
def test_unix_duplicate_launch_reuses_instance_and_releases_lock(tmp_path):
    path = tmp_path / 'config.json'
    first = TrayApp(AppConfig(), config_path=path)
    second = TrayApp(AppConfig(), config_path=path)
    try:
        assert first._claim_instance()
        assert not second._claim_instance()
        assert (tmp_path / 'show.flag').exists()
        first._shutdown()
        assert second._claim_instance()
    finally:
        first._shutdown()
        second._shutdown()
