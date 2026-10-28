"""亮度后端抽象层。

Windows 上控制屏幕亮度有两条完全不同的路：

* 笔记本内屏：``root\\wmi`` 的 ``WmiMonitorBrightness`` / ``WmiMonitorBrightnessMethods``
* 外接显示器：DDC/CI（通过 ``dxva2.dll`` 的 ``GetPhysicalMonitorsFromHMONITOR`` 等）

本模块把它们统一成 :class:`BrightnessBackend`，并提供 :func:`detect_backends`
做可用性探测、:func:`open_backend` 按优先级取第一个能用的后端。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

log = logging.getLogger(__name__)

MIN_LEVEL = 0
MAX_LEVEL = 100


class BrightnessError(RuntimeError):
    """亮度读写失败。"""


@dataclass(frozen=True)
class MonitorInfo:
    """一块可寻址的显示器。"""

    backend: str
    index: int
    key: str            # 后端内部标识（WMI InstanceName / HMONITOR 句柄）
    name: str = ""
    current: Optional[int] = None

    def __str__(self) -> str:  # pragma: no cover - 展示用
        cur = "?" if self.current is None else self.current
        return f"[{self.backend}] {self.name or self.key} 当前={cur}"


class BrightnessBackend:
    """亮度后端基类。

    约定：所有实现都是「一次性、无状态」的 —— 每次 ``get``/``set`` 自己打开句柄、
    用完立刻释放。这样即使外接显示器被拔掉，也不会留下悬挂的资源。
    """

    name = "base"
    #: 该后端是否真的会改变屏幕亮度
    functional = True
    #: set() 已使用同一硬件句柄回读，Engine 无需再枚举显示器并回读一次。
    verifies_writes = False

    def available(self) -> bool:
        raise NotImplementedError

    def monitors(self) -> Sequence[MonitorInfo]:
        raise NotImplementedError

    def get(self, key: str) -> int:
        raise NotImplementedError

    def set(self, key: str, level: int) -> int:
        """写入亮度，返回实际写入值。"""
        raise NotImplementedError

    # --- 便利方法 -------------------------------------------------------
    def first(self) -> Optional[MonitorInfo]:
        mons = list(self.monitors())
        return mons[0] if mons else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name}>"


# --------------------------------------------------------------------------- #
# 后端 1：WMI（笔记本内屏）
# --------------------------------------------------------------------------- #
_WMI_PS_GET = (
    "Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness | "
    "Select-Object -First 1 -ExpandProperty CurrentBrightness"
)
_WMI_PS_SET = (
    "$ErrorActionPreference='Stop'; $result=Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods | "
    "Select-Object -First 1 | Invoke-CimMethod -MethodName WmiSetBrightness "
    "-Arguments @{{Timeout=0; Brightness={level}}}; "
    "if ($null -ne $result -and $null -ne $result.PSObject.Properties['ReturnValue'] "
    "-and [int]$result.ReturnValue -ne 0) {{ throw ('WmiSetBrightness failed: ' + $result.ReturnValue) }}"
)


class WmiBackend(BrightnessBackend):
    """内屏亮度后端：优先 pywin32，退化为 PowerShell CIM。"""

    name = "wmi"
    verifies_writes = True

    def __init__(self, timeout: float = 6.0) -> None:
        self.timeout = timeout
        self._win32 = None
        #: 原生 COM 失败后只尝试一次，再使用 PowerShell CIM 回退。
        self._com_write_disabled = False
        try:  # pragma: no cover - 取决于环境
            import win32com.client  # type: ignore

            self._win32 = win32com.client
        except Exception:  # noqa: BLE001
            self._win32 = None

    # -- pywin32 路径 ---------------------------------------------------
    def _com_objs(self):
        if self._win32 is None:
            raise BrightnessError("pywin32 不可用")
        # COM 对象属于创建它的线程；引擎与界面命令不能共用缓存的代理。
        return self._win32.GetObject("winmgmts:root\\wmi")

    def _com_call(self, operation):
        """在调用线程初始化 COM，并在局部代理释放后平衡初始化计数。"""
        import pythoncom  # type: ignore

        initialized = False
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
        except pythoncom.com_error as exc:
            if exc.hresult != -2147417850:  # RPC_E_CHANGED_MODE：线程已有不同 apartment。
                raise
        try:
            return operation(self._com_objs())
        finally:
            if initialized:
                pythoncom.CoUninitialize()

    def _com_get(self) -> Optional[int]:
        def read(wmi):
            for it in wmi.ExecQuery("Select * from WmiMonitorBrightness"):
                return int(it.CurrentBrightness)
            return None

        return self._com_call(read)

    def _com_set(self, level: int) -> int:
        if self._com_write_disabled:
            raise BrightnessError("pywin32 写亮度已确认不可用，改用 PowerShell")
        def write(wmi):
            done = False
            for it in wmi.ExecQuery("Select * from WmiMonitorBrightnessMethods"):
                # SWbem 的动态包装会按属性名枚举参数；用有名参数避免位置颠倒。
                params = it.Methods_("WmiSetBrightness").InParameters.SpawnInstance_()
                params.Properties_("Brightness").Value = int(level)
                params.Properties_("Timeout").Value = 0
                result = it.ExecMethod_("WmiSetBrightness", params)
                if result is not None and int(getattr(result, "ReturnValue", 0)) != 0:
                    raise BrightnessError(f"WmiSetBrightness 失败：{result.ReturnValue}")
                done = True
            if not done:
                raise BrightnessError("WmiMonitorBrightnessMethods 未返回任何实例")
            # 驱动可能异步提交背光：立即回读有时仍是前一个值。
            deadline = time.monotonic() + 0.35
            while True:
                back = next((int(it.CurrentBrightness) for it in
                             wmi.ExecQuery("Select * from WmiMonitorBrightness")), None)
                if back is None:
                    raise BrightnessError("写入后未返回屏幕亮度")
                if back == level or time.monotonic() >= deadline:
                    return back
                time.sleep(0.02)

        try:
            return self._com_call(write)
        except Exception as exc:  # noqa: BLE001
            # 记住这次失败：后面直接走 PowerShell，不再每次白试
            self._com_write_disabled = True
            log.info("pywin32 写亮度不可用（%s），后续改用 PowerShell CIM", str(exc)[:120])
            raise

    # -- PowerShell 路径 -------------------------------------------------
    def _ps(self, script: str) -> str:
        from .winutil import run_hidden

        exe = "powershell.exe" if os.name == "nt" else "pwsh"
        # 必须走 run_hidden：裸 subprocess 会让 powershell 闪一个黑框出来（用户反馈过）。
        # 注意不要再加 -WindowStyle Hidden：配合 CREATE_NO_WINDOW 会让 powershell 卡住
        # （实测：加上之后 SetBrightness 直接 6 秒超时，去掉立刻正常）。
        try:
            proc = run_hidden(
                [exe, "-NoProfile", "-NonInteractive", "-Command", script],
                timeout=self.timeout,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise BrightnessError(f"PowerShell 亮度调用失败：{exc}") from exc
        if proc.returncode != 0:
            raise BrightnessError((proc.stderr or proc.stdout or "").strip()[:300])
        return (proc.stdout or "").strip()

    # -- 接口实现 -------------------------------------------------------
    def available(self) -> bool:
        try:
            return self._com_get() is not None
        except Exception:  # noqa: BLE001
            pass
        try:
            return self._ps(_WMI_PS_GET) != ""
        except Exception:  # noqa: BLE001
            return False

    def monitors(self) -> Sequence[MonitorInfo]:
        cur = None
        try:
            cur = self._com_get()
        except Exception:  # noqa: BLE001
            pass
        if cur is None:
            try:
                cur = int(float(self._ps(_WMI_PS_GET)))
            except Exception:  # noqa: BLE001
                cur = None
        if cur is None:
            return []
        return [MonitorInfo(self.name, 0, "internal:0", "笔记本内屏", cur)]

    def get(self, key: str) -> int:
        try:
            val = self._com_get()
        except Exception:  # noqa: BLE001
            val = None
        if val is None:
            try:
                val = int(float(self._ps(_WMI_PS_GET)))
            except (ValueError, OverflowError) as exc:
                raise BrightnessError("WMI 未返回有效的屏幕亮度") from exc
        return int(val)

    def set(self, key: str, level: int) -> int:
        level = max(MIN_LEVEL, min(MAX_LEVEL, int(level)))
        try:
            return self._com_set(level)
        except Exception as exc:  # noqa: BLE001
            log.debug("pywin32 写亮度失败(%s)，改用 PowerShell", exc)
        # 走 PowerShell CIM：必须经过 run_hidden（见 _ps），否则每次调亮度都会闪黑框
        self._ps(_WMI_PS_SET.format(level=level))
        return self.get(key)


# --------------------------------------------------------------------------- #
# 后端 2：DDC/CI（外接显示器，纯 ctypes 访问 dxva2.dll）
# --------------------------------------------------------------------------- #
class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", ctypes.c_void_p),
        ("szPhysicalMonitorDescription", ctypes.c_wchar * 128),
    ]


class DdcBackend(BrightnessBackend):
    """外接显示器 DDC/CI 后端。

    VCP 0x10 = 亮度，0x12 = 对比度。很多显示器支持读但写被固件忽略，
    所以 :meth:`set` 之后会回读校验。
    """

    name = "ddc"
    verifies_writes = True
    VCP_BRIGHTNESS = 0x10
    VCP_CONTRAST = 0x12

    def __init__(self) -> None:
        self._dxva2 = None
        self._user32 = None
        if os.name != "nt":
            return
        try:
            self._dxva2 = ctypes.WinDLL("dxva2.dll")
            self._user32 = ctypes.WinDLL("user32.dll")
        except OSError:
            self._dxva2 = None

    # -- 句柄工具 -------------------------------------------------------
    def _handles(self) -> list[tuple[ctypes.c_void_p, str]]:
        """枚举所有物理显示器，返回 [(hmonitor, 描述)]，用完必须 Release。"""
        if self._dxva2 is None or self._user32 is None:
            return []
        out: list[tuple[ctypes.c_void_p, str]] = []
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        )
        found: list[ctypes.c_void_p] = []

        def _cb(hmon, hdc, rect, data):  # noqa: ANN001
            found.append(hmon)
            return 1

        self._user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), None)
        for hmon in found:
            count = ctypes.c_uint32(0)
            if not self._dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                ctypes.c_void_p(hmon), ctypes.byref(count)
            ):
                continue
            arr = (_PHYSICAL_MONITOR * count.value)()
            if not self._dxva2.GetPhysicalMonitorsFromHMONITOR(
                ctypes.c_void_p(hmon), count.value, arr
            ):
                continue
            for i in range(count.value):
                out.append((ctypes.c_void_p(arr[i].hPhysicalMonitor),
                            arr[i].szPhysicalMonitorDescription or ""))
        return out

    def _release(self, handles: Iterable[ctypes.c_void_p]) -> None:
        for h in handles:
            try:
                self._dxva2.DestroyPhysicalMonitor(h)
            except Exception:  # noqa: BLE001
                pass

    def _vcp(self, handle: ctypes.c_void_p, code: int) -> Optional[int]:
        cur = ctypes.c_uint32(0)
        mx = ctypes.c_uint32(0)
        ok = self._dxva2.GetVCPFeatureAndVCPFeatureReply(
            handle, ctypes.c_byte(code), None,
            ctypes.byref(cur), ctypes.byref(mx),
        )
        if not ok:
            return None
        return int(cur.value)

    # -- 接口实现 -------------------------------------------------------
    def available(self) -> bool:
        return bool(self.monitors())

    def monitors(self) -> Sequence[MonitorInfo]:
        if self._dxva2 is None:
            return []
        out: list[MonitorInfo] = []
        pairs = self._handles()
        try:
            for idx, (h, desc) in enumerate(pairs):
                cur = self._vcp(h, self.VCP_BRIGHTNESS)
                if cur is None:
                    continue
                out.append(MonitorInfo(self.name, idx, f"ddc:{idx}", desc or f"显示器{idx}", cur))
        finally:
            self._release(h for h, _ in pairs)
        return out

    def _resolve(self, key: str) -> ctypes.c_void_p:
        want = int(key.split(":")[-1])
        pairs = self._handles()
        try:
            for idx, (h, _desc) in enumerate(pairs):
                if idx == want:
                    return h
            raise BrightnessError(f"未找到 DDC 显示器 {key}")
        finally:
            # 只释放其它句柄
            self._release(h for i, (h, _d) in enumerate(pairs) if i != want)

    def get(self, key: str) -> int:
        h = self._resolve(key)
        try:
            val = self._vcp(h, self.VCP_BRIGHTNESS)
        finally:
            self._release([h])
        if val is None:
            raise BrightnessError(f"读取 {key} 亮度失败")
        return int(val)

    def set(self, key: str, level: int) -> int:
        level = max(MIN_LEVEL, min(MAX_LEVEL, int(level)))
        h = self._resolve(key)
        try:
            if not self._dxva2.SetVCPFeature(h, ctypes.c_byte(self.VCP_BRIGHTNESS),
                                            ctypes.c_uint32(level)):
                raise BrightnessError("SetVCPFeature 调用失败")
            time.sleep(0.05)
            back = self._vcp(h, self.VCP_BRIGHTNESS)
        finally:
            self._release([h])
        if back is not None and abs(int(back) - level) > 1:
            log.warning("DDC 写入 %s 后回读为 %s，显示器可能不支持写", level, back)
        return int(back if back is not None else level)


# --------------------------------------------------------------------------- #
# 后端 3：干跑（不改变任何东西，用于演示/测试/无权限环境）
# --------------------------------------------------------------------------- #
class DryRunBackend(BrightnessBackend):
    """只记录，不真的改亮度。"""

    name = "dryrun"
    functional = False

    def __init__(self, initial: int = 60, name: str = "模拟显示器") -> None:
        self._level = int(initial)
        self._name = name
        self.writes: list[tuple[float, int]] = []

    def available(self) -> bool:
        return True

    def monitors(self) -> Sequence[MonitorInfo]:
        return [MonitorInfo(self.name, 0, "dryrun:0", self._name, self._level)]

    def get(self, key: str) -> int:
        return self._level

    def set(self, key: str, level: int) -> int:
        self._level = max(MIN_LEVEL, min(MAX_LEVEL, int(level)))
        self.writes.append((time.time(), self._level))
        log.info("[dry-run] 亮度 → %d%%", self._level)
        return self._level


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def backend_classes():
    """Only probe the current OS; importing on Unix never invokes PowerShell."""
    if sys.platform == 'win32':
        return (WmiBackend, DdcBackend)
    from .unix_brightness import SysfsBackend, DdcutilBackend, MacOSBackend
    if sys.platform.startswith('linux'):
        return (SysfsBackend, DdcutilBackend)
    if sys.platform == 'darwin':
        return (MacOSBackend,)
    return ()


def _new_backend(cls, timeout):
    return cls() if cls is DdcBackend else cls(timeout=timeout)
def detect_backends(timeout: float = 6.0, only: Optional[Sequence[str]] = None,
                    first_only: bool = False) -> list[BrightnessBackend]:
    """按优先级返回所有**可用**后端。

    :param only: 只探测这些后端名（便于测试/诊断），``None`` 表示全部
    """
    out: list[BrightnessBackend] = []
    for cls in backend_classes():
        if only is not None and cls.name not in only:
            continue
        try:
            be = _new_backend(cls, timeout)
            if be.available():
                out.append(be)
                if first_only:
                    break
        except Exception as exc:  # noqa: BLE001
            log.debug("后端 %s 探测失败: %s", cls.__name__, exc)
    return out


def open_backend(prefer: str = "auto", timeout: float = 6.0,
                 allow_dryrun: bool = False) -> BrightnessBackend:
    """取一个可用后端。

    :param prefer: ``auto`` / ``wmi`` / ``ddc`` / ``dryrun``
    :param allow_dryrun: 没有真实后端时是否退化为干跑
    """
    if prefer == "dryrun":
        return DryRunBackend()

    from .platform_support import BRIGHTNESS_BACKENDS, brightness_help
    explicit = prefer in BRIGHTNESS_BACKENDS and prefer != 'auto'
    found = detect_backends(timeout=timeout,
                            only=[prefer] if explicit else None,
                            first_only=True)
    if explicit:
        for be in found:
            if be.name == prefer:
                return be
        raise BrightnessError(f"指定的后端 {prefer} 在 {sys.platform} 不可用。{brightness_help()}")
    if found:
        return found[0]
    if allow_dryrun:
        log.warning("未发现任何真实亮度后端，退化为 dry-run")
        return DryRunBackend()
    raise BrightnessError(f"未发现可用的亮度后端。{brightness_help()}")


def backend_report(timeout: float = 6.0) -> dict:
    """给 preflight 用的探活报告：不抛异常，只回报事实。"""
    from .platform_support import brightness_help
    report: dict = {"platform": sys.platform, "backends": [], "advice": brightness_help()}
    for cls in backend_classes():
        entry = {"name": cls.name, "available": False, "monitors": [], "error": None}
        try:
            be = _new_backend(cls, timeout)
            mons = list(be.monitors())
            entry["available"] = bool(mons)
            entry["monitors"] = [
                {"key": m.key, "name": m.name, "current": m.current} for m in mons
            ]
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        report["backends"].append(entry)
    return report
