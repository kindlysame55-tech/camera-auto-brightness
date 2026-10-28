"""Hardware-free adapter contracts, including permissions, units and hotplug."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from camab import brightness, unix_brightness as unix
from camab.brightness import BrightnessError


def device(root, name='intel_backlight', maximum=937, value=468, kind='raw'):
    path = root / name
    path.mkdir(parents=True)
    for key, val in {'max_brightness': maximum, 'brightness': value, 'type': kind}.items():
        (path / key).write_text(str(val), encoding='ascii')
    return path


def test_sysfs_scales_native_range_and_ignores_power_saving_actual_value(tmp_path):
    path = device(tmp_path)
    (path / 'actual_brightness').write_text('0')
    be = unix.SysfsBackend(root=tmp_path)
    assert be.get('sysfs:intel_backlight') == 50
    assert be.set('sysfs:intel_backlight', 75) == 75
    assert (path / 'brightness').read_text() == '703'
    assert be.set('sysfs:intel_backlight', 999) == 100
    assert be.set('sysfs:intel_backlight', -1) == 0


def test_sysfs_uses_kernel_interface_priority(tmp_path):
    device(tmp_path, 'a_raw')
    device(tmp_path, 'z_firmware', kind='firmware')
    be = unix.SysfsBackend(root=tmp_path)
    assert be.monitors()[0].key == 'sysfs:z_firmware'


def test_unwritable_backlight_is_not_reported_functional_without_helper(tmp_path, monkeypatch):
    device(tmp_path)
    be = unix.SysfsBackend(root=tmp_path)
    be.executable = None
    monkeypatch.setattr(unix.os, 'access', lambda *_: False)
    with pytest.raises(BrightnessError, match='brightnessctl'):
        be.monitors()


def test_sysfs_permission_failure_uses_helper_for_exact_device(tmp_path, monkeypatch):
    path = device(tmp_path)
    be = unix.SysfsBackend(root=tmp_path)
    be.executable = '/usr/bin/brightnessctl'
    original = Path.write_text
    def denied(self, *args, **kwargs):
        if self == path / 'brightness':
            raise PermissionError('denied')
        return original(self, *args, **kwargs)
    calls = []
    def helper(*args):
        calls.append(args)
        original(path / 'brightness', args[-1])
        return ''
    monkeypatch.setattr(Path, 'write_text', denied)
    monkeypatch.setattr(be, '_run', helper)
    assert be.set('sysfs:intel_backlight', 70) == 70
    assert calls == [('--class=backlight', '--device=intel_backlight', '--min-value=0', 'set', '656')]


@pytest.mark.parametrize('key', ['sysfs:../outside', 'sysfs:..', 'sysfs:/etc', 'sysfs:a/b', 'sysfs:a\\b', 'other:device'])
def test_sysfs_rejects_non_device_paths(tmp_path, key):
    with pytest.raises(BrightnessError):
        unix.SysfsBackend(root=tmp_path).set(key, 50)


def test_sysfs_disappearing_device_and_bad_maximum_are_errors(tmp_path):
    be = unix.SysfsBackend(root=tmp_path)
    with pytest.raises(BrightnessError, match='断开'):
        be.get('sysfs:missing')
    device(tmp_path, maximum=0)
    with pytest.raises(BrightnessError, match='范围'):
        be.set('sysfs:intel_backlight', 50)


def test_ddcutil_keeps_bus_identity_scales_max_and_reads_back_once(monkeypatch):
    be = unix.DdcutilBackend()
    calls = []
    current = [1250]
    def run(*args, **_kwargs):
        calls.append(args)
        if args[0] == 'detect':
            return 'Display 1\n I2C bus: /dev/i2c-6\nMonitor: Example\n'
        if 'getvcp' in args:
            return f'VCP 10 C {current[0]} 5000\n'
        current[0] = 2500
        return ''
    monkeypatch.setattr(be, '_run', run)
    assert be.monitors()[0].key == 'ddcutil:6'
    calls.clear()
    assert be.set('ddcutil:6', 50) == 50
    assert calls == [('--bus', '6', 'setvcp', '10', '2500', '--noverify'),
                     ('--bus', '6', 'getvcp', '10', '--terse')]


@pytest.mark.parametrize('text', ['VCP 10 ERR', 'VCP 10 C 50 0', 'VCP 10 SNC x05', 'invalid'])
def test_ddcutil_bad_reply_never_claims_success(text, monkeypatch):
    be = unix.DdcutilBackend()
    monkeypatch.setattr(be, '_run', lambda *_: text)
    with pytest.raises(BrightnessError):
        be.get('ddcutil:8')


MAC_DISPLAYS = '''display 0: main, active, awake, online, built-in, ID 0x123
display 0: brightness 0.500000
display 1: active, awake, online, external, ID 0x456
brightness: unable to get brightness of display 0x456
'''


def test_mac_uses_display_id_not_mutable_index_and_returns_real_readback(monkeypatch):
    be = unix.MacOSBackend()
    be.executable = '/opt/homebrew/bin/brightness'
    calls = []
    def run(*args, **_kwargs):
        calls.append(args)
        return MAC_DISPLAYS.replace('0.500000', '0.720000') if args == ('-l',) else ''
    monkeypatch.setattr(be, '_run', run)
    monitors = be.monitors()
    assert len(monitors) == 1 and monitors[0].key == 'macos:0x123'
    calls.clear()
    assert be.set('macos:0x123', 72) == 72
    assert calls == [('-d', '0x123', '0.720000'), ('-l',)]


def test_mac_hotplug_cannot_apply_to_a_different_screen(monkeypatch):
    be = unix.MacOSBackend()
    be.executable = 'brightness'
    monkeypatch.setattr(be, '_run', lambda *_a, **_kw: MAC_DISPLAYS.replace('0x123', '0x789'))
    with pytest.raises(BrightnessError, match='断开'):
        be.get('macos:0x123')


def test_mac_missing_tool_has_actionable_error(monkeypatch):
    be = unix.MacOSBackend()
    be.executable = None
    with pytest.raises(BrightnessError, match='brew install brightness'):
        be.monitors()


@pytest.mark.parametrize('backend,key', [(unix.DdcutilBackend, 'ddcutil:1; reboot'), (unix.MacOSBackend, 'macos:0x12; reboot')])
def test_command_adapters_reject_injected_selectors(backend, key):
    with pytest.raises(BrightnessError):
        backend().set(key, 50)


def test_tool_timeout_is_bounded_and_does_not_use_a_shell(monkeypatch):
    be = unix.DdcutilBackend(timeout=1.5)
    be.executable = '/usr/bin/ddcutil'
    def run(args, **kwargs):
        assert isinstance(args, list) and kwargs['timeout'] == 1.5
        assert not kwargs.get('shell') and kwargs['env']['LC_ALL'] == 'C'
        raise subprocess.TimeoutExpired(args, 1.5)
    monkeypatch.setattr(unix.subprocess, 'run', run)
    with pytest.raises(BrightnessError, match='调用失败'):
        be.get('ddcutil:6')


@pytest.mark.parametrize('platform,expected', [('win32', ['wmi', 'ddc']), ('linux', ['sysfs', 'ddcutil']), ('darwin', ['macos'])])
def test_factory_only_constructs_current_platform_adapters(monkeypatch, platform, expected):
    monkeypatch.setattr(brightness.sys, 'platform', platform)
    assert [cls.name for cls in brightness.backend_classes()] == expected


def test_foreign_explicit_backend_is_not_silently_replaced(monkeypatch):
    monkeypatch.setattr(brightness.sys, 'platform', 'darwin')
    with pytest.raises(BrightnessError, match='wmi.*darwin'):
        brightness.open_backend('wmi', allow_dryrun=True)
