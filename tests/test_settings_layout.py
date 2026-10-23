"""Settings remain usable in small viewports and enlarged system text."""
import tkinter as tk

import pytest

from camab.config import AppConfig
from camab.tray import TrayApp
from camab.ui_theme import pixels


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize("scale", [4/3, 5/3, 2, 8/3])
@pytest.mark.parametrize("size", ["440x420", "680x620"])
def test_every_settings_page_scrolls_without_hiding_fields_or_save(tk_window, scale, size):
    before = tk_window.tk.call("tk", "scaling")
    tk_window.tk.call("tk", "scaling", scale)
    app = TrayApp(AppConfig())
    app._win = tk_window
    try:
        app._build_ui()
        tk_window.deiconify()
        app.open_settings()
        panel = app._settings_panel
        panel.win.geometry(size)
        panel.win.update()
        for control in panel.action_buttons:
            assert control.winfo_ismapped()
            assert control.winfo_y()+control.winfo_height() <= control.master.winfo_height()
            assert control.winfo_rooty()+control.winfo_height() <= panel.win.winfo_rooty()+panel.win.winfo_height()
        for index, scroll in enumerate(panel.scrolls):
            panel.tabs.select(index)
            panel.win.update()
            left = scroll.body.winfo_rootx()
            right = left+scroll.body.winfo_width()
            for control in descendants(scroll.body):
                if not control.winfo_ismapped() or not isinstance(control, (tk.Entry, tk.Button)):
                    continue
                assert control.winfo_rootx() >= left
                assert control.winfo_rootx()+control.winfo_width() <= right
                scroll.ensure_visible(control)
                panel.win.update()
                assert scroll.canvas.winfo_rooty() <= control.winfo_rooty()
                assert control.winfo_rooty()+control.winfo_height() <= scroll.canvas.winfo_rooty()+scroll.canvas.winfo_height()
        # All three navigation labels are accessible, even at 200% / 440px.
        visible_tabs = set()
        y = pixels(panel.tabs, 12)
        for x in range(0, panel.tabs.winfo_width(), 2):
            try:
                visible_tabs.add(panel.tabs.index(f"@{x},{y}"))
            except tk.TclError:
                pass
        assert visible_tabs == {0, 1, 2}
        for index in range(3):
            panel.tabs.select(index)
            assert panel.tabs.index("current") == index
    finally:
        app._shutdown()
        tk_window.tk.call("tk", "scaling", before)
