"""把屏幕亮度设成指定值（测试用，跑完记得恢复）。

用法::

    python scripts/set_brightness.py 30      # 设为 30%
    python scripts/set_brightness.py         # 只读当前值
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from camab.brightness import BrightnessError, open_backend  # noqa: E402


def main() -> int:
    try:
        be = open_backend(prefer="auto", allow_dryrun=False)
    except BrightnessError as exc:
        print(f"没有可用的亮度后端：{exc}", file=sys.stderr)
        return 1
    mon = be.first()
    if mon is None:
        print("没找到可寻址的显示器", file=sys.stderr)
        return 1
    before = be.get(mon.key)
    if len(sys.argv) < 2:
        print(f"{mon.name or mon.key} 当前亮度：{before}%")
        return 0
    try:
        target = int(float(sys.argv[1]))
    except ValueError:
        print("亮度要是个数字，比如 30", file=sys.stderr)
        return 2
    applied = be.set(mon.key, target)
    print(f"{mon.name or mon.key}：{before}% → {applied}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
