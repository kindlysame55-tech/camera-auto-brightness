"""配置与 CLI 命令的测试（不碰真实屏幕/摄像头）。"""

from __future__ import annotations

import json
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab.brightness import (  # noqa: E402
    BrightnessError, DryRunBackend, detect_backends, open_backend,
)
from camab.config import AppConfig, CameraConfig, default_config_path  # noqa: E402
from camab.status import Status  # noqa: E402


class TestConfig:
    def test_defaults_sane(self):
        c = AppConfig()
        assert 0 <= c.mapping.floor < c.mapping.ceil <= 100
        assert c.mapping.lux_dark < c.mapping.lux_bright
        assert c.engine.interval_s > 0

    def test_roundtrip_json(self, tmp_path: Path):
        c = AppConfig()
        c.mapping.floor = 27
        c.engine.interval_s = 4.5
        c.avoid_screen = False
        p = tmp_path / "config.json"
        c.save(p)
        back = AppConfig.load(p)
        assert back.mapping.floor == 27
        assert back.engine.interval_s == pytest.approx(4.5)
        assert back.avoid_screen is False

    def test_unknown_keys_ignored(self, tmp_path: Path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({
            "mapping": {"floor": 33, "不存在的键": 1},
            "根本没有的段": {"a": 1},
        }, ensure_ascii=False), encoding="utf-8")
        c = AppConfig.load(p)
        assert c.mapping.floor == 33
        assert c.mapping.ceil == 100      # 其它保持默认

    def test_broken_file_falls_back(self, tmp_path: Path):
        p = tmp_path / "config.json"
        p.write_text("{ 这不是 json", encoding="utf-8")
        c = AppConfig.load(p)
        assert c.mapping.ceil == 100

    def test_clamped_on_load(self, tmp_path: Path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"mapping": {"floor": -50, "ceil": 999},
                                 "engine": {"interval_s": 0.0001}}), encoding="utf-8")
        c = AppConfig.load(p)
        assert c.mapping.floor == 0 and c.mapping.ceil == 100
        assert c.engine.interval_s >= 0.5

    def test_save_is_atomic_and_reloadable(self, tmp_path: Path):
        c = AppConfig()
        p = tmp_path / "sub" / "config.json"
        c.save(p)
        assert p.exists() and not p.with_suffix(".json.tmp").exists()
        assert AppConfig.load(p).mapping.floor == c.mapping.floor

    def test_effective_percentile_avoid_screen(self):
        c = AppConfig()
        c.avoid_screen = True
        c.analysis.percentile = 50.0
        assert c.effective_percentile == 35.0
        c.avoid_screen = False
        assert c.effective_percentile == 50.0

    def test_default_path_env_override(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "custom.json"
        monkeypatch.setenv("CAMAB_CONFIG", str(target))
        assert default_config_path() == target

    @pytest.mark.parametrize("data", [None, [], ["camera"], True, 17, "camera"])
    def test_non_object_root_uses_defaults(self, data):
        c = AppConfig.from_dict(data)
        assert c.camera.width == 320
        assert c.mapping.floor == 20

    def test_damaged_fields_preserve_valid_settings(self, tmp_path: Path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({
            "camera": {"width": {"bad": 1}, "height": 480, "source": 17,
                       "backends": "DSHOW"},
            "mapping": {"floor": 37, "ceil": None, "ema_alpha": "bad"},
            "engine": {"interval_s": "4.5", "dry_run": "false"},
            "analysis": [], "calib": [], "avoid_screen": "false",
        }), encoding="utf-8")
        c = AppConfig.load(p)
        assert c.camera.width == 320 and c.camera.height == 480
        assert c.camera.effective_source() == 0
        from camab.platform_support import camera_backends
        assert c.camera.backends == list(camera_backends())
        assert c.mapping.floor == 37 and c.mapping.ceil == 100
        assert c.mapping.ema_alpha == pytest.approx(0.45)
        assert c.engine.interval_s == pytest.approx(4.5)
        assert c.engine.dry_run is False and c.avoid_screen is False
        assert c.analysis.percentile == 15 and c.calib == 1

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), "NaN"])
    def test_nonfinite_fields_recover_individually(self, bad):
        c = AppConfig.from_dict({
            "camera": {"fps": bad, "open_timeout_s": bad},
            "mapping": {"floor": 31, "cooldown_s": bad, "feedback_guard": bad},
            "engine": {"manual_hold_s": bad, "external_hold_s": bad},
            "calib": bad,
        })
        assert c.mapping.floor == 31
        assert c.camera.fps == 2 and c.camera.open_timeout_s == 10
        assert c.mapping.cooldown_s == 4 and c.mapping.feedback_guard == 0.04
        assert c.engine.manual_hold_s == 60 and c.engine.external_hold_s == 20
        assert c.calib == 1

    def test_invalid_anchors_are_removed_without_losing_valid_points(self):
        c = AppConfig.from_dict({"mapping": {"anchors": [
            [float("nan"), 40], [float("inf"), 50], [3, float("inf")],
            None, {"lux": 4}, [15, 35], [60, 70],
        ]}})
        assert c.mapping.anchors == [(15.0, 35), (60.0, 70)]
        assert all(math.isfinite(lux) for lux, _ in c.mapping.curve_points())

    def test_backend_names_and_timing_limits_are_safe(self):
        c = AppConfig.from_dict({
            "camera": {"backends": [" dshow ", None, "unknown", "DSHOW", "any"],
                       "open_timeout_s": -3, "read_timeout_s": -4, "black_frame_limit": -1},
            "engine": {"status_every_s": -2, "external_hold_s": -1,
                       "resume_threshold": 999},
            "mapping": {"floor": 999, "ceil": -1},
        })
        assert c.camera.backends == ["DSHOW", "ANY"]
        assert c.camera.open_timeout_s > 0 and c.camera.read_timeout_s > 0
        assert c.camera.black_frame_limit == 0
        assert c.engine.status_every_s > 0 and c.engine.external_hold_s == 0
        assert c.engine.resume_threshold == 100
        assert 0 <= c.mapping.floor < c.mapping.ceil <= 100

    def test_dataclass_input_is_detached(self):
        source = CameraConfig(width=1, backends=["DSHOW"])
        c = AppConfig.from_dict({"camera": source})
        assert source.width == 1
        assert c.camera.width == 64
        c.camera.backends.append("ANY")
        assert source.backends == ["DSHOW"]

    def test_partial_mapping_keeps_default_curve_within_new_range(self):
        c = AppConfig.from_dict({"mapping": {"floor": 37, "ceil": 83}})
        assert c.mapping.anchors[0] == (2.0, 37)
        assert c.mapping.anchors[-1] == (1200.0, 83)

    def test_snapshot_is_independent_and_does_not_clamp_source(self):
        source = AppConfig()
        source.camera.width = 1
        source.engine.interval_s = 0
        snapshot = source.snapshot()
        assert snapshot.camera.width == 64 and snapshot.engine.interval_s == 0.5
        assert source.camera.width == 1 and source.engine.interval_s == 0
        snapshot.mapping.anchors.append((5000.0, 100))
        snapshot.camera.backends.append("other")
        assert snapshot.mapping.anchors != source.mapping.anchors
        assert snapshot.camera.backends != source.camera.backends

    def test_save_sanitizes_without_modifying_live_config(self, tmp_path: Path):
        source = AppConfig()
        source.calib = float("nan")
        source.camera.width = 1
        p = source.save(tmp_path / "config.json")
        saved = json.loads(p.read_text(encoding="utf-8"),
                           parse_constant=lambda value: pytest.fail(f"非法 JSON 数字：{value}"))
        assert saved["calib"] == 1 and saved["camera"]["width"] == 64
        assert math.isnan(source.calib) and source.camera.width == 1

    def test_load_accepts_windows_utf8_bom(self, tmp_path: Path):
        p = tmp_path / "config.json"
        p.write_text('{"mapping": {"floor": 33}}', encoding="utf-8-sig")
        assert AppConfig.load(p).mapping.floor == 33

    @pytest.mark.parametrize("operation", ["replace", "fsync"])
    def test_failed_save_preserves_previous_file_and_cleans_temp(self, monkeypatch,
                                                                 tmp_path: Path, operation):
        import camab.config as config

        p = tmp_path / "config.json"
        AppConfig().save(p)
        previous = p.read_bytes()
        c = AppConfig()
        c.mapping.floor = 43

        def fail(*args, **kwargs):
            raise OSError("模拟磁盘写入失败")

        monkeypatch.setattr(config.os, operation, fail)
        with pytest.raises(OSError, match="模拟磁盘写入失败"):
            c.save(p)
        assert p.read_bytes() == previous
        assert list(tmp_path.iterdir()) == [p]

    def test_concurrent_saves_use_independent_temporary_files(self, monkeypatch,
                                                            tmp_path: Path):
        import camab.config as config

        p = tmp_path / "config.json"
        barrier = threading.Barrier(2)
        original_replace = config.os.replace
        original_fsync = config.os.fsync
        temporary_paths = []

        def record_replace(src, dst):
            temporary_paths.append(Path(src))
            original_replace(src, dst)

        def simultaneous_fsync(fd):
            original_fsync(fd)
            barrier.wait(timeout=10)

        monkeypatch.setattr(config.os, "replace", record_replace)
        monkeypatch.setattr(config.os, "fsync", simultaneous_fsync)
        first, second = AppConfig(), AppConfig()
        first.mapping.floor, first.engine.interval_s = 27, 4.5
        second.mapping.floor, second.engine.interval_s = 41, 8.0
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(c.save, p) for c in (first, second)]
            assert all(future.result(timeout=15) == p for future in futures)
        assert len(set(temporary_paths)) == 2
        saved = AppConfig.load(p)
        assert (saved.mapping.floor, saved.engine.interval_s) in ((27, 4.5), (41, 8.0))
        assert list(tmp_path.iterdir()) == [p]


