"""Shared, scale-aware desktop theme and native-widget helpers."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk

BG = "#f3f5f8"
SURFACE = "#ffffff"
FIELD = "#e9eef5"
BORDER = "#d9e1ec"
TINT = "#edf3fc"
FG = "#182437"
DIM = "#526176"
ACCENT = "#1d5cba"
PRIMARY = "#205bb0"
ON_PRIMARY = "#ffffff"
WARN = "#8a540c"
ERROR = "#b4232e"
SUCCESS = "#216344"
FONT = "Microsoft YaHei UI"
MONO = "Segoe UI"


def pixels(win: tk.Misc, value: float) -> int:
    """Scale spacing with Tk's point-to-pixel ratio (96 DPI is 4/3)."""
    return max(1, round(value * float(win.tk.call("tk", "scaling")) / (4 / 3)))


def bind_wrap(label: tk.Label, padding: int = 0) -> tk.Label:
    """Wrap at the allocated label width, including after text or DPI changes."""
    def resize(event):
        width = max(1, event.width - padding - 2 * label.winfo_pixels(label.cget("padx")))
        if label.winfo_pixels(label.cget("wraplength")) != width:
            label.configure(wraplength=width)
    label.configure(justify="left", anchor="w")
    label.bind("<Configure>", resize, add="+")
    return label


