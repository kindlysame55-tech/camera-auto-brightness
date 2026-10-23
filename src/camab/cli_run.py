"""run / probe / calibrate 三个命令的实现。"""

from __future__ import annotations

import csv
import json
import logging
import math
import sys
import time
from typing import Optional

from .camera import Camera
from .config import AppConfig
from .engine import Engine
from .lightmeter import LightMeter
from .status import Status

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# run：前台跑闭环
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    cfg = AppConfig.load(args.config)
    from .__main__ import _apply_overrides

    _apply_overrides(cfg, args)
    engine = Engine(cfg)
    if not engine.start():
        print(f"启动失败：{engine.status.message}", file=sys.stderr)
        return 1

    print("按 Ctrl+C 停止。当前策略：")
    print(f"  采样周期 {cfg.engine.interval_s}s｜迟滞 {cfg.mapping.deadband}%｜"
          f"冷却 {cfg.mapping.cooldown_s}s｜单次最大步进 {cfg.mapping.max_step}%")
    print(f"  曲线（分段线性）：{cfg.mapping.clamped().describe_curve()}")
    print(f"  测光分位 {cfg.effective_percentile:.0f}｜"
          f"屏幕反光扣除 {'已标定' if cfg.screen_calibrated else '未标定'}")
    if cfg.engine.dry_run:
        print("  【干跑模式】不会真的改变屏幕亮度")
    print("-" * 78)

    deadline = time.time() + args.duration if args.duration else None
    last_line = ""
    try:
        while engine.running:
            st = engine.status
            if args.json_lines:
                print(json.dumps(_status_dict(st), ensure_ascii=False), flush=True)
            else:
                line = _render(st)
                if line != last_line:
                    print(line, flush=True)
                    last_line = line
            if deadline and time.time() >= deadline:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        engine.stop()
    print("已退出。")
    return 0


def _status_dict(st: Status) -> dict:
    return {
        "state": st.state, "lux": round(st.lux, 2), "ratio": round(st.ratio, 5),
        "current": st.current, "ideal": st.ideal, "applied": st.applied,
        "reason": st.reason, "samples": st.samples,
        "feedback": st.feedback_suspect, "damped": st.damped,
        "ae_drift": round(st.auto_exposure_drift, 3), "writes": st.writes,
        "errors": st.errors, "camera": st.camera_backend, "backend": st.bright_backend,
    }


def _render(st: Status) -> str:
    flags = []
    if st.feedback_suspect:
        flags.append("回流?")
    if st.damped:
        flags.append("阻尼")
    if st.cooling:
        flags.append("冷却")
    if not st.stabilized:
        flags.append("预热")
    return (f"[{st.state:<8}] lux={st.lux:8.2f} 画面={st.ratio:.4f} "
            f"当前={st.current:3d}% 理想={st.ideal:3d}% 写入={str(st.applied):>4} "
            f"写入次数={st.writes:<3d} {st.reason} {' '.join(flags)}")


