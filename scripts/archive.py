"""里程碑存档脚本（autosave / seal）。

一条命令完成三件事，保证「做崩了也能回到上一版」：

1. ``git add -A`` + ``git commit``（提交信息带阶段号）
2. ``git tag vX.Y``（可 ``git checkout vX.Y`` 回退）
3. 把当前**全量源码**拷贝到 ``archive/vX.Y/``，并写 ``archive/vX.Y/_meta.json``
   （含 commit hash、时间、文件数、关键配置快照）
4. 追加一行到 ``archive/INDEX.md``

用法::

    python scripts/archive.py --stage v0.1 --label "亮度后端+测光+映射引擎"
    python scripts/archive.py --stage v0.2 --message "feat: 托盘 GUI" --no-tag
    python scripts/archive.py --probe            # 看看下一版号是多少
    python scripts/archive.py --list             # 列出所有存档
"""

from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

PROJ = Path(__file__).resolve().parent.parent
ARCHIVE = PROJ / "archive"
MANIFEST = PROJ / "archive-manifest.json"

EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "tmp", "archive",
    ".pytest_cache", "build", "dist", "node_modules", ".mypy_cache",
}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".spec"}


# --------------------------------------------------------------------------- #
# git 辅助
# --------------------------------------------------------------------------- #
def git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=PROJ, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return (proc.stdout or "").strip()


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], cwd=PROJ, capture_output=True).returncode == 0


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #
def load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"versions": []}


def save_manifest(data: dict) -> None:
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def version_key(v: str) -> tuple:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) if nums else (0,)


def next_version(manifest: dict) -> str:
    vers = [e["version"] for e in manifest.get("versions", [])]
    if not vers:
        return "v0.1"
    last = max(vers, key=version_key)
    parts = re.findall(r"\d+", last)
    if not parts:
        return "v0.1"
    major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    return f"v{major}.{minor + 1}"


# --------------------------------------------------------------------------- #
# 拷贝
# --------------------------------------------------------------------------- #
def iter_files(root: Path = PROJ) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]) or rel.parts[0] in EXCLUDE_DIRS:
            continue
        if p.name in EXCLUDE_FILES or p.suffix in EXCLUDE_SUFFIX:
            continue
        if p.name.endswith(".json.tmp"):
            continue
        out.append(p)
    return out


