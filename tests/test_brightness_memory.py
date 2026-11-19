"""Regression coverage for preferences and transient-light rejection."""

from camab.config import AppConfig
from camab.engine import Engine
from camab.metrics import BrightnessMapper


def test_brightness_memory_survives_config_reload_and_stays_local(tmp_path):
    config = AppConfig()
    before = config.mapping.lux_to_level(100)
    far = config.mapping.lux_to_level(10)
    config.mapping.remember(100, before + 10)
    path = config.save(tmp_path / 'config.json')
    mapping = AppConfig.load(path).mapping
    assert BrightnessMapper(mapping).decide(105, before).target == before + 10
    assert mapping.preferred_level(10) == far
    mapping.remember(103, before - 7)
    assert len(mapping.preferences) == 1
    assert mapping.preferred_level(103) == before - 7
    mapping.remember_manual = False
    assert mapping.preferred_level(103) == mapping.lux_to_level(103)


def test_ambient_filter_rejects_alternating_spikes_but_accepts_real_changes():
    engine = Engine(AppConfig())
    assert engine._confirmed_lux(60) == 60
    for lux in (900, 3, 900, 3, 62):
        assert 60 <= engine._confirmed_lux(lux) <= 62
    assert engine._confirmed_lux(160) == 62
    assert engine._confirmed_lux(170) == 170
    assert engine._confirmed_lux(60) == 170
    assert engine._confirmed_lux(57) == 57
