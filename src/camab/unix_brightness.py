"""Linux and macOS adapters. No shell, sudo, permission mutation or gamma overlay.

Linux backlight ABI: kernel.org/doc/Documentation/ABI/stable/sysfs-class-backlight
DDC machine format: ddcutil.com/command_getvcp/
macOS CLI contract: github.com/nriley/brightness
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import shutil
import subprocess

from .brightness import BrightnessBackend, BrightnessError, MonitorInfo


def _percent(value: int | float, maximum: int | float) -> int:
    if not math.isfinite(value) or not math.isfinite(maximum) or maximum <= 0 or value < 0:
        raise BrightnessError('设备返回了无效的亮度范围')
    return max(0, min(100, round(value * 100 / maximum)))


def _tool(name: str, *fallbacks: str) -> str | None:
    return shutil.which(name) or next((p for p in fallbacks if Path(p).is_file()), None)


class CommandBackend(BrightnessBackend):
    verifies_writes = True

    def __init__(self, timeout: float = 6.0):
        self.timeout = max(0.1, float(timeout))
        self.executable: str | None = None

    def _run(self, *args: str, partial: bool = False) -> str:
        if not self.executable:
            raise BrightnessError(f'未安装 {self.name} 所需的亮度工具')
        try:
            result = subprocess.run([self.executable, *args], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace',
                                    timeout=self.timeout, env={**os.environ, 'LC_ALL': 'C'})
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrightnessError(f'{self.name} 调用失败：{exc}') from exc
        if result.returncode and not (partial and result.stdout.strip()):
            detail = (result.stderr or result.stdout or f'退出码 {result.returncode}').strip()
            raise BrightnessError(f'{self.name}：{detail[:350]}')
        return result.stdout

    def available(self) -> bool:
        return bool(self.monitors())


class SysfsBackend(CommandBackend):
    """Direct Linux backlight IO, falling back to brightnessctl/logind if needed."""
    name = 'sysfs'

    def __init__(self, timeout: float = 6.0, root: Path | str = '/sys/class/backlight'):
        super().__init__(timeout)
        self.root = Path(root)
        self.executable = _tool('brightnessctl')

    def _device(self, key: str) -> Path:
        prefix, _, name = key.partition(':')
        if prefix != self.name or not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
            raise BrightnessError(f'无效的背光设备：{key}')
        path = self.root / name
        if not path.is_dir():
            raise BrightnessError(f'背光设备已断开：{name}')
        return path

    @staticmethod
    def _read(path: Path) -> int:
        try:
            return int(path.read_text(encoding='ascii').strip())
        except (OSError, ValueError) as exc:
            raise BrightnessError(f'无法读取背光属性 {path.name}：{exc}') from exc

    def monitors(self):
        if not self.root.is_dir():
            return []
        out, denied = [], []
        try:
            paths = sorted(self.root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise BrightnessError(f'无法枚举背光设备：{exc}') from exc
        # The kernel recommends firmware, then platform, then raw when duplicated.
        def priority(p):
            try:
                kind = (p / 'type').read_text().strip()
            except OSError:
                kind = ''
            return ({'firmware': 0, 'platform': 1, 'raw': 2}.get(kind, 3), p.name)
        for path in sorted(paths, key=priority):
            if not path.is_dir():
                continue
            try:
                level = self.get(f'sysfs:{path.name}')
                if not os.access(path / 'brightness', os.W_OK) and not self.executable:
                    denied.append(path.name)
                    continue
                out.append(MonitorInfo(self.name, len(out), f'sysfs:{path.name}', path.name, level))
            except BrightnessError:
                continue
        if not out and denied:
            raise BrightnessError('背光可读但不可写：' + ', '.join(denied) + '。请安装 brightnessctl 并确认当前用户的背光权限。')
        return out

    def get(self, key: str) -> int:
        path = self._device(key)
        # The kernel advises brightness for userspace decisions: actual_brightness
        # can differ because of blanking/power saving and cause false manual holds.
        return _percent(self._read(path / 'brightness'), self._read(path / 'max_brightness'))

    def set(self, key: str, level: int) -> int:
        path = self._device(key)
        maximum = self._read(path / 'max_brightness')
        _percent(0, maximum)
        value = round(max(0, min(100, int(level))) * maximum / 100)
        try:
            (path / 'brightness').write_text(str(value), encoding='ascii')
        except PermissionError as exc:
            if not self.executable:
                raise BrightnessError('没有背光写入权限；请安装 brightnessctl 并确认当前用户的背光权限。') from exc
            self._run('--class=backlight', '--device=' + path.name, '--min-value=0', 'set', str(value))
        except OSError as exc:
            raise BrightnessError(f'背光写入失败：{exc}') from exc
        return self.get(key)


class DdcutilBackend(CommandBackend):
    """Linux DDC/CI with stable I²C bus keys and no discovery during writes."""
    name = 'ddcutil'

    def __init__(self, timeout: float = 6.0):
        super().__init__(timeout)
        self.executable = _tool('ddcutil')
        self._maximum: dict[str, int] = {}

    def _bus(self, key):
        if not re.fullmatch(r'ddcutil:[0-9]+', key):
            raise BrightnessError(f'无效的 DDC/CI 设备：{key}')
        return key.split(':', 1)[1]

    def monitors(self):
        text = self._run('detect', '--brief', partial=True)
        buses = list(dict.fromkeys(re.findall(r'I2C bus:\s*/dev/i2c-(\d+)', text)))
        out = []
        errors = []
        for bus in buses:
            key = f'ddcutil:{bus}'
            try:
                out.append(MonitorInfo(self.name, len(out), key, f'DDC/CI · i2c-{bus}', self.get(key)))
            except BrightnessError as exc:
                errors.append(str(exc))
        if not out and (buses or text.strip()):
            raise BrightnessError('; '.join(errors) or '未发现支持亮度控制的外屏。请检查 DDC/CI 开关和 I²C 权限。')
        return out

    def get(self, key: str) -> int:
        text = self._run('--bus', self._bus(key), 'getvcp', '10', '--terse')
        match = re.search(r'^\s*VCP\s+(?:0x)?10\s+C\s+(\d+)\s+(\d+)\s*$', text, re.M | re.I)
        if not match:
            raise BrightnessError('显示器未返回有效的 VCP 10 亮度；请检查 DDC/CI 和 I²C 权限。')
        current, maximum = map(int, match.groups())
        result = _percent(current, maximum)
        self._maximum[key] = maximum
        return result

    def set(self, key: str, level: int) -> int:
        bus = self._bus(key)
        if key not in self._maximum:
            self.get(key)
        value = round(max(0, min(100, int(level))) * self._maximum[key] / 100)
        # One explicit readback below replaces ddcutil's internal verification.
        # Never return a guessed level if the monitor silently ignores a write.
        self._run('--bus', bus, 'setvcp', '10', str(value), '--noverify')
        return self.get(key)


class MacOSBackend(CommandBackend):
    """Supported internal/Apple displays via nriley/brightness (Intel + Apple Silicon).

    The upstream helper maintains private OS API compatibility. It is deliberately
    not silently downloaded or compiled in the user's app process.
    """
    name = 'macos'

    def __init__(self, timeout: float = 6.0):
        super().__init__(timeout)
        self.executable = _tool('brightness', '/opt/homebrew/bin/brightness', '/usr/local/bin/brightness')

    def monitors(self):
        if not self.executable:
            raise BrightnessError('未安装 macOS brightness 工具。请运行 brew install brightness 后重试。')
        text = self._run('-l', partial=True)
        identities = {int(index): (identity.lower(), '内屏' if 'built-in' in details else '显示器')
                      for index, details, identity in re.findall(r'^display (\d+): (.*), ID (0x[0-9a-fA-F]+)\s*$', text, re.M)}
        out = []
        for index, value in re.findall(r'^display (\d+): brightness ([0-9.eE+\-]+)\s*$', text, re.M):
            idx = int(index)
            if idx not in identities:
                continue
            identity, label = identities[idx]
            try:
                current = _percent(float(value), 1.0)
            except (ValueError, BrightnessError):
                continue
            out.append(MonitorInfo(self.name, idx, f'macos:{identity}', f'macOS {label} {idx}', current))
        return out

    def get(self, key: str) -> int:
        for monitor in self.monitors():
            if monitor.key == key:
                return int(monitor.current)
        raise BrightnessError(f'显示器已断开或不支持 macOS 亮度接口：{key}')

    def set(self, key: str, level: int) -> int:
        if not re.fullmatch(r'macos:0x[0-9a-fA-F]+', key):
            raise BrightnessError(f'无效的 macOS 显示器：{key}')
        target = max(0, min(100, int(level)))
        self._run('-d', key.split(':', 1)[1], f'{target / 100:.6f}')
        return self.get(key)
