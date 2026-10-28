"""摄像头设备诊断。

为什么需要这个模块：实战中发现「摄像头打不开/全是黑帧」有四种完全不同的成因，
只报一句"取帧失败"没法排查。这里把它们区分开：

1. **硬件未连接**：`HD Webcam` 这类内置摄像头在 PnP 里是 ``CM_PROB_PHANTOM``（Code 45）
   —— 物理上没接上/被 BIOS 关掉/排线松了。任何程序都拿不到，只能修硬件。
2. **被虚拟摄像头顶替**：如 ``AMD Privacy View camera``、OBS Virtual Camera。
   它们会用设备索引 0 注册成 DirectShow 采集设备，真实摄像头反而看不见。
3. **能打开但输出黑帧**：摄像头被物理遮盖、被隐私软件拦截、或房间真的全黑。
4. **被其它程序独占**：微信/腾讯会议/OBS 占着设备。

只用标准库 + PowerShell，不依赖 cv2。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

#: 只要设备名里带这些词，基本可以判定不是真实物理摄像头
VIRTUAL_HINTS = (
    "privacy view", "virtual", "obs", "droidcam", "iriun", "manycam",
    "snap camera", "xsplit", "nvidia broadcast", "epoccam", "camo",
    "虚拟", "e2esoft", "vcam",
)


@dataclass
class DeviceInfo:
    name: str = ""
    status: str = ""
    cls: str = ""
    present: bool = False
    problem: str = ""
    instance_id: str = ""

    @property
    def is_phantom(self) -> bool:
        return self.problem == "CM_PROB_PHANTOM" or self.status.lower() == "unknown"

    @property
    def is_virtual(self) -> bool:
        low = self.name.lower()
        return any(h in low for h in VIRTUAL_HINTS)

    @property
    def real_camera_present(self) -> bool:
        return self.present and not self.is_phantom and not self.is_virtual

    def as_dict(self) -> dict:
        return {
            "name": self.name, "status": self.status, "class": self.cls,
            "present": self.present, "problem": self.problem,
            "instance_id": self.instance_id, "virtual": self.is_virtual,
        }


@dataclass
class DeviceReport:
    devices: list[DeviceInfo] = field(default_factory=list)
    dshow_names: list[str] = field(default_factory=list)
    services: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def real_cameras(self) -> list[DeviceInfo]:
        return [d for d in self.devices if d.real_camera_present]

    @property
    def virtual_cameras(self) -> list[DeviceInfo]:
        return [d for d in self.devices if d.present and d.is_virtual]

    @property
    def phantom_cameras(self) -> list[DeviceInfo]:
        return [d for d in self.devices if d.cls in ("Camera", "Image") and d.is_phantom]


_PS_ENUM = r"""
$ErrorActionPreference='SilentlyContinue'
$out = @()
Get-PnpDevice | Where-Object { $_.Class -in @('Camera','Image') -or $_.FriendlyName -match 'camera|webcam|摄像' } |
  ForEach-Object {
    $out += [pscustomobject]@{
      name = $_.FriendlyName; status = "$($_.Status)"; cls = "$($_.Class)";
      present = [bool]$_.Present; problem = "$($_.Problem)"; instanceId = "$($_.InstanceId)"
    }
  }
