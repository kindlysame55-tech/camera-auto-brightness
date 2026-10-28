"""GUI entry point shared by installed Unix apps and native bundles."""
from __future__ import annotations

import sys


def gui_main():
    from .__main__ import main
    from .config import default_config_dir
    args = sys.argv[1:] or ['tray']
    try:
        return main(args)
    except Exception:
        import traceback
        details = traceback.format_exc()
        try:
            folder = default_config_dir()
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / 'camab-crash.log').open('a', encoding='utf-8') as output:
                output.write(details + '\n')
        except OSError:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror('摄像头自动亮度 · 启动失败', details[-1200:], parent=root)
            root.destroy()
        except Exception:
            print(details, file=sys.stderr)
        return 1