class TestBackendSelection:
    def test_dryrun_always_available(self):
        be = open_backend(prefer="dryrun")
        assert isinstance(be, DryRunBackend)
        assert be.get("dryrun:0") == 60
        be.set("dryrun:0", 73)
        assert be.get("dryrun:0") == 73
        assert be.writes[-1][1] == 73

    def test_dryrun_clamps(self):
        be = open_backend(prefer="dryrun")
        assert be.set("dryrun:0", 999) == 100
        assert be.set("dryrun:0", -5) == 0

    def test_unknown_backend_raises(self):
        """`prefer=wmi` 但本机 WMI 不可用时 → 明确报错，而不是偷偷换后端。"""
        if any(b.name == "wmi" for b in detect_backends(only=["wmi"])):
            pytest.skip("本机 WMI 可用，无法构造该场景")
        with pytest.raises(BrightnessError):
            open_backend(prefer="wmi", allow_dryrun=False)

    def test_unknown_name_falls_back_to_auto(self):
        """不认识的 prefer 名字按 auto 处理（宽进严出，不静默失败）。"""
        be = open_backend(prefer="wmi2", allow_dryrun=True)
        assert be is not None
        assert hasattr(be, "get") and hasattr(be, "set")


class TestStatus:
    def test_board_renders(self):
        s = Status(state="running", lux=12.5, current=40, ideal=55, applied=47,
                   reason="测试")
        text = s.board()
        assert "running" in text and "12.5" in text and "47" in text