# --------------------------------------------------------------------------- #
# probe：只测光，不改亮度
# --------------------------------------------------------------------------- #
def cmd_probe(args) -> int:
    cfg = AppConfig.load(args.config)
    from .__main__ import _apply_overrides

    _apply_overrides(cfg, args)

    cam = Camera(index=cfg.camera.effective_source(),
                 size=(cfg.camera.width, cfg.camera.height),
                 fps=cfg.camera.fps, lock_exposure=cfg.camera.lock_exposure,
                 backends=tuple(cfg.camera.backends),
                 black_frame_limit=cfg.camera.black_frame_limit)
    if not cam.open(timeout=cfg.camera.open_timeout_s):
        print(f"摄像头打开失败：{cam.stats.last_error}", file=sys.stderr)
        return 1

    from dataclasses import replace

    meter = LightMeter(analysis=replace(cfg.analysis, percentile=cfg.effective_percentile),
                       calib=cfg.calib, lux_gamma=cfg.lux_gamma)
    print(f"测光 {args.seconds:.0f}s（摄像头后端 {cam.stats.backend}，"
          f"{cam.stats.width}x{cam.stats.height}）。用手遮挡/开灯观察变化：")
    print(f"{'时间':>7} {'lux':>9} {'画面亮度':>9} {'中位':>8} {'P95':>8} "
          f"{'曝光':>9} {'变化率':>8}")
    rows: list[dict] = []
    t0 = time.time()
    prev_lux: Optional[float] = None
    try:
        while time.time() - t0 < args.seconds:
            f = cam.read(timeout=2.0)
            if f is None:
                print("  取帧失败，重试…")
                continue
            est = meter.measure(f.image, f.exposure_s, f.gain)
            a = meter.last_analysis
            rate = 0.0 if prev_lux in (None, 0) else (est.lux - prev_lux) / max(1e-6, prev_lux)
            prev_lux = est.lux
            rows.append({
                "t": round(time.time() - t0, 2), "lux": round(est.lux, 3),
                "ratio": round(est.ratio, 5),
                "median": round(a.median if a else 0, 5),
                "p95": round(a.p95 if a else 0, 5),
                "exposure_s": est.exposure_s, "gain": est.gain,
                "ae_drift": round(est.auto_exposure_drift, 3),
                "rel_change": round(rate, 4),
            })
            print(f"{rows[-1]['t']:7.2f} {est.lux:9.2f} {est.ratio:9.4f} "
                  f"{(a.median if a else 0):8.4f} {(a.p95 if a else 0):8.4f} "
                  f"{(est.exposure_s or 0):9.5f} {rate:+8.1%}")
            time.sleep(max(0.05, 1.0 / max(0.5, cfg.camera.fps)))
    except KeyboardInterrupt:
        pass
    finally:
        cam.close()

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"已写入 CSV：{args.csv}")

    if rows:
        luxes = sorted(r["lux"] for r in rows)
        lo, hi = luxes[0], luxes[-1]
        print(f"\n样本 {len(rows)} 条，lux 范围 {lo:.2f} ~ {hi:.2f}（{hi / max(lo, 1e-6):.1f}×）")
        print("提示：如果遮挡摄像头时 lux 几乎不变，说明摄像头自动曝光在自我补偿，"
              "可尝试 camera.lock_exposure=true 或换用曝光可读的后端。")
    return 0


