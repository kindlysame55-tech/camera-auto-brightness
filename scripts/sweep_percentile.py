"""在真机镜头上扫一遍"测光分位"，看哪个分位最能代表房间环境光。

用法::

    python scripts/sweep_percentile.py                 # 每个分位各测 5 帧
    python scripts/sweep_percentile.py --frames 8

输出各分位下的：画面亮度、等效 lux、以及新曲线会给出的屏幕亮度。
判断标准：同一房间不动时数值要稳；把摄像头挡住时要有明显下降。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from camab.camera import Camera  # noqa: E402
from camab.config import AppConfig  # noqa: E402
from camab.metrics import AnalysisConfig, analyze_frame, lux_from_ratio  # noqa: E402

PERCENTILES = (5, 10, 15, 20, 25, 30, 40, 50, 60, 75)


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描测光分位对读数的影响")
    ap.add_argument("--frames", type=int, default=5, help="每个分位取几帧（取中位）")
    ap.add_argument("--source", default=None, help="视频源（默认摄像头）")
    args = ap.parse_args()

    cfg = AppConfig()
    cam = Camera(index=args.source or cfg.camera.effective_source(),
                 size=(cfg.camera.width, cfg.camera.height), fps=cfg.camera.fps,
                 black_frame_limit=0)
    if not cam.open(timeout=cfg.camera.open_timeout_s):
        print(f"打不开摄像头：{cam.stats.last_error}", file=sys.stderr)
        return 1

    # 先把帧取齐，保证每个分位面对的是同一批画面
    frames = []
    for _ in range(args.frames):
        f = cam.read(timeout=3.0)
        if f is not None:
            frames.append(f.image)
    cam.close()
    if not frames:
        print("取不到帧", file=sys.stderr)
        return 1

    mapping = cfg.mapping.clamped()
    print(f"样本 {len(frames)} 帧 / 分辨率 {frames[0].shape[1]}x{frames[0].shape[0]}")
    print(f"曲线：{mapping.describe_curve()}\n")
    print(f"{'分位':>5} {'画面亮度':>10} {'等效lux':>10} {'→屏幕亮度':>10}  说明")
    print("-" * 62)
    for p in PERCENTILES:
        acfg = AnalysisConfig(percentile=float(p), linearize=cfg.analysis.linearize,
                              crop=cfg.analysis.crop, ignore_frac=cfg.analysis.ignore_frac)
        ratios = [analyze_frame(img, acfg).robust for img in frames]
        ratio = float(np.median(ratios))
        lux = lux_from_ratio(ratio, calib=cfg.calib, gamma=cfg.lux_gamma)
        level = mapping.lux_to_level(lux)
        spread = max(ratios) - min(ratios)
        note = []
        if spread > ratio * 0.35:
            note.append("帧间抖动大")
        if p == int(cfg.effective_percentile):
            note.append("← 当前默认")
        print(f"{p:>5} {ratio:>10.4f} {lux:>10.2f} {level:>9}%  {' '.join(note)}")

    print("\n怎么挑：")
    print("  · 想要『跟房间亮度走』：选数值稳定、且遮住镜头时下降明显的那个分位（一般 20~30）")
    print("  · 摄像头正对人/正对屏幕时，分位越低越不容易被脸和屏幕反光带偏")
    print("  · 改默认值：camab config set analysis.percentile 25")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
