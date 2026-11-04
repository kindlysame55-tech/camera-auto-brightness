"""Exercise the tray startup path that a GUI-only self-test used to skip."""

import pytest

from camab.config import AppConfig
from camab.tray import TrayApp


@pytest.fixture
def application(tk_window):
    app = TrayApp(AppConfig())
    app._win = tk_window
    app._build_ui()
    yield app
    app._shutdown()


def tray_backend(monkeypatch, **capabilities):
    import pystray
    from pystray._base import Icon

    class Backend(Icon):
        started = False
        stopped = False

        def __del__(self):
            # Constructor-failure coverage intentionally creates a partial object.
            pass

        def run_detached(self):
            self.started = True

        def stop(self):
            self.stopped = True

    for key, value in capabilities.items():
        monkeypatch.setattr(Backend, key, value)
    monkeypatch.setattr(pystray, 'Icon', Backend)
    return Backend


@pytest.mark.parametrize('supports_default', [True, False])
def test_startup_uses_documented_default_action_capability(application, monkeypatch, supports_default):
    backend = tray_backend(monkeypatch, HAS_DEFAULT_ACTION=supports_default)
    # This is the actual pystray API contract, including absence of the old typo.
    assert not hasattr(backend, 'HAS_DEFAULT')
    application._start_tray()
    assert isinstance(application._icon, backend)
    assert application._icon.started
    show_item = next(item for item in application._icon.menu if item.text == '显示窗口')
    assert show_item.default is supports_default
    show_item(application._icon)
    assert application._ui_queue.get_nowait() == application._show_window


def test_missing_optional_default_capability_keeps_tray_usable(application, monkeypatch):
    import pystray

    class Backend:
        HAS_MENU = True

        def __init__(self, name, image, title, menu):
            self.menu = menu

        def run_detached(self):
            self.started = True

        def stop(self):
            pass

    monkeypatch.setattr(pystray, 'Icon', Backend)
    application._start_tray()
    assert application._icon.started
    show_item = next(item for item in application._icon.menu if item.text == '显示窗口')
    assert show_item.default is False


@pytest.mark.parametrize('failure', ['construct', 'run'])
def test_backend_failure_preserves_main_window(application, monkeypatch, failure):
    backend = tray_backend(monkeypatch)
    stopped = []

    def fail(*args, **kwargs):
        raise RuntimeError('native tray unavailable')

    monkeypatch.setattr(backend, '__init__' if failure == 'construct' else 'run_detached', fail)
    monkeypatch.setattr(backend, 'stop', lambda self: stopped.append(True))
    application._start_tray()
    assert application._icon is None
    assert stopped == ([True] if failure == 'run' else [])
    application._show_window()
    application._win.update()
    assert application._win.winfo_viewable()


def test_no_menu_support_does_not_create_unrecoverable_icon(application, monkeypatch):
    backend = tray_backend(monkeypatch, HAS_MENU=False)
    monkeypatch.setattr(backend, '__init__', lambda *args: pytest.fail('icon must not be constructed'))
    application._start_tray()
    assert application._icon is None


def test_gui_selftest_includes_tray_startup(tk_window, monkeypatch):
    import camab.display as display
    import camab.tray as tray
    app = TrayApp(AppConfig())
    calls = []
    monkeypatch.setattr(display, 'configure_dpi', lambda: None)
    monkeypatch.setattr(tray.tk, 'Tk', lambda: tk_window)
    monkeypatch.setattr(app, '_build_ui', lambda: None)
    monkeypatch.setattr(app, '_start_tray', lambda: calls.append('tray'))
    monkeypatch.setattr(app, '_render', lambda status: None)
    monkeypatch.setattr(app, '_selftest', lambda: 0)
    assert app.start(selftest=True) == 0
    assert calls == ['tray']
