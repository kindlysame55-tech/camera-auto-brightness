"""命令行界面测试。

用临时配置文件 + dry-run 后端，确保不碰真实屏幕、不依赖真实摄像头。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camab.__main__ import build_parser, main  # noqa: E402
from camab.config import AppConfig  # noqa: E402


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
class TestParser:
    def test_global_flags_before_subcommand(self):
        args = build_parser().parse_args(["--source", "http://x/video", "--dry-run", "run"])
        assert args.source == "http://x/video" and args.dry_run is True
        assert args.cmd == "run"

    def test_all_subcommands_parse(self):
        p = build_parser()
        for argv in (["preflight"], ["run"], ["tray"], ["probe"], ["calibrate"],
                     ["devices"], ["config", "show"]):
            args = p.parse_args(argv)
            assert args.cmd == argv[0]

    def test_run_options(self):
        args = build_parser().parse_args(["run", "--interval", "2.5", "--backend", "dryrun",
                                          "--duration", "10", "--json-lines"])
        assert args.interval == 2.5 and args.backend == "dryrun"
        assert args.duration == 10 and args.json_lines is True

    def test_tray_selftest_flag(self):
        assert build_parser().parse_args(["tray", "--selftest"]).selftest is True

    def test_probe_defaults(self):
        args = build_parser().parse_args(["probe"])
        assert args.seconds == 20.0 and args.csv is None


# --------------------------------------------------------------------------- #
# config 子命令
# --------------------------------------------------------------------------- #
class TestConfigCommand:
    def test_path_and_show(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "c.json"))
        assert main(["config", "path"]) == 0
        assert str(tmp_path / "c.json") in capsys.readouterr().out

        assert main(["config", "show"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["mapping"]["ceil"] == 100
        assert data["camera"]["source"] == ""

    def test_set_int_and_float(self, tmp_path: Path, monkeypatch, capsys):
        cfg_path = tmp_path / "c.json"
        monkeypatch.setenv("CAMAB_CONFIG", str(cfg_path))
        assert main(["config", "set", "mapping.floor", "33"]) == 0
        assert main(["config", "set", "engine.interval_s", "4.5"]) == 0
        assert main(["config", "set", "avoid_screen", "false"]) == 0
        saved = AppConfig.load(cfg_path)
        assert saved.mapping.floor == 33
        assert saved.engine.interval_s == pytest.approx(4.5)
        assert saved.avoid_screen is False

    def test_set_source_string(self, tmp_path: Path, monkeypatch):
        cfg_path = tmp_path / "c.json"
        monkeypatch.setenv("CAMAB_CONFIG", str(cfg_path))
        assert main(["config", "set", "camera.source", "http://1.2.3.4:8080/video"]) == 0
        assert AppConfig.load(cfg_path).camera.source == "http://1.2.3.4:8080/video"

    def test_set_unknown_key_fails(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "c.json"))
        assert main(["config", "set", "mapping.没有这个", "1"]) == 2
        assert "未知配置项" in capsys.readouterr().err

    def test_reset_writes_defaults(self, tmp_path: Path, monkeypatch):
        cfg_path = tmp_path / "c.json"
        monkeypatch.setenv("CAMAB_CONFIG", str(cfg_path))
        main(["config", "set", "mapping.floor", "40"])
        assert main(["config", "reset"]) == 0
        assert AppConfig.load(cfg_path).mapping.floor == 20

    def test_set_anchors_as_json_array(self, tmp_path: Path, monkeypatch, capsys):
        """回归：折点必须按 JSON 解析。

        早先按逗号切分，会把 [[2,20],[60,60]] 切成字符串，曲线退化成
        2lx→20% 6lx→20%，屏幕一直很暗（docsmith 实测踩到）。
        """
        cfg_path = tmp_path / "c.json"
        monkeypatch.setenv("CAMAB_CONFIG", str(cfg_path))
        assert main(["config", "set", "mapping.anchors", "[[2,20],[60,60],[300,92]]"]) == 0
        cfg = AppConfig.load(cfg_path)
        assert cfg.mapping.curve_points() == [(2.0, 20), (60.0, 60), (300.0, 92)]
        assert cfg.mapping.lux_to_level(60) == 60
        assert "现在生效的曲线" in capsys.readouterr().out

    def test_set_anchors_bad_json_fails_loudly(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "c.json"))
        assert main(["config", "set", "mapping.anchors", "2:20, 60:60"]) == 2
        assert "JSON 数组" in capsys.readouterr().err

    def test_set_floor_below_default_takes_effect(self, tmp_path: Path, monkeypatch):
        """回归：floor 调低必须真的生效（旧实现是单向旋钮）。"""
        cfg_path = tmp_path / "c.json"
        monkeypatch.setenv("CAMAB_CONFIG", str(cfg_path))
        assert main(["config", "set", "mapping.floor", "8"]) == 0
        cfg = AppConfig.load(cfg_path)
        assert cfg.mapping.lux_to_level(0.5) == 8


# --------------------------------------------------------------------------- #
# preset 子命令
# --------------------------------------------------------------------------- #
class TestPresetCommand:
    def test_list(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "c.json"))
        assert main(["preset"]) == 0
        out = capsys.readouterr().out
        assert "sensitive" in out and "balanced" in out and "calm" in out

    def test_apply(self, tmp_path: Path, monkeypatch, capsys):
        cfg_path = tmp_path / "c.json"
        monkeypatch.setenv("CAMAB_CONFIG", str(cfg_path))
        assert main(["preset", "sensitive"]) == 0
        cfg = AppConfig.load(cfg_path)
        assert cfg.engine.preset == "sensitive"
        assert cfg.engine.interval_s == pytest.approx(1.5)
        assert cfg.mapping.deadband == 3
        assert cfg.mapping.ramp_per_s == pytest.approx(30.0), "变化速度要一起套用"
        out = capsys.readouterr().out
        assert "变化速度" in out

    def test_unknown_preset(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "c.json"))
        assert main(["preset", "没有这个"]) == 2
        assert "没有这个预设" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# devices 子命令（能跑通不炸即可，结论看机器实际状态）
# --------------------------------------------------------------------------- #
class TestDevicesCommand:
    def test_runs_and_reports(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setenv("CAMAB_CONFIG", str(tmp_path / "c.json"))
        code = main(["devices"])
        out = capsys.readouterr().out
        assert "摄像头设备诊断" in out
        assert code in (0, 1)          # 取决于本机有没有可用摄像头


# --------------------------------------------------------------------------- #
# run：用 dryrun 后端跑一小段，确认不碰真实亮度
# --------------------------------------------------------------------------- #
class TestRunCommand:
    def test_run_with_video_source_dry(self, tmp_path: Path, monkeypatch, capsys):
        """用合成视频 + dryrun 后端跑 1 秒：必须退出码 0 且打印过状态。"""
        import cv2
        import numpy as np

        clip = tmp_path / "clip.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        w = cv2.VideoWriter(str(clip), fourcc, 5.0, (64, 48))
        assert w.isOpened(), "本机 OpenCV 无法写测试视频"
        for i in range(10):
            val = 20 + i * 12
            w.write(np.full((48, 64, 3), val, np.uint8))
        w.release()

        cfg_path = tmp_path / "c.json"
        cfg = AppConfig()
        cfg.engine.backend = "dryrun"
        cfg.engine.interval_s = 0.6
        cfg.camera.fps = 5.0
        cfg.mapping.cooldown_s = 0.0
        cfg.save(cfg_path)

        code = main(["--config", str(cfg_path), "--source", str(clip),
                     "--dry-run", "run", "--duration", "1.5"])
        out = capsys.readouterr().out
        assert code == 0
        assert "干跑" in out or "干跑模式" in out
