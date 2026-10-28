"""Small platform policies shared by configuration, diagnostics and adapters."""
from __future__ import annotations

import sys

CAMERA_APIS = ('DSHOW', 'MSMF', 'V4L2', 'AVFOUNDATION', 'ANY')
BRIGHTNESS_BACKENDS = ('auto', 'wmi', 'ddc', 'sysfs', 'ddcutil', 'macos', 'dryrun')


def camera_backends(platform: str | None = None) -> tuple[str, ...]:
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        return ('DSHOW', 'MSMF', 'ANY')
    if platform == 'darwin':
        return ('AVFOUNDATION', 'ANY')
    if platform.startswith('linux'):
        return ('V4L2', 'ANY')
    return ('ANY',)


def usable_camera_backends(requested) -> tuple[str, ...]:
    """Preserve explicit local choices; migrate copied defaults from another OS."""
    supported = camera_backends()
    names = tuple(dict.fromkeys(str(name).upper() for name in requested))
    native = tuple(name for name in names if name in supported and name != 'ANY')
    if native:
        return native + (('ANY',) if 'ANY' in names else ())
    # Old Windows settings on Linux/macOS must still try the native camera API.
    return supported if any(name not in supported for name in names) or not names else ('ANY',)


def camera_help(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        return '请在“系统设置 → 隐私与安全性 → 摄像头”中允许本应用（源码运行时为 Terminal/Python），然后重新启动。'
    if platform.startswith('linux'):
        return '请检查 /dev/video* 访问权限、桌面会话的摄像头许可，并关闭占用摄像头的应用。'
    return '请在“设置 → 隐私与安全性 → 相机”中允许桌面应用访问，并关闭占用摄像头的应用。'


def brightness_help(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        return '请安装 brightness（brew install brightness）；支持的内屏/Apple 显示器可调，普通外屏取决于硬件。'
    if platform.startswith('linux'):
        return '内屏请安装 brightnessctl 并确认背光权限；外屏请安装 ddcutil、启用显示器 DDC/CI 并确认 I²C 权限。'
    return '请确认内屏支持 WMI，或外屏已开启 DDC/CI。'