$svc = @{}
foreach($n in @('FrameServer','FrameServerMonitor','camsvc')){
  $s = Get-Service -Name $n -ErrorAction SilentlyContinue
  if($s){ $svc[$n] = "$($s.Status)" }
}
[pscustomobject]@{ devices = $out; services = $svc } | ConvertTo-Json -Depth 5 -Compress
"""


def enumerate_windows_devices(timeout: float = 25.0) -> DeviceReport:
    """用 PowerShell 查 Windows 侧的摄像头设备状态。"""
    rep = DeviceReport()
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_ENUM],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        rep.error = f"{type(exc).__name__}: {exc}"
        return rep
    text = (proc.stdout or "").strip()
    if not text:
        rep.error = (proc.stderr or "").strip()[:300] or "PowerShell 没有输出"
        return rep
    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        rep.error = f"解析失败：{exc}"
        return rep
    raw = data.get("devices") or []
    if isinstance(raw, dict):
        raw = [raw]
    for d in raw:
        rep.devices.append(DeviceInfo(
            name=str(d.get("name") or ""), status=str(d.get("status") or ""),
            cls=str(d.get("cls") or ""), present=bool(d.get("present")),
            problem=str(d.get("problem") or ""), instance_id=str(d.get("instanceId") or ""),
        ))
    rep.services = data.get("services") or {}
    return rep


def dshow_device_names(timeout: float = 20.0) -> list[str]:
    """通过 ffmpeg 列 DirectShow 视频设备名（有 ffmpeg 才用）。

    这一步能抓到 PnP 里看不见的**软设备**，例如 ``AMD Privacy View camera``。
    """
    import shutil

    exe = shutil.which("ffmpeg")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return []
    names: list[str] = []
    for line in (proc.stderr or "").splitlines():
        if "(video)" in line and '"' in line:
            try:
                names.append(line.split('"')[1])
            except IndexError:
                continue
    return names


def analyze(report: DeviceReport, opencv_ok: bool = False,
            frame_mean: Optional[float] = None) -> dict:
    """把设备报告 + 采集结果综合成结论与建议。"""
    problems: list[str] = []
    advice: list[str] = []
    severity = "ok"

    if report.error and not report.devices and not report.dshow_names:
        return {"severity": "unknown", "problems": [f"设备枚举失败：{report.error}"],
                "advice": [], "real_cameras": 0, "virtual_cameras": 0, "phantom_cameras": 0}

    reals, virtuals, phantoms = report.real_cameras, report.virtual_cameras, report.phantom_cameras

    if phantoms and not reals:
        severity = "blocked"
        names = "、".join(f"{d.name}" for d in phantoms)
        problems.append(f"发现摄像头记录但**未连接**（Code 45 / 幽灵设备）：{names}")
        if virtuals:
            vnames = "、".join(d.name for d in virtuals)
            problems.append(f"实际能打开的只有虚拟摄像头：{vnames}")
            advice.append(
                "虚拟摄像头（如 AMD Privacy View / OBS）会把设备索引 0 占掉，"
                "真实摄像头反而看不见；要拿真实画面得先修好摄像头本身。"
            )
        advice += [
            "按顺序排查：① 设备管理器「查看 → 显示隐藏的设备」，看「照相机 / 图像设备」里"
            "是否有半透明的设备，右键「启用设备」；",
            "② 重启进 BIOS/UEFI，找 Integrated Camera / Camera / Webcam 之类的开关，"
            "确认是 Enabled（笔记本重装系统后经常是关的）；",
            "③ 笔记本屏幕上方/侧面的物理遮盖滑块拨开；",
            "④ 任意插一个 USB 摄像头验证是不是整机 USB 视频类驱动的问题；",
            "⑤ 摄像头排线松动属于硬件问题，需要送修或外接摄像头。",
        ]
    elif not reals and not virtuals and not phantoms:
        severity = "none"
        problems.append("系统里没有任何摄像头设备（连幽灵记录都没有）")
        advice.append("插一个 USB 摄像头试试；若仍无反应，检查 BIOS 里 USB 控制器设置。")

    if virtuals and not reals:
        advice.append(
            "在修好之前，可以用手机当光传感器绕过这个问题："
            "手机装 IP Webcam / DroidCam 之类 App，然后用 "
            "`camab --source \"http://手机IP:8080/video\" run` 指向它"
            "（注意 --source 这类全局参数要写在子命令 run 前面）。"
        )

    if opencv_ok and frame_mean is not None and frame_mean < 1.0:
        severity = "black"
        problems.append(
            f"摄像头能打开但画面是全黑的（平均亮度 {frame_mean:.2f}/255）"
        )
        advice.append(
            "黑帧的常见原因：① 物理遮挡/隐私快门没开；② 隐私软件（AMD Privacy View 等）"
            "正在拦截画面；③ 房间真的全黑（开灯再测一次即可排除）；"
            "④ 设备被微信/腾讯会议/OBS 独占。"
        )
    elif opencv_ok and not problems:
        severity = "ok"

    return {
        "severity": severity, "problems": problems, "advice": advice,
        "real_cameras": len(reals), "virtual_cameras": len(virtuals),
        "phantom_cameras": len(phantoms),
        "real_camera_names": [d.name for d in reals],
        "virtual_camera_names": [d.name for d in virtuals],
    }


def full_report(opencv_ok: bool = False, frame_mean: Optional[float] = None,
                with_ffmpeg: bool = True) -> dict:
    """一次性给出设备、DirectShow 名称、结论。"""
    if sys.platform != 'win32':
        from .platform_support import camera_help
        names = []
        if sys.platform.startswith('linux'):
            try:
                names = [p.read_text().strip() for p in sorted(Path('/sys/class/video4linux').glob('video*/name'))]
            except OSError:
                pass
        black = opencv_ok and frame_mean is not None and frame_mean < 1.0
        return {'platform': sys.platform, 'severity': 'black' if black else 'ok' if opencv_ok else 'unknown',
                'real_camera_names': names, 'virtual_camera_names': [], 'devices': [],
                'dshow_names': [], 'services': {},
                'problems': ['摄像头返回黑帧，请检查遮挡和视频源。'] if black else [],
                'advice': [] if opencv_ok and not black else [camera_help()]}
    rep = enumerate_windows_devices()
    if with_ffmpeg:
        rep.dshow_names = dshow_device_names()

    # DirectShow 里的软设备（如 AMD Privacy View camera）在 PnP 里查不到，
    # 但它们会占掉设备索引 0，必须单独记一笔，否则诊断结论会互相矛盾。
    known = {d.name.strip().lower() for d in rep.devices}
    for name in rep.dshow_names:
        if name.strip().lower() not in known:
            rep.devices.append(DeviceInfo(
                name=name, status="OK", cls="DirectShow", present=True,
                problem="", instance_id="(DirectShow 软设备)",
            ))

    out = analyze(rep, opencv_ok=opencv_ok, frame_mean=frame_mean)
    out["devices"] = [d.as_dict() for d in rep.devices]
    out["dshow_names"] = rep.dshow_names
    out["services"] = rep.services
    if rep.error:
        out["enumerate_error"] = rep.error
    return out


def format_text(info: dict) -> str:
    """人类可读的诊断块。"""
    lines = ["[摄像头设备诊断]"]
    for name in info.get("real_camera_names", []):
        lines.append(f"  ✅ 可用物理摄像头：{name}")
    for name in info.get("virtual_camera_names", []):
        lines.append(f"  ⚠️  虚拟摄像头：{name}（非真实画面）")
    if info.get("phantom_cameras"):
        lines.append(f"  ❌ 幽灵设备（未连接）数量：{info['phantom_cameras']}")
    if info.get("devices"):
        for d in info["devices"]:
            mark = "·"
            if d.get("virtual"):
                mark = "⚠️ "
            elif not d.get("present"):
                mark = "❌"
            lines.append(f"  {mark} {d['name']}  状态={d['status']} 存在={d['present']} "
                         f"{d.get('problem') or ''}")
    for p in info.get("problems", []):
        lines.append(f"  ❌ {p}")
    for a in info.get("advice", []):
        lines.append(f"  💡 {a}")
    return "\n".join(lines)
