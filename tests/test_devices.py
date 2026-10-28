"""设备诊断与摄像头源处理的测试（不依赖真实摄像头）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab import devices as dev  # noqa: E402
from camab.camera import Camera  # noqa: E402
from camab.config import AppConfig  # noqa: E402


# --------------------------------------------------------------------------- #
# 设备分类
# --------------------------------------------------------------------------- #
class TestDeviceInfo:
    def test_real_camera(self):
        d = dev.DeviceInfo(name="HD Webcam", status="OK", cls="Camera",
                           present=True, problem="CM_PROB_NONE")
        assert d.real_camera_present and not d.is_virtual and not d.is_phantom

    def test_phantom_is_not_real(self):
        d = dev.DeviceInfo(name="HD Webcam", status="Unknown", cls="Camera",
                           present=False, problem="CM_PROB_PHANTOM")
        assert d.is_phantom and not d.real_camera_present

    def test_virtual_detected(self):
        for name in ("AMD Privacy View camera", "OBS Virtual Camera",
                     "DroidCam Source 3", "NVIDIA Broadcast", "某某虚拟摄像头"):
            assert dev.DeviceInfo(name=name, present=True).is_virtual, name

    def test_present_but_virtual_is_not_real(self):
        d = dev.DeviceInfo(name="AMD Privacy View camera", status="OK",
                           cls="Camera", present=True, problem="CM_PROB_NONE")
        assert d.is_virtual and not d.real_camera_present


# --------------------------------------------------------------------------- #
# 结论生成
# --------------------------------------------------------------------------- #
def rep_with(*devs, dshow=()):
    r = dev.DeviceReport(devices=list(devs))
    r.dshow_names = list(dshow)
    return r


class TestAnalyze:
    def test_healthy(self):
        r = rep_with(dev.DeviceInfo(name="HD Webcam", status="OK", cls="Camera",
                                    present=True, problem="CM_PROB_NONE"))
        info = dev.analyze(r, opencv_ok=True, frame_mean=60.0)
        assert info["severity"] == "ok"
        assert info["real_cameras"] == 1 and not info["problems"]

    def test_phantom_plus_virtual_gives_actionable_advice(self):
        """本机实测场景：内置摄像头是幽灵设备，只有 AMD Privacy View 虚拟摄像头。"""
        r = rep_with(
            dev.DeviceInfo(name="HD Webcam", status="Unknown", cls="Camera",
                           present=False, problem="CM_PROB_PHANTOM"),
            dev.DeviceInfo(name="AMD Privacy View camera", status="OK", cls="Camera",
                           present=True, problem="CM_PROB_NONE"),
        )
        info = dev.analyze(r, opencv_ok=True, frame_mean=0.25)
        assert info["severity"] == "black"
        assert info["phantom_cameras"] == 1
        assert info["virtual_cameras"] == 1
        blob = " ".join(info["problems"] + info["advice"])
        assert "Code 45" in blob or "未连接" in blob
        assert "BIOS" in blob
        assert "手机" in blob           # 给了绕过方案
        assert "全黑" in blob

    def test_dshow_only_virtual_camera_is_recorded(self, monkeypatch):
        """DirectShow 里能看到的软设备要并入设备列表并识别为虚拟摄像头。"""
        monkeypatch.setattr(dev.sys, "platform", "win32")
        monkeypatch.setattr(dev, "enumerate_windows_devices", lambda *a, **k: rep_with(
            dev.DeviceInfo(name="HD Webcam", status="Unknown", cls="Camera",
                           present=False, problem="CM_PROB_PHANTOM")))
        monkeypatch.setattr(dev, "dshow_device_names",
                            lambda *a, **k: ["AMD Privacy View camera"])
        info = dev.full_report(opencv_ok=True, frame_mean=0.2)
        assert info["virtual_cameras"] == 1
        assert any(d["name"] == "AMD Privacy View camera" for d in info["devices"])
        assert info["severity"] == "black"

    def test_no_devices_at_all(self):
        info = dev.analyze(rep_with(), opencv_ok=False)
        assert info["severity"] == "none"
        assert any("USB" in a for a in info["advice"])

    def test_black_frame_with_healthy_device(self):
        r = rep_with(dev.DeviceInfo(name="HD Webcam", status="OK", cls="Camera",
                                    present=True, problem="CM_PROB_NONE"))
        info = dev.analyze(r, opencv_ok=True, frame_mean=0.1)
        assert info["severity"] == "black"
        assert any("遮挡" in a or "隐私" in a for a in info["advice"])

    def test_enumeration_error_reported(self):
        r = dev.DeviceReport(error="PowerShell 挂了")
        info = dev.analyze(r, opencv_ok=False)
        assert info["severity"] == "unknown"
        assert "PowerShell" in info["problems"][0]

    def test_format_text_has_marks(self):
        r = rep_with(
            dev.DeviceInfo(name="HD Webcam", status="Unknown", cls="Camera",
                           present=False, problem="CM_PROB_PHANTOM"),
            dev.DeviceInfo(name="AMD Privacy View camera", status="OK", cls="Camera",
                           present=True, problem="CM_PROB_NONE"),
        )
        text = dev.format_text(dev.analyze(r, opencv_ok=True, frame_mean=0.2))
        assert "虚拟摄像头" in text and "幽灵设备" in text


# --------------------------------------------------------------------------- #
# 摄像头源
# --------------------------------------------------------------------------- #
class TestCameraSource:
    def test_index_source(self):
        c = Camera(0)
        assert c.index == 0 and c.is_stream is False

    def test_file_source(self, tmp_path: Path):
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00")
        c = Camera(str(p))
        assert c.is_stream and c.index == -1

    def test_http_source(self):
        c = Camera("http://192.168.1.5:8080/video")
        assert c.is_stream and c.source.startswith("http")

    def test_numeric_string_is_index(self):
        c = Camera("1")
        assert c.is_stream is False and c.index == 1

    def test_config_effective_source(self):
        cfg = AppConfig()
        assert cfg.camera.effective_source() == 0
        cfg.camera.index = 2
        assert cfg.camera.effective_source() == 2
        cfg.camera.source = "http://1.2.3.4:8080/video"
        assert cfg.camera.effective_source() == "http://1.2.3.4:8080/video"

    def test_config_source_roundtrip(self, tmp_path: Path):
        cfg = AppConfig()
        cfg.camera.source = "rtsp://cam/stream"
        cfg.camera.black_frame_limit = 12
        p = tmp_path / "c.json"
        cfg.save(p)
        back = AppConfig.load(p)
        assert back.camera.source == "rtsp://cam/stream"
        assert back.camera.black_frame_limit == 12


# --------------------------------------------------------------------------- #
# 黑帧检测
# --------------------------------------------------------------------------- #
class TestBlackFrameGuard:
    def test_black_streak_accumulates(self):
        c = Camera(0, black_frame_limit=3)
        black = np.zeros((10, 10, 3), np.uint8)
        for _ in range(2):
            c._check_black(black)
        assert c.stats.black_streak == 2
        assert "全黑" not in c.stats.last_error
        c._check_black(black)                    # 第 3 帧触发告警
        assert c.stats.black_streak == 3
        assert "全黑" in c.stats.last_error

    def test_normal_frame_resets_streak(self):
        c = Camera(0, black_frame_limit=3)
        c._check_black(np.zeros((10, 10, 3), np.uint8))
        c._check_black(np.full((10, 10, 3), 40, np.uint8))
        assert c.stats.black_streak == 0
        assert c.stats.last_peak == 40

    def test_dark_but_not_black_is_ok(self):
        """真正的暗环境（峰值 12）不该被误报为黑帧。"""
        c = Camera(0, black_frame_limit=2)
        c._check_black(np.full((10, 10, 3), 12, np.uint8))
        c._check_black(np.full((10, 10, 3), 12, np.uint8))
        assert c.stats.black_streak == 0

    def test_guard_can_be_disabled(self):
        c = Camera(0, black_frame_limit=0)
        for _ in range(5):
            c._check_black(np.zeros((10, 10, 3), np.uint8))
        assert c.stats.black_streak == 0
