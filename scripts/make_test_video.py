"""生成"光照变化"合成视频，用于没有摄像头时验证整条闭环。

生成的视频是一段**灰度均匀画面**，亮度按 10 → 400 lux 的余弦曲线往返
（前半段由暗变亮，后半段由亮变暗），等价于"有人开灯、过一会儿又关灯"。

用法::

    python scripts/make_test_video.py                      # 默认 tmp/light-ramp.mp4
    python scripts/make_test_video.py --seconds 60 --fps 5
    python scripts/make_test_video.py --out demo.mp4 --pattern stripes

依赖 ffmpeg（winget install Gyan.FFmpeg）。用 numpy 逐帧造数据再管道给 ffmpeg，
避免 ffmpeg 表达式求值的各种坑，同时保证像素值与测光公式严格互逆。
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

#: 视频要覆盖的照度范围（lux），与 camab.metrics 的标定锚点一致
DARK_LUX = 10.0
BRIGHT_LUX = 400.0
LUX_REF = 200.0
REF_RATIO = 0.18


def lux_to_srgb(lux: float) -> float:
    """lux → sRGB 编码值（0-1），是 metrics.lux_from_ratio 的逆运算。"""
    ratio = (max(0.001, lux) / LUX_REF) * REF_RATIO
    ratio = max(0.0005, min(0.95, ratio))
    if ratio <= 0.0031308:
        return ratio * 12.92
    return 1.055 * ratio ** (1 / 2.4) - 0.055


def lux_at(t: float, seconds: float, reverse: bool = False) -> float:
    """t 时刻的等效照度：在暗/亮之间余弦往返（reverse 时从亮开始往暗走）。"""
    frac = 0.5 - 0.5 * math.cos(2 * math.pi * t / max(1e-6, seconds))
    if reverse:
        frac = 1.0 - frac
    return math.exp(math.log(DARK_LUX) + (math.log(BRIGHT_LUX) - math.log(DARK_LUX)) * frac)


def make_frame(lux: float, width: int, height: int, pattern: str = "flat") -> np.ndarray:
    """按照度造一帧灰度图（uint8）。"""
    base = lux_to_srgb(lux)
    if pattern == "flat":
        return np.full((height, width), int(round(base * 255)), dtype=np.uint8)
    if pattern == "gradient":
        ramp = np.linspace(0.6, 1.4, width, dtype=np.float32)
        row = np.clip(base * ramp, 0.0, 1.0) * 255.0
        img = np.repeat(row[None, :], height, axis=0)
        return img.astype(np.uint8)
    if pattern == "stripes":
        # 模拟「画面里有亮窗帘 + 暗墙角」：左半亮、右半暗
        img = np.empty((height, width), dtype=np.float32)
        img[:, : width // 2] = min(1.0, base * 1.8)
        img[:, width // 2:] = base * 0.4
        return (np.clip(img, 0, 1) * 255.0).astype(np.uint8)
    raise ValueError(f"未知 pattern：{pattern}")


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    base = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if base.exists():
        hits = sorted(base.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
        if hits:
            return str(hits[-1])
    for p in (Path("C:/ffmpeg/bin/ffmpeg.exe"),
              Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe")):
        if p.exists():
            return str(p)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="生成光照变化的合成测试视频")
    ap.add_argument("--out", default=str(ROOT / "tmp" / "light-ramp.mp4"))
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--fps", type=float, default=5.0)
    ap.add_argument("--width", type=int, default=320)
    ap.add_argument("--height", type=int, default=240)
    ap.add_argument("--pattern", choices=["flat", "gradient", "stripes"], default="flat")
    ap.add_argument("--reverse", action="store_true",
                    help="从最亮开始往最暗走（用来验证「变暗时屏幕应变暗」）")
    args = ap.parse_args()

    ff = find_ffmpeg()
    if not ff:
        print("找不到 ffmpeg。请先安装：winget install Gyan.FFmpeg", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = max(2, int(args.seconds * args.fps))

    cmd = [
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "-s", f"{args.width}x{args.height}", "-r", str(args.fps), "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        for i in range(frames):
            t = i / args.fps
            frame = make_frame(lux_at(t, args.seconds, args.reverse), args.width,
                               args.height, args.pattern)
            assert proc.stdin is not None
            proc.stdin.write(frame.tobytes())
    finally:
        if proc.stdin:
            proc.stdin.close()
    err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
    proc.wait()
    if proc.returncode != 0:
        print("ffmpeg 失败：", err[-800:], file=sys.stderr)
        return 1

    size = out.stat().st_size / 1024
    print(f"已生成：{out}")
    print(f"  {frames} 帧 / {args.seconds:.0f}s @ {args.fps:g}fps / "
          f"{args.width}x{args.height} / {args.pattern} / {size:.0f} KB")
    print(f"  等效照度范围：{DARK_LUX:.0f} → {BRIGHT_LUX:.0f} lux（灰度 "
          f"{int(lux_to_srgb(DARK_LUX) * 255)} → {int(lux_to_srgb(BRIGHT_LUX) * 255)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