class ScrollFrame(tk.Frame):
    """A width-fitting scroll body with local wheel and keyboard-focus support.

    Pack/grid this frame; add content to ``body``. ``canvas`` remains available
    for page scrolling. It never binds a global mouse wheel handler.
    """
    def __init__(self, parent, *, bg=BG, **kwargs):
        super().__init__(parent, bg=bg, **kwargs)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=bg, borderwidth=0, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self._pending = None
        self.body.bind("<Configure>", self._schedule)
        self.canvas.bind("<Configure>", self._resize)
        self._top = self.winfo_toplevel()
        self._bindings = [
            ("<MouseWheel>", self._top.bind("<MouseWheel>", self._wheel, add="+")),
            ("<FocusIn>", self._top.bind("<FocusIn>", self._focus, add="+")),
            ("<Next>", self._top.bind("<Next>", lambda e: self._page(e, 1), add="+")),
            ("<Prior>", self._top.bind("<Prior>", lambda e: self._page(e, -1), add="+")),
        ]
        self.bind("<Destroy>", self._cleanup, add="+")

    def _owns(self, widget):
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._layout)

    def _resize(self, event):
        self.canvas.itemconfigure(self._window, width=max(1, event.width))
        self._schedule()

    def _layout(self):
        self._pending = None
        if not self.winfo_exists():
            return
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.body.winfo_reqheight() > self.canvas.winfo_height() + 1:
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _wheel(self, event):
        if not self._owns(event.widget) or isinstance(event.widget, ttk.Scale):
            return None
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            steps = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
            self.canvas.yview_scroll(steps, "units")
            return "break"
        return None

    def _page(self, event, direction):
        if self._owns(event.widget):
            self.canvas.yview_scroll(direction, "pages")
            return "break"
        return None

    def _focus(self, event):
        if self._owns(event.widget):
            self.ensure_visible(event.widget)

    def ensure_visible(self, widget):
        """Reveal the focused control without jumping when it is already visible."""
        if not self.winfo_exists() or not widget.winfo_exists():
            return
        height = self.body.winfo_height()
        if height <= self.canvas.winfo_height():
            return
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self.canvas.canvasy(0)
        visible_bottom = visible_top + self.canvas.winfo_height()
        margin = min(pixels(self, 8), max(0, (self.canvas.winfo_height()-widget.winfo_height()) // 2))
        if top - margin < visible_top:
            self.canvas.yview_moveto(max(0, top - margin) / height)
        elif bottom + margin > visible_bottom:
            self.canvas.yview_moveto(max(0, bottom + margin - self.canvas.winfo_height()) / height)

    def _cleanup(self, event):
        if event.widget is not self:
            return
        if self._pending is not None:
            self.after_cancel(self._pending)
        for sequence, funcid in self._bindings:
            try:
                self._top.unbind(sequence, funcid)
            except tk.TclError:
                pass


def fit_window(win, width, height, minwidth=440, minheight=440):
    """Set an initial size and minimum that fit the current monitor work area."""
    win.update_idletasks()
    left = top = 0
    right, bottom = win.winfo_screenwidth(), win.winfo_screenheight()
    try:
        import ctypes
        from ctypes import wintypes
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = user32.MonitorFromWindow(win.winfo_id(), 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            left, top, right, bottom = (info.rcWork.left, info.rcWork.top,
                                        info.rcWork.right, info.rcWork.bottom)
    except (AttributeError, OSError):
        pass
    # Leave room for the native window border and title bar as well as taskbar.
    available_width = max(1, right - left - pixels(win, 24))
    available_height = max(1, bottom - top - pixels(win, 56))
    minwidth, minheight = min(minwidth, available_width), min(minheight, available_height)
    width = max(minwidth, min(round(width), available_width))
    height = max(minheight, min(round(height), available_height))
    win.minsize(minwidth, minheight)
    x = left + max(0, (right - left - width) // 2)
    y = top + max(0, (bottom - top - height - pixels(win, 32)) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


def _scale_images(win: tk.Misc):
    owner = win._root()
    size = pixels(win, 22)
    images = getattr(owner, "_camab_scale_images", None)
    if images is None:
        images = [tk.PhotoImage(master=owner) for _ in range(4)]
        owner._camab_scale_images = images
    if getattr(owner, "_camab_scale_image_size", None) == size:
        return images
    owner._camab_scale_image_size = size
    radius = (size - 1) / 2
    ring = max(2, pixels(win, 2))
    for index, (thumb, color) in enumerate(zip(images[:3], (ACCENT, PRIMARY, DIM))):
        thumb.configure(width=size, height=size)
        thumb.blank()
        for y in range(size):
            extent = math.sqrt(max(0, radius ** 2 - (y - radius) ** 2))
            left, right = math.ceil(radius - extent), math.floor(radius + extent) + 1
            if right > left:
                thumb.put(color, to=(left, y, right, y + 1))
            inner = radius - ring
            if index != 1 and abs(y - radius) <= inner:
                extent = math.sqrt(max(0, inner ** 2 - (y - radius) ** 2))
                left, right = math.ceil(radius - extent), math.floor(radius + extent) + 1
                if right > left:
                    thumb.put(SURFACE, to=(left, y, right, y + 1))
    track = images[-1]
    track.configure(width=3, height=size + pixels(win, 8))
    track.blank()
    middle = (size + pixels(win, 8)) // 2
    thickness = pixels(win, 3)
    track.put(BORDER, to=(0, middle - thickness // 2, 3, middle + (thickness + 1) // 2))
    return images


def apply_theme(win: tk.Misc) -> None:
    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=FIELD, foreground=DIM,
                    padding=(pixels(win, 16), pixels(win, 9)), font=(FONT, 10))
    style.map("TNotebook.Tab", background=[("selected", SURFACE)],
              foreground=[("selected", ACCENT)])
    style.configure("TFrame", background=BG)
    style.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG,
                    bordercolor=BG, lightcolor=BORDER, darkcolor=BORDER,
                    arrowcolor=DIM, borderwidth=0, arrowsize=pixels(win, 13))
    images = _scale_images(win)
    if "Camab.slider" not in style.element_names():
        style.element_create("Camab.slider", "image", images[0],
                             ("disabled", images[2]), ("focus", images[1]), ("active", images[1]))
        style.element_create("Camab.trough", "image", images[-1], border=(1, 0, 1, 0), sticky="we")
    for name in ("Main", "Set", "Calib"):
        style.layout(f"{name}.Horizontal.TScale", [("Camab.trough", {
            "sticky": "we", "children": [("Camab.slider", {"side": "left", "sticky": ""})]})])
        style.configure(f"{name}.Horizontal.TScale", background=SURFACE,
                        troughcolor=BORDER, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        borderwidth=0)
    style.configure("Brightness.Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=FIELD, borderwidth=0, thickness=pixels(win, 4))


def button(parent, text, command, *, primary=False, **kwargs):
    options = dict(text=text, command=command, bg=PRIMARY if primary else FIELD,
                   fg=ON_PRIMARY if primary else FG,
                   activebackground="#174b92" if primary else "#dae4f1",
                   activeforeground=ON_PRIMARY if primary else FG,
                   disabledforeground="#bdcce2" if primary else DIM,
                   relief="flat", borderwidth=0, highlightthickness=1,
                   highlightbackground=PRIMARY if primary else BORDER,
                   highlightcolor=ACCENT, cursor="hand2", takefocus=True,
                   font=(FONT, 10), padx=pixels(parent, 12), pady=pixels(parent, 7))
    options.update(kwargs)
    control = tk.Button(parent, **options)
    def activate(_event):
        control.invoke()
        return "break"
    control.bind("<Return>", activate)
    return control


def entry(parent, variable, width=10):
    return tk.Entry(parent, textvariable=variable, width=width, bg=SURFACE,
                    fg=FG, insertbackground=FG, relief="flat",
                    highlightthickness=1, highlightbackground=BORDER,
                    highlightcolor=ACCENT, font=(MONO, 11))


def update_widget(widget, **options) -> None:
    """Avoid unchanged Tk commands; tolerate a just-closed settings window."""
    if widget is None or not widget.winfo_exists():
        return
    cached = getattr(widget, "_camab_options", {})
    changed = {key: value for key, value in options.items() if cached.get(key) != value}
    if changed:
        widget.configure(**changed)
        cached.update(changed)
        widget._camab_options = cached