# --------------------------------------------------------------------------- #
# calibrate-wizard：终端版自动校准（GUI 的等价物）
# --------------------------------------------------------------------------- #
def cmd_calibrate_wizard(args) -> int:
    """一步一步问用户："这个光照下你想要多亮"，然后反推曲线。

    和托盘里的「自动校准」按钮是同一套逻辑（`camab.calibration`），
    只是把滑块换成键盘输入、把实时读数换成每次采样。
    """
    import time
    from dataclasses import replace

    from .brightness import BrightnessError, open_backend
    from .calibration import Sample, build_anchors, suggest_levels
    from .lightmeter import LightMeter

    cfg = AppConfig.load(getattr(args, "config", None))
    from .__main__ import _apply_overrides

    _apply_overrides(cfg, args)

    try:
        backend = open_backend(prefer=cfg.engine.backend, allow_dryrun=False)
    except BrightnessError as exc:
        print(f"没有可用的亮度后端：{exc}", file=sys.stderr)
        return 1
    mon = backend.first()
    if mon is None:
        print("找不到可写亮度的显示器", file=sys.stderr)
        return 1
    original = backend.get(mon.key)

    cam = Camera(index=cfg.camera.effective_source(),
                 size=(cfg.camera.width, cfg.camera.height), fps=cfg.camera.fps,
                 backends=tuple(cfg.camera.backends),
                 black_frame_limit=cfg.camera.black_frame_limit)
    if not cam.open(timeout=cfg.camera.open_timeout_s):
        print(f"摄像头打开失败：{cam.stats.last_error}", file=sys.stderr)
        return 1
    meter = LightMeter(analysis=replace(cfg.analysis, percentile=cfg.effective_percentile),
                       calib=cfg.calib, lux_gamma=cfg.lux_gamma,
                       compensation=cfg.reflection())

    def measure(n: int = 5) -> float:
        vals = []
        for _ in range(n):
            f = cam.read(timeout=3.0)
            if f is None:
                continue
            vals.append(meter.measure(f.image, f.exposure_s, f.gain,
                                      level=backend.get(mon.key)).lux)
            time.sleep(0.3)
        vals.sort()
        return vals[len(vals) // 2] if vals else 0.0

    samples: list[Sample] = []
    print("=" * 66)
    print(" 自动校准（终端版）：你只要说「这个光照下我想要多亮」")
    print("=" * 66)
    print("流程：① 看当前光照读数 ② 输入你想要的亮度 ③ 换个光照再来一次")
    print("      采 2~3 个点后自动算出曲线。中途按 Ctrl+C 退出（不会改配置）。")
    print(f"亮度范围是 {cfg.mapping.floor}~{cfg.mapping.ceil}%，"
          f"也可以直接回车接受建议值。\n")
    try:
        for i in range(args.points):
            lux = measure()
            if lux <= 0.6:
                print(f"\n第 {i+1} 点：测到 {lux:.2f} lx —— 摄像头可能没画面/被挡。")
                print("先跑 `camab devices` 看看。")
                return 1
            sug, _lo = suggest_levels(lux, backend.get(mon.key),
                                      cfg.mapping.floor, cfg.mapping.ceil)
            print(f"\n第 {i+1} 点：当前环境 ≈ {lux:.1f} lx（建议亮度 {sug}%）")
            raw = input(f"  这个光照下你想要多少亮度 %（回车={sug}，q=结束采样）：").strip()
            if raw.lower() in ("q", "quit", "退出"):
                break
            try:
                lv = sug if not raw else int(float(raw))
            except ValueError:
                print("  要输入数字，比如 65")
                continue
            lv = max(cfg.mapping.floor, min(cfg.mapping.ceil, lv))
            backend.set(mon.key, lv)
            print(f"  已把屏幕设成 {lv}%（看看是不是你想要的）")
            keep = input("  记录这个点吗？[Y/n] ").strip().lower()
            if keep in ("", "y", "yes"):
                samples.append(Sample(lux=lux, level=lv, label=f"第{len(samples)+1}点"))
                print(f"  ✔ 已记录：{lux:.1f} lx → {lv}%")
            if i == 0:
                print("\n👉 现在**改变房间光照**（开灯 / 关灯 / 拉窗帘），然后回车继续。")
                try:
                    input()
                except EOFError:
                    break
            if len(samples) >= 2 and i + 1 >= args.points:
                break
    except (KeyboardInterrupt, EOFError):
        print("\n已中断。")
    finally:
        backend.set(mon.key, original)
        cam.close()
        print(f"\n亮度已恢复到 {original}%。")

    res = build_anchors(samples, floor=cfg.mapping.floor, ceil=cfg.mapping.ceil)
    if not res.ok:
        print(f"\n❌ {res.message}")
        return 1
    print(f"\n✅ {res.message}")
    print(f"  新曲线：{res.describe()}")
    print(f"  含义：{res.anchors[0][1]}% ~ {res.anchors[-1][1]}% 之间按你的选择过渡")
    try:
        ans = "y" if getattr(args, "yes", False) else input("保存到配置？[Y/n] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        ans = "n"
    if ans in ("", "y", "yes"):
        cfg.mapping.anchors = res.anchors
        cfg.clamped()
        p = cfg.save(getattr(args, "config", None))
        print(f"已保存：{p}\n重启托盘程序后生效。")
    else:
        print("未保存。")
    return 0


# --------------------------------------------------------------------------- #
# preset：一键切换手感
# --------------------------------------------------------------------------- #
def cmd_stop(args) -> int:
    """优雅停止托盘程序：写停止信号，等进程自己退出。

    别用 Stop-Process -Force —— 强杀会在 Windows 通知区域留下幽灵图标。
    """
    import time as _t

    from .config import stop_flag_path

    flag = stop_flag_path()

    running = _tray_process_count()
    if running == 0:
        # 没在跑就别写信号文件 —— 否则下次启动会被这个残留信号立刻劝退
        # （实测踩到：旧信号留着，新进程一起来就自己退出了）。
        if flag.exists():
            flag.unlink()
            print("托盘程序没在运行；顺手清掉了上次残留的停止信号。")
        else:
            print("托盘程序没在运行。")
        return 0

    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("stop", encoding="utf-8")
    print(f"已发出停止信号（当前 {running} 个进程）：{flag}")

    deadline = _t.time() + float(getattr(args, "timeout", 10.0))
    while _t.time() < deadline:
        if _tray_process_count() == 0:
            print("托盘程序已优雅退出（通知区域图标已注销）。")
            return 0
        _t.sleep(0.4)
    # 超时就撤掉信号，别留着害下一次启动
    if flag.exists():
        flag.unlink()
    print("等待超时：托盘程序可能卡住了。可以看日志，"
          "或在托盘右键菜单里选「退出」。")
    return 1


def _tray_process_count() -> int:
    """当前有几个 CamabTray 进程（拿不到就返回 0）。"""
    try:
        import subprocess

        import os
        import sys

        if os.name != "nt":
            return 0
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq CamabTray.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        return sum(1 for line in out.splitlines() if "CamabTray.exe" in line)
    except Exception:  # noqa: BLE001
        return 0


def cmd_preset(args) -> int:
    from .metrics import PRESETS

    cfg = AppConfig.load(getattr(args, "config", None))
    name = str(getattr(args, "preset_name", "") or "").strip().lower()
    if not name or name == "list":
        print("可用手感预设：")
        for key, p in PRESETS.items():
            print(f"  {key:<10} 周期 {p['interval_s']}s｜迟滞 {p['deadband']}%｜"
                  f"冷却 {p['cooldown_s']}s｜单步 {p['max_step']}%｜"
                  f"速度 {p.get('ramp_per_s', '?')}%/s")
        print(f"\n当前：{cfg.engine.preset}（周期 {cfg.engine.interval_s}s，"
              f"迟滞 {cfg.mapping.deadband}%，冷却 {cfg.mapping.cooldown_s}s，"
              f"单步 {cfg.mapping.max_step}%，变化速度 {cfg.mapping.ramp_per_s:g}%/s）")
        print("用法：camab preset sensitive|balanced|calm")
        return 0

    if not cfg.engine.apply_preset(name):
        print(f"没有这个预设：{name}（可选 {', '.join(PRESETS)}）", file=sys.stderr)
        return 2
    cfg.engine.apply_preset(name)
    cfg.apply_mapping_preset(name)
    cfg.clamped()
    m = cfg.mapping
    path = cfg.save(getattr(args, "config", None))
    print(f"已套用「{name}」手感：")
    print(f"  采样周期 {cfg.engine.interval_s}s｜迟滞 {m.deadband}%｜"
          f"冷却 {m.cooldown_s}s｜单次最大步进 {m.max_step}%｜EMA {m.ema_alpha}")
    print(f"  变化速度 {m.ramp_per_s:g}%/秒｜写入最小间隔 {m.min_write_interval_s:g}s")
    print(f"  曲线（不变）：{m.describe_curve()}")
    print(f"  已保存：{path}")
    print("  重启托盘程序后生效（或在设置面板里核对数值）。")
    return 0


# --------------------------------------------------------------------------- #
# calibrate-screen：标定屏幕反光，扣除"自己造的光"
# --------------------------------------------------------------------------- #
def cmd_calibrate_screen(args) -> int:
    """把屏幕压到两个亮度各测一次，算出屏幕反光系数 k。"""
    import time

    from .brightness import BrightnessError, open_backend
    from .reflection import ReflectionCompensation

    cfg = AppConfig.load(getattr(args, "config", None))
    from .__main__ import _apply_overrides

    _apply_overrides(cfg, args)
    from dataclasses import replace

    from .lightmeter import LightMeter

    try:
        backend = open_backend(prefer=cfg.engine.backend, allow_dryrun=False)
    except BrightnessError as exc:
        print(f"没有可用的亮度后端：{exc}", file=sys.stderr)
        return 1
    mon = backend.first()
    if mon is None:
        print("找不到可写亮度的显示器", file=sys.stderr)
        return 1
    original = backend.get(mon.key)

    cam = Camera(index=cfg.camera.effective_source(),
                 size=(cfg.camera.width, cfg.camera.height),
                 fps=cfg.camera.fps, backends=tuple(cfg.camera.backends),
                 black_frame_limit=cfg.camera.black_frame_limit)
    if not cam.open(timeout=cfg.camera.open_timeout_s):
        print(f"摄像头打开失败：{cam.stats.last_error}", file=sys.stderr)
        return 1

    meter = LightMeter(analysis=replace(cfg.analysis, percentile=cfg.effective_percentile),
                       calib=cfg.calib, lux_gamma=cfg.lux_gamma)
    lows, highs = (args.low, args.high)

    def sample(level: int, n: int) -> float:
        """把屏幕设成 level，稳定后连测 n 帧，返回中位数。"""
        backend.set(mon.key, level)
        time.sleep(args.settle)
        vals = []
        for _ in range(n):
            f = cam.read(timeout=3.0)
            if f is None:
                continue
            est = meter.measure(f.image, f.exposure_s, f.gain)
            vals.append(est.ratio)
            time.sleep(0.25)
        vals.sort()
        return vals[len(vals) // 2] if vals else 0.0

    print("=" * 66)
    print(" 屏幕反光标定（顺手把亮度调两次，约 15 秒）")
    print("=" * 66)
    print(f"当前亮度 {original}%，将依次测试 {lows}% 和 {highs}% 两档。")
    print("⚠️ 标定期间请**不要动**：别开灯关灯、别移动摄像头、别站起来。")
    print("   这个系数算的是「屏幕亮度变化对画面亮度的影响」，只跟摄像头位置有关。")
    if sys.stdin.isatty() and not args.yes:
        try:
            input("\n准备好了按回车开始…")
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            cam.close()
            return 1
    else:
        print("\n（--yes / 非交互环境：直接开始）")

    try:
        print(f"\n① 屏幕调到 {lows}%…")
        r_low = sample(lows, args.frames)
        print(f"   画面亮度 = {r_low:.5f}")
        print(f"② 屏幕调到 {highs}%…")
        r_high = sample(highs, args.frames)
        print(f"   画面亮度 = {r_high:.5f}")
    finally:
        backend.set(mon.key, original)
        cam.close()
        print(f"\n已把亮度恢复到 {original}%。")

    comp = ReflectionCompensation.estimate([(lows, r_low), (highs, r_high)])
    ratio_delta = r_high - r_low
    screen_share = 100.0 * ratio_delta / max(1e-9, r_high)
    print(f"\n屏幕从 {lows}% → {highs}% 时，画面亮度变化 {ratio_delta:+.5f}"
          f"（占高亮时画面的 {screen_share:.0f}%）")
    print(f"结果：{comp.note}")

    if not comp.calibrated:
        print("\n❌ 标定失败，配置未改动。可能原因：")
        print("   · 摄像头没拍到屏幕（对着墙壁/窗外）→ 那其实不用标定，直接跑就好")
        print("   · 两档亮度差别被自动曝光吃掉了 → 试 `--frames 10 --settle 2`")
        print("   · 画面里有别的东西同时在变（有人走动、显示器在放视频）→ 保持静止重试")
        return 1

    print(f"\n✅ 反光系数 k = {comp.k:.6f}（每 1% 屏幕亮度贡献的画面线性亮度）")
    print(f"   含义：以后测光会先把 {screen_share:.0f}% 量级的「自己造的光」扣掉，")
    print("   剩下的才是真正的环境光 → 曲线更线性、响应更灵敏。")
    if not sys.stdin.isatty():
        print("（非交互环境，未保存；可加 --yes 直接保存）")
        if not args.yes:
            return 0
    elif not args.yes:
        try:
            ans = input("保存到配置？[Y/n] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            ans = "n"
        if ans not in ("", "y", "yes"):
            print("未保存。")
            return 0
    cfg.screen_reflection_k = comp.k
    cfg.screen_calibrated = True
    path = cfg.save(getattr(args, "config", None))
    print(f"已保存：{path}")
    print("重启托盘程序后生效。想撤销：camab config set screen_calibrated false")
    return 0


# --------------------------------------------------------------------------- #
# devices：设备清单 + 诊断
# --------------------------------------------------------------------------- #
def cmd_devices(args) -> int:
    from . import devices as dev
    from .camera import Camera

    cfg = AppConfig.load(getattr(args, "config", None))
    from .__main__ import _apply_overrides

    _apply_overrides(cfg, args)

    print("=" * 66)
    print(" 摄像头设备诊断")
    print("=" * 66)

    # 先用 OpenCV 逐个索引试，拿到"能开但全黑"的事实
    opened, means = [], []
    cam = Camera(index=cfg.camera.effective_source(),
                 size=(cfg.camera.width, cfg.camera.height),
                 fps=cfg.camera.fps, backends=tuple(cfg.camera.backends),
                 black_frame_limit=0)
    ok = cam.open(timeout=cfg.camera.open_timeout_s)
    mean = None
    if ok:
        import numpy as np

        vals = []
        for _ in range(5):
            f = cam.read(timeout=2.0)
            if f is not None:
                vals.append(float(np.asarray(f.image).mean()))
        mean = sum(vals) / len(vals) if vals else None
        print(f"当前源：{cfg.camera.effective_source()} → 可打开（后端 {cam.stats.backend}，"
              f"{cam.stats.width}x{cam.stats.height}），平均亮度 "
              f"{'?' if mean is None else f'{mean:.2f}/255'}")
        cam.close()
    else:
        print(f"当前源：{cfg.camera.effective_source()} → 打不开：{cam.stats.last_error}")

    info = dev.full_report(opencv_ok=ok, frame_mean=mean)
    print()
    print(dev.format_text(info))
    print()
    if info.get("severity") == "ok":
        print("✅ 摄像头正常，可以运行：camab run")
        return 0
    print("❌ 摄像头当前不可用于测光，先按上面的建议处理。")
    print("   应急绕过：camab --source \"D:\\某段视频.mp4\" run"
          "  或  camab --source \"http://手机IP:8080/video\" run")
    return 1


# --------------------------------------------------------------------------- #
# calibrate：两点标定
# --------------------------------------------------------------------------- #
def cmd_calibrate(args) -> int:
    cfg = AppConfig.load(args.config)
    from .__main__ import _apply_overrides

    _apply_overrides(cfg, args)

    cam = Camera(index=cfg.camera.effective_source(),
                 size=(cfg.camera.width, cfg.camera.height),
                 fps=cfg.camera.fps, backends=tuple(cfg.camera.backends),
                 black_frame_limit=cfg.camera.black_frame_limit)
    if not cam.open(timeout=cfg.camera.open_timeout_s):
        print(f"摄像头打开失败：{cam.stats.last_error}", file=sys.stderr)
        return 1
    meter = LightMeter(analysis=cfg.analysis, calib=cfg.calib, lux_gamma=cfg.lux_gamma)

    def sample(n: int = 6) -> float:
        vals = []
        for _ in range(n):
            f = cam.read(timeout=3.0)
            if f is None:
                continue
            est = meter.measure(f.image, f.exposure_s, f.gain)
            vals.append(est.lux)
            time.sleep(0.35)
        vals.sort()
        return vals[len(vals) // 2] if vals else 0.0

    print("=" * 60)
    print("两点标定：让工具知道「多暗算暗、多亮算亮」")
    print("=" * 60)
    try:
        print("\n第 1 步：把房间调到你**常用的最暗场景**（关灯/拉窗帘），然后回车。")
        input("  > ")
        dark = sample()
        print(f"  测得暗场景 ≈ {dark:.2f} lx（原始估计）")

        print("\n第 2 步：把房间调到**常用的最亮场景**（开大灯/白天窗前），然后回车。")
        input("  > ")
        bright = sample()
        print(f"  测得亮场景 ≈ {bright:.2f} lx（原始估计）")
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")
        return 1
    finally:
        cam.close()

    if dark <= 0 or bright <= dark:
        print("两次测量差异过小，标定失败。请确保两次光照差别明显。", file=sys.stderr)
        return 1

    # 关键：曲线现在由 mapping.anchors（分段线性折点）决定，
    # 光改 lux_dark/lux_bright 是**空操作**（实测踩到）。
    # 所以这里要真的生成折点：暗场景 → floor，亮场景 → ceil，中间放一个中点。
    mapping = cfg.mapping
    old = mapping.clamped().describe_curve()
    d = round(dark * 1.05, 2)
    b = round(bright * 0.95, 2)
    mid_lux = round(math.sqrt(max(d, 0.01) * max(b, d + 0.01)), 2)
    mid_level = int(round((mapping.floor + mapping.ceil) / 2))
    mapping.anchors = [
        (round(d * 0.4, 2), mapping.floor),   # 更暗的场景保持最低亮度
        (d, mapping.floor),                   # 你测的"最暗场景" = 最低亮度
        (mid_lux, mid_level),                 # 中间点：对数中点
        (b, mapping.ceil),                    # 你测的"最亮场景" = 最高亮度
        (round(b * 2.5, 2), mapping.ceil),    # 更亮也保持最高亮度
    ]
    cfg.clamped()
    print("\n建议曲线（分段线性折点）：")
    print(f"  原来：{old}")
    print(f"  现在：{cfg.mapping.clamped().describe_curve()}")
    print(f"  即：≤{d:.1f} lx 给 {mapping.floor}%，≥{b:.1f} lx 给 {mapping.ceil}%")
    if not sys.stdin.isatty():
        if getattr(args, "auto", False):
            p = cfg.save(args.config)
            print(f"（--auto）已保存：{p}")
            return 0
        print("（非交互环境，未保存；加 --auto 可在无人值守时直接保存）")
        return 0
    ans = input("保存到配置？[Y/n] ").strip().lower()
    if ans in ("", "y", "yes"):
        p = cfg.save(args.config)
        print(f"已保存：{p}")
        print("重启托盘程序后生效。")
    else:
        print("未保存。")
    return 0
