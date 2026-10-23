"""Named WMI parameters and delayed driver commits; no real hardware."""
from types import SimpleNamespace
import pytest

from camab import brightness
from camab.brightness import BrightnessError, WmiBackend


def test_named_com_write_waits_for_real_driver_commit(monkeypatch):
    values = {name: SimpleNamespace(Value=None) for name in ("Brightness", "Timeout")}
    params = SimpleNamespace(Properties_=values.__getitem__)
    reads = [50, 50, 73]
    calls = []
    def execute(name, received):
        assert name == "WmiSetBrightness" and received is params
        assert values["Brightness"].Value == 73 and values["Timeout"].Value == 0
        calls.append(name)
        return None
    method = SimpleNamespace(Methods_=lambda _: SimpleNamespace(InParameters=SimpleNamespace(SpawnInstance_=lambda: params)),
                             ExecMethod_=execute)
    def query(sql):
        if sql.endswith("Methods"):
            return [method]
        return [SimpleNamespace(CurrentBrightness=reads.pop(0))]
    backend = WmiBackend()
    backend._com_call = lambda operation: operation(SimpleNamespace(ExecQuery=query))
    sleeps = []
    monkeypatch.setattr(brightness.time, "sleep", sleeps.append)
    assert backend.set("internal:0", 73) == 73
    assert calls == ["WmiSetBrightness"] and sleeps == [.02, .02]
    assert backend.verifies_writes and not backend._com_write_disabled


def test_cim_fallback_returns_readback_and_remembers_com_failure():
    backend = WmiBackend()
    attempts, scripts = [], []
    def failure(operation):
        attempts.append(operation)
        raise BrightnessError("COM unavailable")
    backend._com_call = failure
    backend._ps = lambda script: scripts.append(script) or ""
    backend.get = lambda key: 71
    assert backend.set("internal:0", 73) == 71
    assert backend.set("internal:0", 73) == 71
    assert len(attempts) == 1 and len(scripts) == 2


def test_readback_timeout_returns_actual_value_instead_of_claiming_success(monkeypatch):
    backend = WmiBackend()
    prop = SimpleNamespace(Value=None)
    params = SimpleNamespace(Properties_=lambda _: prop)
    method = SimpleNamespace(Methods_=lambda _: SimpleNamespace(InParameters=SimpleNamespace(SpawnInstance_=lambda: params)),
                             ExecMethod_=lambda *_: None)
    backend._com_call = lambda operation: operation(SimpleNamespace(ExecQuery=lambda sql:
        [method] if sql.endswith("Methods") else [SimpleNamespace(CurrentBrightness=50)]))
    clock = [0.0]
    monkeypatch.setattr(brightness.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(brightness.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    assert backend.set("internal:0", 73) == 50
    assert .35 <= clock[0] <= .38


def test_explicit_method_failure_is_not_reported_as_a_successful_small_step():
    backend = WmiBackend()
    prop = SimpleNamespace(Value=None)
    params = SimpleNamespace(Properties_=lambda _: prop)
    method = SimpleNamespace(Methods_=lambda _: SimpleNamespace(InParameters=SimpleNamespace(SpawnInstance_=lambda: params)),
                             ExecMethod_=lambda *_: SimpleNamespace(ReturnValue=5))
    backend._com_call = lambda operation: operation(SimpleNamespace(ExecQuery=lambda _: [method]))
    backend._ps = lambda _: (_ for _ in ()).throw(BrightnessError("CIM failure"))
    with pytest.raises(BrightnessError, match="CIM failure"):
        backend.set("internal:0", 52)
    assert backend._com_write_disabled
