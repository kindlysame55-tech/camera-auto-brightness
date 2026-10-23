"""端到端验证：用合成光序列直接驱动引擎，检查亮度跟随是否正确。

不依赖摄像头、不依赖视频文件、可任意加速。默认跑 **dry-run**（不动屏幕），
加 ``--real`` 才会真的改屏幕亮度（跑完自动恢复原值）。

用法::

    python scripts/verify_tracking.py                  # 干跑，快
    python scripts/verify_tracking.py --seconds 1      # 更快：每拍间隔 1 秒
    python scripts/verify_tracking.py --real           # 真调亮度（会恢复）
    python scripts/verify_tracking.py --json out.json  # 输出逐拍记录

退出码：0 = 全部断言通过；1 = 有断言失败。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from camab.brightness import BrightnessBackend, MonitorInfo, open_backend  # noqa: E402
from camab.camera import Frame  # noqa: E402
from camab.config import AppConfig  # noqa: E402
from camab.engine import Engine  # noqa: E402

#: 与 metrics 标定一致的参考参数
LUX_REF, REF_RATIO = 200.0, 0.18
DARK_LUX, BRIGHT_LUX = 10.0, 400.0


def lux_to_srgb(lux: float) -> float:
    ratio = max(0.0005, min(0.95, (max(0.001, lux) / LUX_REF) * REF_RATIO))
    return ratio * 12.92 if ratio <= 0.0031308 else 1.055 * ratio ** (1 / 2.4) - 0.055


def frame_for(lux: float, size=(48, 64)) -> np.ndarray:
    val = int(round(lux_to_srgb(lux) * 255))
    return np.full((*size, 3), val, dtype=np.uint8)


class LightSequence:
    """按拍给出照度：从最亮平滑降到最暗，再平滑升回最亮。

    用 cos 的**半周期**保证两端连续（``lux(t) = exp(lo + (hi-lo)*(1-cos(2πt/T))/2)``
    的 t∈[0,T] 恰好是"亮→暗→亮"）。
    """

    def __init__(self, ticks: int) -> None:
        self.ticks = max(2, ticks)

    def lux(self, i: int) -> float:
        frac = 0.5 - 0.5 * math.cos(2 * math.pi * i / self.ticks)
        return math.exp(math.log(DARK_LUX) + (math.log(BRIGHT_LUX) - math.log(DARK_LUX)) * frac)


class FakeCam:
    """把合成光序列喂给引擎的假摄像头。"""

    def __init__(self, seq: LightSequence) -> None:
        self.seq = seq
        self.i = 0
        self.stats = type("S", (), {"backend": "SYNTH", "success_rate": 1.0,
                                    "last_error": "", "ok": 0, "fail": 0, "width": 64,
                                    "height": 48})()

    def read(self, timeout: float = 1.0):
        i = min(self.i, self.seq.ticks - 1)
        self.i += 1
        return Frame(frame_for(self.seq.lux(i)), time.time(), None, None, i)

    def open(self, timeout: float = 5.0) -> bool:
        return True

    def close(self) -> None:
        pass


class SimBackend(BrightnessBackend):
    """干跑模式用的**有状态**假屏幕。

    注意：不能用 DryRunBackend —— 它读回的是固定初始值，引擎会把"我写了但读回来没变"
    误判成外部改动，然后一直让出控制权（实测踩到过）。这里让它真的记住写入值。
    """

    name = "sim"
    functional = True

    def __init__(self, initial: int = 60) -> None:
        self.level = int(initial)
        self.writes: list[int] = []

    def available(self) -> bool:
        return True

    def monitors(self):
        return [MonitorInfo(self.name, 0, "sim:0", "模拟屏幕", self.level)]

    def get(self, key: str) -> int:
        return self.level

    def set(self, key: str, level: int) -> int:
        self.level = int(max(0, min(100, level)))
        self.writes.append(self.level)
        return self.level


def main() -> int:
    ap = argparse.ArgumentParser(description="合成光序列端到端跟随验证")
    ap.add_argument("--ticks", type=int, default=40, help="总拍数")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="每拍真实间隔秒数（0 = 极速模式，手动推进冷却时钟）")
    ap.add_argument("--real", action="store_true", help="真的改屏幕亮度（结束恢复原值）")
    ap.add_argument("--json", default=None, help="把逐拍记录写到 JSON")
    args = ap.parse_args()

    cfg = AppConfig()
    cfg.engine.dry_run = False          # 写入交给后端；干跑用 SimBackend 实现
    cfg.engine.interval_s = args.seconds if args.seconds > 0 else 0.05
    cfg.mapping = cfg.mapping.clamped()
    cfg.mapping.cooldown_s = max(0.0, cfg.engine.interval_s)

    if args.real:
        backend = open_backend(prefer="auto", allow_dryrun=False)
        mon = backend.first()
        original = backend.get(mon.key) if mon else None
        if original is not None:
            print(f"真实模式：当前亮度 {original}%，验证结束后会恢复")
            backend.set(mon.key, 95)
            time.sleep(0.3)
    else:
        backend = SimBackend(initial=60)
        mon = backend.first()
        original = None
        print("干跑模式：使用有状态模拟屏幕，不会改你的真实亮度")

    seq = LightSequence(args.ticks)
    cam = FakeCam(seq)

    eng = Engine(cfg, backend=backend)
    eng._camera = cam
    eng._monitor = mon
    eng._t0 = time.monotonic()
    if mon:
        lv = backend.get(mon.key)
        eng._mapper.reset(current=lv)
        eng._last_obs_level = lv
        eng._started_level = lv

    rows = []
    try:
        for i in range(args.ticks):
            st = eng.tick()
            rows.append({"i": i, "lux": round(st.lux, 2), "ideal": st.ideal,
                         "current": st.current, "applied": st.applied,
                         "reason": st.reason, "feedback": st.feedback_suspect})
            if args.seconds <= 0:
                # 极速模式：手动把冷却计时往前推，模拟时间流逝
                eng._mapper.last_write_t -= cfg.mapping.cooldown_s + 0.01
                eng._mapper.force_cool_until = min(eng._mapper.force_cool_until,
                                                   time.monotonic() - 1)
    finally:
        if args.real and original is not None and mon:
            backend.set(mon.key, original)
            print(f"已恢复亮度：{original}%")

    # ---- 断言 ----
    bright_phase = [r["current"] for r in rows if r["lux"] > 250]
    dim_phase = [r["current"] for r in rows if r["lux"] < 30]
    b_avg = sum(bright_phase) / len(bright_phase) if bright_phase else 0
    d_avg = sum(dim_phase) / len(dim_phase) if dim_phase else 0

    checks: list[tuple[str, bool, str]] = []
    checks.append(("有写入发生", eng.status.writes > 0, f"writes={eng.status.writes}"))
    checks.append(("强光时亮度明显高于弱光时", (b_avg - d_avg) > 15,
                   f"强光均 {b_avg:.0f}% vs 弱光均 {d_avg:.0f}%（差 {b_avg - d_avg:+.0f}）"))
    peak = max(r["lux"] for r in rows)
    peak_level = [r["current"] for r in rows if r["lux"] >= peak * 0.95]
    checks.append(("接近最亮时屏幕也接近上限", bool(peak_level) and max(peak_level) >= 70,
                   f"峰值 lux={peak:.0f} 对应亮度 {max(peak_level) if peak_level else '?'}%"))
    dim_end = min(r["current"] for r in rows)
    checks.append(("暗环境下亮度降到低档", dim_end <= 70, f"过程中最低亮度 {dim_end}%"))
    checks.append(("亮度随光照单调响应（无反向）",
                   all(b >= d for b, d in ((b_avg, d_avg),)),
                   f"强光段平均 {b_avg:.0f}% ≥ 弱光段平均 {d_avg:.0f}%"))
    writes = eng.status.writes
    checks.append(("写入次数受控（不是每拍都写）", writes <= args.ticks * 0.8,
                   f"{writes}/{args.ticks} 拍有写入"))
    # 不振荡：方向反转次数有限
    flips = 0
    seq_levels = [r["current"] for r in rows]
    for k in range(2, len(seq_levels)):
        d1 = seq_levels[k - 1] - seq_levels[k - 2]
        d2 = seq_levels[k] - seq_levels[k - 1]
        if d1 * d2 < 0:
            flips += 1
    checks.append(("没有来回摇摆（方向反转 ≤ 3 次）", flips <= 3, f"反转 {flips} 次"))

    print(f"\n{'拍':>3} {'lux':>8} {'理想%':>6} {'当前%':>6} {'写入':>5}  说明")
    for r in rows[:: max(1, args.ticks // 20)]:
        print(f"{r['i']:>3} {r['lux']:>8.1f} {r['ideal']:>6} {r['current']:>6} "
              f"{str(r['applied']):>5}  {r['reason']}")

    print("\n[断言]")
    ok = True
    for name, passed, detail in checks:
        print(f"  {'✅' if passed else '❌'} {name}：{detail}")
        ok = ok and passed

    if args.json:
        Path(args.json).write_text(json.dumps({"rows": rows, "checks": checks},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n逐拍记录已写入：{args.json}")
    print("\n" + ("全部通过 ✅" if ok else "有断言失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
