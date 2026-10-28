"""`python -m camab` 的多命令入口。"""

from __future__ import annotations

import argparse
import json
import sys
from .platform_support import BRIGHTNESS_BACKENDS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="camab",
        description="摄像头自动亮度：用摄像头当环境光传感器，自动调节屏幕亮度。",
    )
    p.add_argument("--config", help="配置文件路径（默认使用当前系统的用户配置目录；config path 可查看）")
    p.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    p.add_argument("--log-file", default=None, help="同时写入日志文件")
    p.add_argument("--index", type=int, default=None, help="摄像头序号（可被子命令覆盖）")
    p.add_argument("--source", default=None,
                   help="视频源：视频文件路径 / http(s) MJPEG / rtsp 地址（优先于 --index）")
    p.add_argument("--dry-run", action="store_true", help="只记录不真的改亮度（可被子命令覆盖）")
    sub = p.add_subparsers(dest="cmd", required=True)

    # preflight ---------------------------------------------------------
    pf = sub.add_parser("preflight", help="环境自检：摄像头 + 亮度后端")
    pf.add_argument("--json", action="store_true", help="输出 JSON")
    pf.add_argument("--timeout", type=float, default=30.0, help="单次探测超时秒数")
    pf.add_argument("--no-camera", action="store_true", help="跳过摄像头探测")
    pf.add_argument("--write-test", action="store_true",
                    help="真的写一次亮度并回读（会上下动一次屏幕，默认只读）")

    # run ---------------------------------------------------------------
    run = sub.add_parser("run", help="前台运行自动调光")
    run.add_argument("--interval", type=float, default=None, help="采样周期（秒）")
    run.add_argument("--backend", choices=BRIGHTNESS_BACKENDS, default=None)
    run.add_argument("--monitor", default=None, help="显示器标识（preflight --json 可查看；省略则自动选择）")
    run.add_argument("--duration", type=float, default=0.0, help="跑多少秒后自动退出（0=一直跑）")
    run.add_argument("--json-lines", action="store_true", help="每行输出一条 JSON 状态")

    # tray --------------------------------------------------------------
    tr = sub.add_parser("tray", help="托盘常驻 + 设置面板")
    tr.add_argument("--selftest", action="store_true",
                    help="自检模式：建好界面渲染几帧就退出，不留在桌面上")
    tr.add_argument("--screenshot", default=None, metavar="PNG",
                    help="自检模式下把窗口截图存到指定 png（写文档用）")
    tr.add_argument("--calib-screenshot", default=None, metavar="PNG",
                    help="自检模式下额外打开校准向导并截图")
    tr.add_argument("--settings-screenshot", default=None, metavar="PNG",
                    help="自检模式下把设置面板（含自动校准组）截图")

    # stop --------------------------------------------------------------
    sp = sub.add_parser("stop", help="优雅停止后台的托盘程序（不会留幽灵图标）")
    sp.add_argument("--timeout", type=float, default=10.0, help="最多等多少秒")

    # calibrate ---------------------------------------------------------
    cal = sub.add_parser("calibrate", help="标定：按提示在亮/暗环境下测光，生成曲线参数")
    cal.add_argument("--auto", action="store_true", help="不询问，直接用当前光照做单点校准")

    # calibrate-wizard --------------------------------------------------
    cw = sub.add_parser("calibrate-wizard",
                        help="自动校准（推荐）：你说想要多亮，软件反推整条曲线")
    cw.add_argument("--points", type=int, default=3, help="采集几个点（默认 3，最少 2）")
    cw.add_argument("--yes", action="store_true", help="算完直接保存，不询问")

    # calibrate-screen --------------------------------------------------
    cs = sub.add_parser("calibrate-screen",
                        help="标定屏幕反光（把屏幕压暗测一次，之后可扣除屏幕自身的光）")
    cs.add_argument("--low", type=int, default=25, help="低档屏幕亮度（默认 25）")
    cs.add_argument("--high", type=int, default=90, help="高档屏幕亮度（默认 90）")
    cs.add_argument("--frames", type=int, default=6, help="每档测几帧（默认 6）")
    cs.add_argument("--settle", type=float, default=1.2, help="换档后稳定等待秒数")
    cs.add_argument("--yes", action="store_true", help="算完直接保存，不询问")

    # probe -------------------------------------------------------------
    pr = sub.add_parser("probe", help="只测光：连续打印环境照度估计，不改亮度")
    pr.add_argument("--seconds", type=float, default=20.0)
    pr.add_argument("--csv", default=None, help="把测光序列写入 CSV")

    # devices -----------------------------------------------------------
    sub.add_parser("devices", help="列出摄像头设备并给出诊断结论")

    # preset ------------------------------------------------------------
    pre = sub.add_parser("preset", help="切换调光手感（sensitive/balanced/calm）")
    pre.add_argument("preset_name", nargs="?", default=None,
                     help="sensitive / balanced / calm（省略则只列出当前值）")

    # config ------------------------------------------------------------
    cfgp = sub.add_parser("config", help="查看/修改配置")
    cfgp.add_argument("action", choices=["show", "path", "reset", "set"])
    cfgp.add_argument("key", nargs="?", help="例如 mapping.floor")
    cfgp.add_argument("value", nargs="?", help="新值")
    return p


