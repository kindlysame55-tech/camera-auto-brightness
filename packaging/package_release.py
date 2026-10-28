"""Package native builds; run on each target OS after tests and GUI smoke checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist'
OUT = DIST / 'release'
OUT.mkdir(parents=True, exist_ok=True)
stage = DIST / 'portable'
stage.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT / 'src'))
from camab import __version__

readme = '''摄像头自动亮度 / Camera Auto Brightness
Version: {version}

Website: https://kindlysame55-tech.github.io/camera-auto-brightness/
Repository: https://github.com/kindlysame55-tech/camera-auto-brightness

Windows: 解压后双击 start-camab.exe；保留同目录内全部文件。
Linux: 解压后运行 ./CamabTray；内屏需要背光权限或 brightnessctl，外屏需要 ddcutil 和 I²C 权限。需要图形桌面。
macOS: 解压并打开 Camera Auto Brightness.app；先安装 brew install brightness。应用未经过 Apple 公证；若系统拒绝运行，可使用仓库源码指南。

Portable packages include Python. Linux may require system GUI libraries and a CJK font; macOS requires the brightness helper. Supported hardware varies. Camera permissions may need approval in system settings.
Windows uses WMI/DDC; Linux uses backlight/brightnessctl or ddcutil; macOS uses brightness for compatible internal/Apple displays. Ordinary external Mac monitors may not support this interface.

通过原生系统的测试与界面冒烟检查不等于真实摄像头/显示器硬件验证。
Native CI tests and GUI smoke checks do not establish real hardware compatibility.
'''.format(version=__version__)
(stage / 'README.txt').write_text(readme, encoding='utf-8')

if sys.platform == 'win32':
    asset = 'camera-auto-brightness-windows.zip'
    for name in ('Camab.exe', 'CamabTray.exe'):
        shutil.copy2(DIST / name, stage / name)
    shutil.copy2(ROOT / 'start-camab.exe', stage / 'start-camab.exe')
    with zipfile.ZipFile(OUT / asset, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in (stage / name for name in ('README.txt', 'Camab.exe', 'CamabTray.exe', 'start-camab.exe')):
            archive.write(path, path.name)
elif sys.platform == 'darwin':
    arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x86_64'
    asset = f'camera-auto-brightness-macos-{arch}.zip'
    # ditto preserves the .app symlinks and executable bits.
    import subprocess
    bundle = DIST / 'Camera Auto Brightness.app'
    # Do not modify the bundle after PyInstaller signs it. README.md is already
    # included by the spec before signing; ditto only copies the finished app.
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(bundle), str(OUT / asset)], check=True)
else:
    arch = platform.machine().lower()
    asset = f'camera-auto-brightness-linux-{arch}.tar.gz'
    for name in ('Camab', 'CamabTray'):
        shutil.copy2(DIST / name, stage / name)
    with tarfile.open(OUT / asset, 'w:gz') as archive:
        for path in (stage / name for name in ('README.txt', 'Camab', 'CamabTray')):
            archive.add(path, arcname=path.name)

path = OUT / asset
manifest = {'version': __version__, 'file': asset, 'size': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'platform': sys.platform, 'architecture': platform.machine()}
(OUT / f'{asset}.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(json.dumps(manifest, indent=2))