def copy_snapshot(dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in iter_files():
        rel = f.relative_to(PROJ)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        n += 1
    return n


def config_snapshot() -> dict:
    """把用户配置目录里的 config.json 也存一份（不影响运行）。"""
    import os

    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return {}
    p = Path(base) / "camera-auto-brightness" / "config.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"_error": "解析失败"}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def probe() -> int:
    m = load_manifest()
    nxt = next_version(m)
    print(f"当前存档数：{len(m.get('versions', []))}")
    print(f"下一个版本号：{nxt}")
    if git_ok("rev-parse", "HEAD"):
        print(f"HEAD: {git('log', '-1', '--oneline')}")
    dirty = git("status", "--porcelain")
    print(f"未提交改动：{len(dirty.splitlines())} 个文件")
    return 0


def listing() -> int:
    m = load_manifest()
    if not m.get("versions"):
        print("还没有任何存档。先跑：python scripts/archive.py --stage v0.1 --label \"第一个里程碑\"")
        return 0
    print(f"{'版本':<8} {'时间':<17} {'文件':>4}  {'标签':<10} 说明")
    print("-" * 88)
    for e in m["versions"]:
        print(f"{e['version']:<8} {e['time'][:16]:<17} {e.get('files', 0):>4}  "
              f"{e.get('tag') or '-':<10} {e.get('label', '')}")
    print(f"\n快照目录：{ARCHIVE}")
    return 0


def seal(stage: Optional[str], label: str, message: Optional[str],
         make_tag: bool, allow_empty: bool) -> int:
    if not git_ok("rev-parse", "--git-dir"):
        raise SystemExit("这里不是 git 仓库。先运行 git init。")

    manifest = load_manifest()
    version = stage or next_version(manifest)
    if version in [e["version"] for e in manifest.get("versions", [])]:
        raise SystemExit(f"版本 {version} 已存在，换个号或删除 archive/{version}。")

    # 1) 提交
    changes = git("status", "--porcelain")
    committed = False
    if changes:
        git("add", "-A")
        msg = message or f"{version}: {label or '里程碑存档'}"
        commit_out = git("commit", "-q", "-m", msg)
        committed = True
        print(f"✔ 已提交：{msg}")
    elif allow_empty:
        git("commit", "-q", "--allow-empty", "-m", message or f"{version}: 空存档")
        committed = True
        print("✔ 已创建空提交")
    else:
        print("· 没有改动需要提交（继续做快照）")

    head = git("rev-parse", "HEAD") if git_ok("rev-parse", "HEAD") else ""
    short = head[:8]

    # 2) 打标签
    tag = None
    if make_tag and head:
        if git_ok("rev-parse", "-q", "--verify", f"refs/tags/{version}"):
            print(f"· 标签 {version} 已存在，跳过")
            tag = version
        else:
            git("tag", "-a", version, "-m", label or version)
            tag = version
            print(f"✔ 已打标签：{tag}")

    # 3) 拷贝全量快照
    dest = ARCHIVE / version
    if dest.exists():
        shutil.rmtree(dest)
    n = copy_snapshot(dest)
    meta = {
        "version": version,
        "label": label,
        "message": message or label,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "commit": head,
        "commit_short": short,
        "tag": tag,
        "files": n,
        "dirty_before": bool(changes),
        "config_snapshot": config_snapshot(),
        "python": sys.version.split()[0],
    }
    (dest / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✔ 快照已写入：{dest.relative_to(PROJ)}（{n} 个文件）")

    # 4) 清单 + 索引
    manifest.setdefault("versions", []).append({
        "version": version, "label": label, "time": meta["time"],
        "commit": head, "commit_short": short, "tag": tag, "files": n,
    })
    save_manifest(manifest)
    write_archive_index(manifest)
    print(f"✔ 清单已更新：{MANIFEST.name}")
    print(f"\n回退方式：git checkout {tag or short}   （或直接看 archive/{version}/）")
    return 0


def write_archive_index(manifest: dict) -> None:
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 存档索引（每个里程碑一份全量快照）",
        "",
        "由 `scripts/archive.py` 自动生成，请勿手改。每条记录都可用 `git checkout <tag>` 回退。",
        "",
        "| 版本 | 时间 | 文件数 | git 提交 | 标签 | 说明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for e in manifest.get("versions", []):
        lines.append(
            f"| {e['version']} | {e['time']} | {e.get('files', 0)} | "
            f"`{e.get('commit_short', '')}` | {e.get('tag') or '—'} | {e.get('label', '')} |"
        )
    lines += [
        "",
        "## 手工回退",
        "",
        "```powershell",
        "git log --oneline                 # 看所有存档提交",
        "git checkout v0.2                 # 回到某个里程碑（只读）",
        "git checkout -b fix/from-v0.2     # 以某个里程碑为起点改",
        "```",
        "",
        "## 快照内容",
        "",
        "每个 `archive/vX.Y/` 内含该版本全部源码 + `_meta.json`（提交号、时间、当时的用户配置）。",
        "不含 `.venv/`、`tmp/`、`archive/` 自身。",
    ]
    (ARCHIVE / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="里程碑存档：提交 + 打标签 + 全量快照")
    p.add_argument("--stage", help="版本号，如 v0.1（默认自动递增）")
    p.add_argument("--label", default="", help="里程碑说明")
    p.add_argument("--message", default=None, help="自定义 commit 信息")
    p.add_argument("--no-tag", action="store_true", help="不打标签")
    p.add_argument("--allow-empty", action="store_true", help="没有改动也建一个空提交")
    p.add_argument("--probe", action="store_true", help="只显示下一个版本号")
    p.add_argument("--list", action="store_true", help="列出所有存档")
    args = p.parse_args(argv)
    if args.probe:
        return probe()
    if args.list:
        return listing()
    return seal(args.stage, args.label, args.message, not args.no_tag, args.allow_empty)


if __name__ == "__main__":
    raise SystemExit(main())