def _apply_overrides(cfg, args) -> None:
    if getattr(args, "dry_run", False):
        cfg.engine.dry_run = True
    if getattr(args, "interval", None):
        cfg.engine.interval_s = float(args.interval)
    if getattr(args, "backend", None):
        cfg.engine.backend = args.backend
    if getattr(args, "monitor", None) is not None:
        cfg.engine.monitor = args.monitor
    if getattr(args, "source", None):
        cfg.camera.source = str(args.source)
    if getattr(args, "index", None) is not None:
        cfg.camera.index = int(args.index)
    cfg.clamped()


def cmd_config(args) -> int:
    from .config import AppConfig, default_config_path

    if args.action == "path":
        print(default_config_path())
        return 0
    if args.action == "reset":
        p = default_config_path()
        if p.exists():
            p.unlink()
        AppConfig().save()
        print(f"已重置为默认配置：{p}")
        return 0
    cfg = AppConfig.load(args.config)
    if args.action == "show":
        print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
        return 0
    # set
    if not args.key or args.value is None:
        print("用法：camab config set mapping.floor 25", file=sys.stderr)
        return 2
    parts = args.key.split(".")
    obj = cfg
    for part in parts[:-1]:
        if not hasattr(obj, part):
            print(f"未知配置段：{part}", file=sys.stderr)
            return 2
        obj = getattr(obj, part)
    leaf = parts[-1]
    if not hasattr(obj, leaf):
        print(f"未知配置项：{args.key}", file=sys.stderr)
        return 2
    cur = getattr(obj, leaf)
    try:
        if isinstance(cur, bool):
            val = str(args.value).lower() in ("1", "true", "yes", "on", "是")
        elif isinstance(cur, int):
            val = int(float(args.value))
        elif isinstance(cur, float):
            val = float(args.value)
        elif isinstance(cur, list):
            # 列表必须按 JSON 解析：早先按逗号切会把 [[2,20],[60,60]] 切坏，
            # 曲线会退化成 2lx→20% 6lx→20%，屏幕一直很暗（实测踩到）。
            raw = str(args.value).strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"列表类配置要写成 JSON 数组，例如："
                      f"mapping.anchors \"[[2,20],[60,60]]\"（解析失败：{exc}）", file=sys.stderr)
                return 2
            if not isinstance(parsed, list):
                print(f"{args.key} 需要一个 JSON 数组", file=sys.stderr)
                return 2
            val = parsed
        else:
            val = args.value
    except ValueError as exc:
        print(f"值不合法：{exc}", file=sys.stderr)
        return 2
    setattr(obj, leaf, val)
    cfg.clamped()
    p = cfg.save(args.config)
    print(f"{args.key} = {val!r} → 已写入 {p}")
    if args.key.startswith("mapping."):
        print(f"现在生效的曲线：{cfg.mapping.clamped().describe_curve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from .status import setup_logging, default_log_path

    args = build_parser().parse_args(argv)
    if args.config:
        import os
        from pathlib import Path
        # All commands, logging and stop/show IPC must use the same explicit file.
        os.environ['CAMAB_CONFIG'] = str(Path(args.config).expanduser().resolve())
    setup_logging(args.log_level or "INFO", args.log_file or default_log_path())

    if args.cmd == "config":
        return cmd_config(args)

    if args.cmd == "preflight":
        from .preflight import run_preflight

        return run_preflight(as_json=args.json, index=args.index,
                             timeout=args.timeout, skip_camera=args.no_camera,
                             write_test=args.write_test)

    if args.cmd == "probe":
        from .cli_run import cmd_probe

        return cmd_probe(args)

    if args.cmd == "devices":
        from .cli_run import cmd_devices

        return cmd_devices(args)

    if args.cmd == "preset":
        from .cli_run import cmd_preset

        return cmd_preset(args)

    if args.cmd == "calibrate":
        from .cli_run import cmd_calibrate

        return cmd_calibrate(args)

    if args.cmd == "calibrate-wizard":
        from .cli_run import cmd_calibrate_wizard

        return cmd_calibrate_wizard(args)

    if args.cmd == "calibrate-screen":
        from .cli_run import cmd_calibrate_screen

        return cmd_calibrate_screen(args)

    if args.cmd == "run":
        from .cli_run import cmd_run

        return cmd_run(args)

    if args.cmd == "tray":
        from .tray import run_tray

        return run_tray(args)

    if args.cmd == "stop":
        from .cli_run import cmd_stop

        return cmd_stop(args)

    print(f"未知命令：{args.cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
