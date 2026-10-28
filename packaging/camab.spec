# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：camera-auto-brightness。

用法（在**项目根目录**执行）：

    .\\.venv\\Scripts\\python.exe -m PyInstaller packaging\\camab.spec --noconfirm `
        --distpath dist --workpath build

一次分析、两个 EXE：

* ``dist\\Camab.exe``     —— 控制台版，跑 preflight / run / probe / calibrate / config
* ``dist\\CamabTray.exe`` —— 窗口版（``console=False``，无黑框），双击直接进托盘

设计要点
--------
1. **两个入口脚本是构建期生成的**（写进 ``build\\<spec名>\\_gen\\``），
   这样 ``packaging\\`` 目录里不需要额外的 .py 文件。
2. ``pathex`` 指向 ``src``（等价于命令行 ``--paths src``），保证 ``camab`` 包能被找到。
3. ``camab.__main__`` 里的 import 都写在函数内部（惰性导入），静态分析虽然能找到，
   这里仍把 13 个子模块 + pystray / pywin32 后端全部显式列进 ``hiddenimports``，更保险。
4. ``runtime_hooks`` 里那个钩子解决一个真实缺陷：``camab.camera.probe_in_subprocess()``
   用 ``sys.executable -c <脚本>`` 起子进程探测摄像头，冻结后 ``sys.executable`` 变成
   ``Camab.exe``，不拦的话 argparse 会报错、preflight 的摄像头探测直接废掉。
5. 默认 **onefile**（dist 下就两个 exe，符合"双击即用"）。想换成 onedir
   （启动更快、不用每次解压）就先设置环境变量 ``CAMAB_PYI_ONEDIR=1``；
   ``packaging\\build_exe.ps1 -OneDir`` 已经帮你设置好了。
"""

import os
import sys

# --------------------------------------------------------------------------- #
# 路径：spec 在 packaging\ 下，项目根 = 上一级
# --------------------------------------------------------------------------- #
# SPECPATH 是 spec 所在**目录**（packaging\）；兼容有人把 spec 挪走的情况：
# 从锚点往上找，谁里面有 src\camab 谁就是项目根。
_ANCHOR = os.path.abspath(
    globals().get("SPECPATH")
    or os.path.dirname(globals().get("SPEC", ""))
    or os.path.dirname(os.path.abspath(__file__))
)
if os.path.isfile(_ANCHOR):
    _ANCHOR = os.path.dirname(_ANCHOR)


def _find_project_root(start):
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, "src", "camab")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


PROJECT_ROOT = _find_project_root(_ANCHOR) or os.path.dirname(_ANCHOR)
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

if not os.path.isdir(SRC_DIR):
    raise SystemExit(
        "[camab.spec] 找不到源码目录：%s\n"
        "  （从 %s 往上都没看到 src\\camab；请把 spec 放在 <项目>\\packaging\\ 下）"
        % (SRC_DIR, _ANCHOR)
    )

# onefile（默认）还是 onedir
ONEDIR = os.environ.get("CAMAB_PYI_ONEDIR", "").strip().lower() in ("1", "true", "yes", "on")
if sys.platform == 'darwin':
    ONEDIR = True  # Stable .app identity for macOS camera permission and signing.

# --------------------------------------------------------------------------- #
# 构建期生成：入口脚本 + 运行时钩子（避免在 packaging\ 里堆小文件）
# --------------------------------------------------------------------------- #
try:
    _BUILD_BASE = workpath                                            # noqa: F821
except NameError:                                                     # pragma: no cover
    _BUILD_BASE = os.path.join(PROJECT_ROOT, "build")
_GEN_DIR = os.path.join(_BUILD_BASE, "_gen")
os.makedirs(_GEN_DIR, exist_ok=True)


def _write(name, text):
    path = os.path.join(_GEN_DIR, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


# 控制台版入口：参数原样透传给 camab CLI
ENTRY_CONSOLE = _write("entry_console.py", '''\
"""由 packaging/camab.spec 构建期自动生成，请勿手工修改。"""
import sys

from camab.__main__ import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
''')

# 托盘版入口：不给参数时默认进 tray（双击即用）；崩溃时写日志 + 弹窗，不留哑巴进程
ENTRY_TRAY = _write("entry_tray.py", '''\
"""由 packaging/camab.spec 构建期自动生成，请勿手工修改。"""
import os
import sys

from camab.__main__ import main


def _crash_log(text):
    """无控制台的窗口版程序崩了不能一声不吭：落一个日志文件。"""
    try:
        import datetime
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "camera-auto-brightness")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "camab-crash.log")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\\n===== %s =====\\n%s\\n" % (stamp, text))
        return path
    except Exception:
        return None


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        argv = ["tray"]          # 双击运行 → 直接进托盘
    try:
        sys.exit(main(argv))
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        path = _crash_log(tb)
        try:
            import tkinter.messagebox as mb
            mb.showerror(
                "摄像头自动亮度 —— 启动失败",
                tb[-1500:] + ("\\n\\n日志：%s" % path if path else ""),
            )
        except Exception:
            pass
        raise
''')

# 运行时钩子：让冻结后的 exe 支持 `-c <code>`（模拟 python -c）
RTHOOK = _write("rthook_frozen_c.py", '''\
"""PyInstaller 运行时钩子：在冻结的 exe 里模拟 `python -c <代码>`。

camab.camera.probe_in_subprocess() 用 `sys.executable -c <脚本>` 起子进程探测摄像头；
打包后 sys.executable 是 Camab.exe，不拦的话子进程会变成 `Camab.exe -c ...`，
argparse 直接报 "unrecognized arguments: -c"，摄像头探测失效。
这里在启动最早期截获 `-c`，用 exec 跑那段代码，等价于 `python -c`。
camab CLI 本身没有 -c 选项，不存在冲突。
"""
import os
import sys


def _shim():
    argv = sys.argv
    if len(argv) < 3 or argv[1] != "-c":
        return
    code = argv[2]
    sys.argv = [argv[0]] + list(argv[3:])
    try:
        exec(compile(code, "<camab -c>", "exec"), {"__name__": "__main__"})
        status = 0
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        import traceback
        traceback.print_exc()
        status = 1
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(status)


_shim()
''')

# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #
HIDDEN = [
    # 本包（__main__ 里是惰性导入，显式列出更保险）
    "camab",
    "camab.__main__",
    "camab.brightness",
    "camab.camera",
    "camab.cli_run",
    "camab.config",
    "camab.devices",
    "camab.engine",
    "camab.lightmeter",
    "camab.metrics",
    "camab.preflight",
    "camab.status",
    "camab.tray",
    # 托盘：pystray 按平台动态选后端，Windows 用 _win32
    "pystray",
    "pystray._base",
    "pystray._win32",
    # pywin32 / WMI 亮度后端
    "win32com",
    "win32com.client",
    "win32com.client.dynamic",
    "win32timezone",
    "pythoncom",
    "pywintypes",
    "win32api",
    # 其它
    "PIL.Image",
    "PIL.ImageDraw",
    "cv2",
    "numpy",
]
if sys.platform != 'win32':
    HIDDEN = [name for name in HIDDEN if not name.startswith(('win32', 'pystray._win32'))
              and name not in ('pythoncom', 'pywintypes')]
    HIDDEN += (['pystray._darwin'] if sys.platform == 'darwin' else ['pystray._xorg', 'pystray._appindicator', 'pystray._gtk'])
HIDDEN += ['camab.platform_support', 'camab.unix_brightness', 'camab.ui_main', 'camab.ui_settings', 'camab.ui_theme']

EXCLUDES = [
    # 明显用不到的大块头，剔掉能显著瘦身
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "jupyter",
    "notebook",
    "pytest",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
]

# 找包：只要 src。venv 的 site-packages 本来就在 PyInstaller 自己的搜索路径里
# （PyInstaller 就是用这个 venv 的解释器跑的），再手动加一遍只会触发
# "Foreign Python environment's site-packages paths added to --paths/pathex"
# 弃用告警（PyInstaller 7.0 会直接报错）。
PATHEX = [SRC_DIR]


def _analyze(entry_script, name):
    return Analysis(
        [entry_script],
        pathex=PATHEX,
        binaries=[],
        datas=[],
        hiddenimports=HIDDEN,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[RTHOOK],
        excludes=EXCLUDES,
        noarchive=False,
        optimize=0,
    )


a_console = _analyze(ENTRY_CONSOLE, "Camab")
a_tray = _analyze(ENTRY_TRAY, "CamabTray")

pyz_console = PYZ(a_console.pure)
pyz_tray = PYZ(a_tray.pure)

_EXE_KW = dict(
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if ONEDIR:
    exe_console = EXE(pyz_console, a_console.scripts, [],
                      exclude_binaries=True, name="Camab", console=True, **_EXE_KW)
    exe_tray = EXE(pyz_tray, a_tray.scripts, [],
                   exclude_binaries=True, name="CamabTray", console=False, **_EXE_KW)
    coll_console = COLLECT(exe_console, a_console.binaries, a_console.datas,
                           strip=False, upx=False, name="Camab")
    coll_tray = COLLECT(exe_tray, a_tray.binaries, a_tray.datas,
                        strip=False, upx=False, name="CamabTray")
else:
    # 单文件：dist 下就两个 exe，拷贝/双击最省事
    exe_console = EXE(pyz_console, a_console.scripts, a_console.binaries, a_console.datas,
                      [], name="Camab", console=True, **_EXE_KW)
    exe_tray = EXE(pyz_tray, a_tray.scripts, a_tray.binaries, a_tray.datas,
                   [], name="CamabTray", console=False, **_EXE_KW)

if sys.platform == 'darwin':
    app = BUNDLE(coll_tray, name='Camera Auto Brightness.app',
                 bundle_identifier='tech.kindlysame55.camera-auto-brightness',
                 info_plist={
                     'CFBundleShortVersionString': '2.2.0',
                     'CFBundleVersion': '2.2.0',
                     'NSHighResolutionCapable': True,
                     'NSPrincipalClass': 'NSApplication',
                     'NSCameraUsageDescription': '摄像头画面仅在本机用于估算环境光并调节屏幕亮度。Camera images are processed locally to estimate ambient light.',
                 })

print("[camab.spec] 项目根     : %s" % PROJECT_ROOT)
print("[camab.spec] 布局       : %s" % ("onedir" if ONEDIR else "onefile"))
print("[camab.spec] 生成文件   : %s" % _GEN_DIR)
