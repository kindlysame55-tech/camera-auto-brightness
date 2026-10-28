"""Platform paths, camera units, numerical equivalence and GUI device changes."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from camab import config, platform_support as platforms
from camab.camera import Camera
from camab.config import AppConfig
from camab.metrics import AnalysisConfig, luma_ratio
from camab.ui_theme import wheel_steps


@pytest.mark.parametrize('platform,expected', [('win32', ('DSHOW', 'MSMF', 'ANY')), ('linux', ('V4L2', 'ANY')), ('darwin', ('AVFOUNDATION', 'ANY'))])
def test_native_camera_defaults_and_imported_windows_settings(monkeypatch, platform, expected):
    monkeypatch.setattr(platforms.sys, 'platform', platform)
    assert tuple(AppConfig().camera.backends) == expected
    assert Camera(backends=('DSHOW', 'MSMF', 'ANY')).backends == expected


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_configuration_uses_native_user_directory(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(config.sys, 'platform', platform)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    expected = tmp_path / ('Library/Application Support' if platform == 'darwin' else '.config') / config.APP_NAME
    assert config.default_config_dir() == expected
    monkeypatch.setenv('CAMAB_CONFIG', str(tmp_path / 'explicit.json'))
    assert config.default_config_path() == tmp_path / 'explicit.json'


def test_linux_respects_absolute_xdg_and_ignores_relative_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(config.sys, 'platform', 'linux')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    assert config.default_config_dir() == tmp_path / 'xdg' / config.APP_NAME
    monkeypatch.setenv('XDG_CONFIG_HOME', 'relative')
    assert config.default_config_dir() == tmp_path / '.config' / config.APP_NAME


def test_linux_device_source_is_not_mistaken_for_a_video_file():
    camera = Camera('/dev/video3')
    assert camera.index == 3 and not camera.is_stream


def test_v4l2_exposure_units_and_manual_mode():
    import cv2
    camera = Camera()
    camera.stats.backend = 'V4L2'
    camera._cap = SimpleNamespace(get=lambda prop: 250 if prop == cv2.CAP_PROP_EXPOSURE else 1)
    exposure, gain = camera._read_exposure()
    assert exposure == pytest.approx(.025) and gain == 1
    assert camera.is_auto_exposure() is False


def test_x11_wheel_buttons_and_macos_trackpad_units():
    assert wheel_steps(number=4)[0] == -1
    assert wheel_steps(number=5)[0] == 1
    assert wheel_steps(delta=3, aqua=True)[0] == -3
    first, remainder = wheel_steps(delta=60)
    second, remainder = wheel_steps(delta=60, remainder=remainder)
    assert first == 0 and second == -1 and remainder == 0


def test_float_normalization_keeps_full_frame_scale_when_cropping():
    frame = np.full((20, 20, 3), 64., dtype=np.float32)
    frame[0, 0, 0] = 2000
    value = luma_ratio(frame, AnalysisConfig(crop=.3, linearize=False, ignore_frac=0))
    assert value.robust == pytest.approx(64/255, abs=1e-7)


@pytest.mark.parametrize('dtype', [np.uint8, np.uint16, np.float32])
def test_analysis_does_not_modify_noncontiguous_input(dtype):
    maximum = np.iinfo(dtype).max if np.issubdtype(dtype, np.integer) else 1.
    base = np.linspace(0, maximum, 6000).reshape(40, 50, 3).astype(dtype)
    frame = base[::2, ::2]
    before = frame.copy()
    result = luma_ratio(frame)
    assert np.array_equal(frame, before)
    assert 0 <= result.p05 <= result.median <= result.p95 <= 1


def test_backend_settings_survive_config_roundtrip(tmp_path):
    cfg = AppConfig.from_dict({'engine': {'backend': 'ddcutil', 'monitor': 'ddcutil:6'},
                              'camera': {'backends': ['V4L2', 'ANY']}})
    path = tmp_path / 'config.json'
    cfg.save(path)
    restored = AppConfig.load(path)
    assert restored.engine.backend == 'ddcutil' and restored.engine.monitor == 'ddcutil:6'
    assert restored.camera.backends == ['V4L2', 'ANY']
