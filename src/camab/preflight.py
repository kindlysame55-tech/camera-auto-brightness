"""preflight：环境自检。

在真正跑之前，先把「摄像头能不能取帧」「屏幕能不能写亮度」两件事验证清楚，
并给出人类可读的结论与建议 —— 这个项目最容易失败的环节就是这两处。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional

from .brightness import BrightnessError, backend_report, open_backend
from .camera import BACKENDS, probe_in_subprocess

OK = "✅"
WARN = "⚠️ "
BAD = "❌"


def _deps() -> dict:
    out: dict[str, Any] = {}
    for mod in ("cv2", "numpy", "PIL", "pystray", "win32com"):
        try:
            m = __import__(mod)
            out[mod] = {"ok": True, "version": getattr(m, "__version__", "?")}
        except Exception as exc:  # noqa: BLE001
            out[mod] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def _camera(cfg_index: int = 0, timeout: float = 30.0, size=(320, 240)) -> dict:
    t0 = time.time()
    res = probe_in_subprocess(index=cfg_index, size=size, timeout=timeout)
    for r in res.get("results", []):
        r.setdefault("index", res.get("index", cfg_index))
    res["elapsed_s"] = round(time.time() - t0, 2)
    return res


def _brightness(write_test: bool = False) -> dict:
    rep = backend_report()
    rep["write_test"] = None
    if write_test:
        try:
            be = open_backend(prefer="auto", allow_dryrun=False)
            mon = be.first()
            if mon is None:
                rep["write_test"] = {"ok": False, "error": "没有可寻址的显示器"}
            else:
                before = be.get(mon.key)
                target = before - 4 if before > 30 else before + 4
                try:
                    be.set(mon.key, target)
                    time.sleep(0.4)
                    after = be.get(mon.key)
                finally:
                    be.set(mon.key, before)   # 回读失败也必须尝试复原。
                rep["write_test"] = {
                    "ok": abs(after - target) <= 2,
                    "monitor": mon.key, "before": before,
                    "target": target, "after": after, "restored": be.get(mon.key),
                }
        except BrightnessError as exc:
            rep["write_test"] = {"ok": False, "error": str(exc)}
    return rep


def run_preflight(as_json: bool = False, index: Optional[int] = None,
                  timeout: float = 30.0, skip_camera: bool = False,
                  write_test: bool = False) -> int:
    from . import __version__
    from .config import AppConfig, default_config_path
    from . import devices as dev

    cfg = AppConfig.load()
    idx = cfg.camera.index if index is None else int(index)

    py = {
        "version": sys.version.split()[0],
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    deps = _deps()
    bright = _brightness(write_test=write_test)
    cam = {"skipped": True} if skip_camera else _camera(idx, timeout=timeout,
                                                        size=(cfg.camera.width, cfg.camera.height))

    # 摄像头设备诊断（找出"能开但全黑"的真实原因）
    frame_mean = None
    cam_ok = False
    probe_black = None
    if not skip_camera and cam.get("results"):
        best = next((r for r in cam["results"] if r.get("frame")), None)
        if best is not None:
            cam_ok = True
            frame_mean = best.get("mean")
            probe_black = best.get("black")
    devinfo = dev.full_report(opencv_ok=cam_ok and not cam.get("error"),
                              frame_mean=frame_mean if probe_black is None else None)
    black_frames = bool(probe_black) if probe_black is not None else devinfo.get("severity") == "black"
    if black_frames:
        devinfo["severity"] = "black"

    # ---- 结论 ----
    problems: list[str] = []
    notes: list[str] = []
    if not skip_camera:
        if cam.get("error"):
            problems.append(f"摄像头探测报错：{cam['error']}")
        elif not cam_ok:
            problems.append("摄像头能被打开但取不到帧（可能被其它程序占用，或需要在"
                            "「设置 → 隐私 → 相机」里允许桌面应用访问）")
        else:
            best = next(r for r in cam["results"] if r.get("frame"))
            if black_frames:
                problems.append(
                    f"摄像头能开但画面全黑（平均亮度 {best.get('mean')}/255），"
                    "测光拿不到有效数据，等同于没有摄像头"
                )
            else:
                notes.append(f"摄像头可用：后端 {best['backend']}，分辨率 "
                             f"{best['shape'][1]}x{best['shape'][0]}，耗时 {best['seconds']}s")
                if best["backend"] != "DSHOW":
                    notes.append("建议在配置里把 camera.backends 的 DSHOW 放前面（通常更稳）")

    be_names = [b["name"] for b in bright.get("backends", []) if b.get("available")]
    if not be_names:
        problems.append("没有发现可用的亮度后端（WMI / DDC-CI 都失败）")
    else:
        notes.append(f"可用亮度后端：{', '.join(be_names)}")
    wt = bright.get("write_test")
    if write_test and wt is not None and not wt.get("ok", False):
        problems.append(f"亮度写入测试失败：{wt.get('error') or wt}")

    ok = (cam_ok and not black_frames) if not skip_camera else True
    ok = bool(ok) and bool(be_names) and (not write_test or bool(wt and wt.get("ok")))

    if as_json:
        print(json.dumps({
            "version": __version__, "python": py, "deps": deps,
            "camera": cam, "camera_devices": devinfo, "brightness": bright,
            "config_path": str(default_config_path()),
            "conclusion": {"ok": bool(ok), "problems": problems, "notes": notes},
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    # ---- 人类可读 ----
    print("=" * 66)
    print(f" camera-auto-brightness preflight  v{__version__}")
    print("=" * 66)
    print(f" Python : {py['version']}  {py['executable']}")
    print(f" 配置   : {default_config_path()}")
    print("\n[依赖]")
    for name, info in deps.items():
        mark = OK if info["ok"] else (WARN if name in ("pystray", "win32com") else BAD)
        print(f"  {mark} {name:<10} {info.get('version') or info.get('error')}")

    print("\n[摄像头]")
    if skip_camera:
        print("  (已跳过)")
    elif cam.get("error"):
        print(f"  {BAD} {cam['error']}")
    else:
        for r in cam.get("results", []):
            mark = OK if r.get("frame") else BAD
            shape = f"{r['shape'][1]}x{r['shape'][0]}" if r.get("shape") else "-"
            print(f"  {mark} #{r['index']} 后端={r['backend']:<6} 打开={r['opened']} "
                  f"取帧={r['frame']} {shape} {r['seconds']}s {r.get('error') or ''}")
        print(f"  （总耗时 {cam.get('elapsed_s')}s）")

    print("\n[摄像头设备]")
    print(dev.format_text(devinfo))
    if devinfo.get("severity") == "ok":
        print(f"  {OK} 摄像硬件状态正常")

    print("\n[亮度后端]")
    for b in bright.get("backends", []):
        mark = OK if b.get("available") else WARN
        print(f"  {mark} {b['name']:<6} 可用={b['available']}")
        for m in b.get("monitors", []):
            print(f"       └ {m['name'] or m['key']}  当前={m['current']}%")
        if b.get("error"):
            print(f"       └ 错误：{b['error']}")
    if wt is not None:
        print(f"  写入测试：{'通过' if wt.get('ok') else '失败'} {wt}")

    print("\n[结论]")
    for n in notes:
        print(f"  {OK} {n}")
    for p in devinfo.get("problems", []):
        problems.append(p)
    for p in problems:
        print(f"  {BAD} {p}")
    if ok and not problems:
        print(f"  {OK} 一切就绪，可以运行：python -m camab run")
    elif ok:
        print(f"  {WARN} 基本可用，但有告警项")
    else:
        print(f"  {BAD} 存在阻塞项，先按上面的提示处理")
    print("=" * 66)
    return 0 if ok else 1
