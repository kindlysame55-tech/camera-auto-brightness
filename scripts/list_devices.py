"""枚举本机所有可用的视频采集设备（OpenCV 层）。

用途：preflight 发现设备 0 是黑帧时，知道还有没有别的设备可试。

用法::

    python scripts/list_devices.py --max 6
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def enumerate_devices(max_index: int = 6) -> list[dict]:
    """用 OpenCV 逐个索引尝试打开，返回可用设备列表。"""
    found: list[dict] = []
    for idx in range(max_index):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap or not cap.isOpened():
            cap = cv2.VideoCapture(idx, cv2.CAP_ANY)
        if not cap or not cap.isOpened():
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
            continue
        info = {"index": idx, "opened": True, "frames": 0, "mean": None,
                "max": None, "shape": None, "error": None, "name": None}
        try:
            backend_name = cap.getBackendName()
            info["backend"] = backend_name
        except Exception:  # noqa: BLE001
            info["backend"] = "?"
        for _ in range(8):
            try:
                ok, img = cap.read()
            except Exception as exc:  # noqa: BLE001
                info["error"] = f"{type(exc).__name__}: {exc}"
                break
            if ok and img is not None:
                info["frames"] += 1
                mean = float(img.mean())
                info["mean"] = mean if info["mean"] is None else max(info["mean"], mean)
                info["max"] = int(img.max()) if info["max"] is None else max(info["max"], int(img.max()))
                info["shape"] = img.shape
            time.sleep(0.12)
        cap.release()
        found.append(info)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="枚举可用的视频采集设备")
    ap.add_argument("--max", type=int, default=6, help="最多探测到第几个索引")
    args = ap.parse_args()

    devs = enumerate_devices(args.max)
    if not devs:
        print("没有找到任何可打开的视频设备。")
        print("请检查：① 摄像头是否插好/是否被物理遮挡 ② 设备管理器里是否显示为已连接状态")
        return 1

    print(f"{'序号':>4} {'后端':<10} {'分辨率':<12} {'取帧':>4} {'最大均值':>9} {'峰值':>5} 判断")
    print("-" * 78)
    for d in devs:
        shape = f"{d['shape'][1]}x{d['shape'][0]}" if d.get("shape") else "-"
        verdict = "黑帧（不可用）" if (d["mean"] or 0) < 1.0 else "✅ 有画面"
        if d.get("error"):
            verdict = f"报错 {d['error']}"
        print(f"{d['index']:>4} {d.get('backend', '?'):<10} {shape:<12} "
              f"{d['frames']:>4} {(d['mean'] or 0):>9.3f} {d['max'] or 0:>5} {verdict}")

    usable = [d for d in devs if (d["mean"] or 0) >= 1.0]
    print()
    if usable:
        print(f"可用设备序号：{[d['index'] for d in usable]}"
              f"  → 配置里改 camera.index 或命令行加 --index")
    else:
        print("⚠️ 所有设备都只返回黑帧。常见原因：")
        print("   1. 摄像头没接上/被物理挡板遮住（设备状态会显示 Code 45 幽灵设备）")
        print("   2. 房间真的全黑（可以开灯后再测一次确认）")
        print("   3. 其它程序独占了摄像头（微信/腾讯会议/OBS）")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
