"""Process-local Windows display setup, before Tk creates any windows."""

from __future__ import annotations

import ctypes
import sys


def configure_dpi() -> bool:
    """Use system DPI on Tk 8.6; leave already-aware hosts untouched."""
    if sys.platform != "win32":
        return False
    try:
        shcore = ctypes.windll.shcore
        awareness = ctypes.c_int()
        if shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness)) == 0:
            if awareness.value:
                return True
        return shcore.SetProcessDpiAwareness(1) == 0
    except (AttributeError, OSError):
        try:
            return bool(ctypes.windll.user32.SetProcessDPIAware())
        except (AttributeError, OSError):
            return False
