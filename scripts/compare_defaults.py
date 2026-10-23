"""对比"新默认值"与"你当前保存的配置"在同一真实画面下的差异。

只测光、不写亮度。用来回答："换了新默认值，屏幕会比现在亮还是暗？"

用法::

    python scripts/compare_defaults.py            # 每个配置测 6 帧
    python scripts/compare_defaults.py --frames 10
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from camab.camera import Camera  # noqa: E402
from camab.config import AppConfig  # noqa: E402
from camab.lightmeter import LightMeter  # noqa: E402


def measure(cfg: AppConfig, frames) -> tuple[float, float]:
    meter = LightMeter(analysis=replace(cfg.analysis, percentile=cfg.effective_percentile),
                       calib=cfg.calib, lux_gamma=cfg.lux_gamma,
                       compensation=cfg.reflection())
    luxes, levels = [], []
    for f in frames:
        est = meter.measure(f.image, f.exposure_s, f.gain, level=60)
        luxes.append(est.lux)
        levels.append(cfg.mapping.clamped().lux_to_level(est.lux))
    lux = float(np.median(luxes))
    return lux, float(np.median(levels))


def main() -> int:
    ap = argparse.ArgumentParser(description="对比新旧默认值下的测光与目标亮度")
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--current-level", type=int, default=60, help="当前屏幕亮度（用于反光扣除）")
    args = ap.parse_args()

    saved = AppConfig.load()
    fresh = AppConfig()          # 新默认值

    from camab.config import default_config_path

    print(f"你保存的配置：{default_config_path()}")
    print(f"  分位 {saved.effective_percentile:.0f}｜周期 {saved.engine.interval_s}s｜"
          f"迟滞 {saved.mapping.deadband}%｜冷却 {saved.mapping.cooldown_s}s")
    print(f"  曲线：{saved.mapping.clamped().describe_curve()}")
    print(f"  反光扣除：{'已标定 k=%.6f' % saved.screen_reflection_k if saved.screen_calibrated else '未标定'}")
    print(f"\n新默认值：")
    print(f"  分位 {fresh.effective_percentile:.0f}｜周期 {fresh.engine.interval_s}s｜"
          f"迟滞 {fresh.mapping.deadband}%｜冷却 {fresh.mapping.cooldown_s}s")
    print(f"  曲线：{fresh.mapping.clamped().describe_curve()}")

    cam = Camera(index=fresh.camera.effective_source(),
                 size=(fresh.camera.width, fresh.camera.height), fps=fresh.camera.fps,
                 black_frame_limit=0)
    if not cam.open(timeout=fresh.camera.open_timeout_s):
        print(f"\n打不开摄像头：{cam.stats.last_error}", file=sys.stderr)
        return 1
    frames = []
    for _ in range(args.frames):
        f = cam.read(timeout=3.0)
        if f is not None:
            frames.append(f)
    cam.close()
    if not frames:
        print("取不到帧", file=sys.stderr)
        return 1

    s_lux, s_lvl = measure(saved, frames)
    n_lux, n_lvl = measure(fresh, frames)
    print(f"\n同一画面（{len(frames)} 帧中位）：")
    print(f"  你当前配置   → {s_lux:7.2f} lx → 目标 {s_lvl:.0f}%")
    print(f"  新默认值     → {n_lux:7.2f} lx → 目标 {n_lvl:.0f}%")
    diff = n_lvl - s_lvl
    if abs(diff) < 3:
        print("  结论：亮度基本一致（差异 < 3 个百分点）")
    elif diff > 0:
        print(f"  结论：新默认值下屏幕会**更亮**约 {diff:.0f} 个百分点")
    else:
        print(f"  结论：新默认值下屏幕会**更暗**约 {-diff:.0f} 个百分点")
    print("\n想切到新默认值又保留你的亮度范围：")
    print("  camab config set analysis.percentile 15")
    print("  camab preset balanced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
