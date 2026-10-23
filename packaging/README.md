# camera-auto-brightness 打包说明（PyInstaller）

目标：让不懂 Python 的人**双击就能用**——不用装 Python、不用敲命令。

打包产物是两个自包含的 exe：`dist\Camab.exe`（命令行版）和 `dist\CamabTray.exe`（托盘版，双击即用、无黑框）。

---

## 1. 一分钟上手

```powershell
# 在项目根目录执行（或右键 build_exe.ps1 → 使用 PowerShell 运行）
powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1
```

跑完会自己验证一遍（冻结版 `-c` 冒烟 + `dist\Camab.exe preflight --no-camera`），
最后打印「结论：打包 + 验证全部通过。」并且退出码为 0 才算成功。

日常使用：

| 想干什么 | 怎么做 |
| --- | --- |
| 托盘常驻（最常用） | 双击 `scripts\start.cmd`，或双击 `dist\CamabTray.exe` |
| 环境自检 | `scripts\start.cmd check` |
| 命令行全部参数 | `dist\Camab.exe --help` |
| 开机自启 | `powershell -ExecutionPolicy Bypass -File packaging\install_autostart.ps1` |
| 取消开机自启 | 同上，末尾加 `-Remove` |

> 没有 exe 也能用：`scripts\start.cmd` 会自动退回项目内 `.venv` 的 Python 源码方式。

---

## 2. 产物与体积（2026-10-07 实测）

| 文件 | 字节 | 大小 | 说明 |
| --- | --- | --- | --- |
| `dist\Camab.exe` | 75,645,536 | 72.14 MiB | 控制台版，`preflight` / `run` / `probe` / `calibrate` / `devices` / `config` |
| `dist\CamabTray.exe` | 75,643,178 | 72.14 MiB | 托盘版，`console=False`（无控制台窗口），无参数双击 = 进托盘 |

- 两者合计约 **144 MiB**。体积主要来自 opencv（约 60 MB）+ numpy（约 20 MB）+ tcl/tk。
- **构建耗时**：约 **45–52 秒**（`-Clean` 全新构建、一次分析出两个 EXE；最后一次实测 46.1 秒）。
- PyInstaller 缓存放在 `build\`，实测约 **157 MB**，可以随时删（下次构建会重建）。
- `-OneDir` 布局：`dist\Camab\Camab.exe` 与 `dist\CamabTray\CamabTray.exe` 各 5.5 MB，但每个目录合计 **186 MB**
  （onefile 会 zlib 压缩，onedir 是解压后的 DLL，所以 onedir 总体更大、启动更快）。

环境（实测）：Python 3.12.14（项目内 `.venv`）、PyInstaller 6.22.3 + pyinstaller-hooks-contrib 2026.8、
opencv-python 5.0.0.93、numpy 2.5.3、pillow 12.3.0、pystray 0.19.5、pywin32 312。

---

## 3. 目录里都是什么

| 文件 | 作用 |
| --- | --- |
| `build_exe.ps1` | 主入口：装 PyInstaller（阿里云镜像）→ 跑 spec → 报体积 → 验证产物 |
| `camab.spec` | PyInstaller 配置。**两个入口脚本和运行时钩子是构建期自动生成的**，写在 `build\camab\_gen\`，所以本目录只有这几个文件 |
| `requirements.txt` | 运行依赖 + 构建依赖，文件里已内置阿里云镜像配置 |
| `install_autostart.ps1` | 注册/取消「当前用户」开机自启（不需要管理员），幂等 |
| `README.md` | 本文件 |
| `..\scripts\start.cmd` | 一键启动（双击），优先用 exe，没 exe 退回源码 |

`camab.spec` 生成的三个文件（每次构建重新生成，不用手改）：

- `entry_console.py`：`from camab.__main__ import main`，参数原样透传。
- `entry_tray.py`：**不给参数就默认 `tray`**（双击即用）；崩了会写 `%LOCALAPPDATA%\camera-auto-brightness\camab-crash.log` 并弹窗，不会哑巴退出。
- `rthook_frozen_c.py`：运行时钩子，见下方「为什么需要它」。

### 为什么需要 `rthook_frozen_c.py`

`src\camab\camera.py` 的 `probe_in_subprocess()` 用 `sys.executable -c <脚本>` 起子进程探测摄像头。
打包后 `sys.executable` 变成了 `Camab.exe`，如果不拦，子进程就变成 `Camab.exe -c ...`，
argparse 直接报 `unrecognized arguments: -c`，**preflight 的摄像头探测会废掉**。
钩子在启动最早期截获 `-c`，用 `exec` 执行那段代码，等价于 `python -c`。
camab 的 CLI 本身没有 `-c` 选项，不存在冲突。（这条已实测，见第 5 节第 3、4 条。）

---

## 4. `build_exe.ps1` 参数

| 参数 | 作用 |
| --- | --- |
| （无） | 默认 onefile：`dist\Camab.exe` + `dist\CamabTray.exe` |
| `-Clean` | 先删 `build\camab` 和旧 dist 产物，再做干净构建（推荐每次正式打包都加） |
| `-OneDir` | 改成 onedir 布局（`dist\Camab\Camab.exe`），启动更快、不必每次解压 |
| `-SkipInstall` | 跳过 pip 安装步骤 |
| `-NoVerify` | 跳过打包后的验证 |
| `-PythonExe <路径>` | 指定解释器，默认 `<项目>\.venv\Scripts\python.exe` |

脚本做的几件「别扭但必要」的事：

1. **pip 只走阿里云镜像**：`-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com`
   （本机 PyPI 官方源不通）。
2. **清掉 `PYTHONPATH` / `PYTHONHOME`**：空的 `PYTHONPATH` 会把「当前目录」塞进模块搜索路径，
   外部环境传进来的 `PYTHONPATH` 还可能让 PyInstaller 收错包。
3. `pathex` 只给 `src`，**不手动加 venv 的 site-packages**：PyInstaller 本来就用这个 venv 在跑，
   再加一遍会触发弃用告警（PyInstaller 7.0 会直接报错）。
4. `hiddenimports` 显式列出 camab 全部 13 个子模块 + `pystray._win32` + pywin32 全家（`win32com.client`、`pythoncom`、`pywintypes`、`win32timezone`）。
5. 打包后**真的跑一次** `dist\Camab.exe preflight --no-camera`，非 0 退出码不算成功。

---

## 5. 实测验证记录（都是真跑过的）

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `packaging\build_exe.ps1 -Clean` | 退出码 0，用时 46.1 秒，结论「打包 + 验证全部通过」 |
| 2 | `dist\Camab.exe preflight --no-camera` | 退出码 0；依赖 ✅ cv2 5.0.0、numpy 2.5.3、PIL 12.3.0、pystray、win32com；亮度后端 wmi 可用（笔记本内屏 67%）；⚠️ 告警项来自摄像头硬件（幽灵设备），与打包无关 |
| 3 | `dist\Camab.exe -c "import sys; sys.stdout.write('PYI_C_SHIM_OK')"` | 输出 `PYI_C_SHIM_OK`，退出码 0 → 运行时钩子生效 |
| 4 | `dist\Camab.exe preflight --timeout 20`（完整版，真起子进程探摄像头） | 退出码 1；关键行 `✅ #0 后端=DSHOW 打开=True 取帧=True 320x240 6.06s` → 冻结版 `-c` 在真实路径有效；退出码 1 是因为画面全黑（硬件问题） |
| 5 | `dist\Camab.exe --dry-run --source tmp\light-ramp.mp4 tray --selftest` | 退出码 0，`界面自检通过：窗口/状态渲染/设置面板/暂停恢复 均正常` |
| 6 | `dist\CamabTray.exe --dry-run --source tmp\light-ramp.mp4 tray --selftest` | 退出码 0（无控制台版里 tkinter + PIL + pystray 也都正常） |
| 7 | `dist\Camab.exe devices` | 输出含新文案：`` `camab --source "http://手机IP:8080/video" run` 指向它（注意 --source 这类全局参数要写在子命令 run 前面） `` → 打包的是最新源码 |
| 8 | `build_exe.ps1 -OneDir -Clean -NoVerify` | 退出码 0，22.5 秒；`dist\Camab\Camab.exe preflight --no-camera` 正常 |
| 9 | `scripts\start.cmd help / xyz / devices / check / tray` | 中文正常；`help` 退出码 0、未知模式退出码 2、`check` 退出码 1（摄像头硬件）、`tray` 真的拉起了 `CamabTray.exe`（窗口标题「摄像头自动亮度」） |
| 10 | `install_autostart.ps1` 全流程 | `-Status`（未注册）→ 注册 → 再注册（幂等，输出「注册表里已经是同样的命令，无需改动」）→ `-WhatIf`（不写入，实测注册表仍为空）→ `-UseStartupFolder`（生成 `启动\摄像头自动亮度.lnk`，Target/WorkDir 正确）→ `-Remove`（两种位置都清掉）→ 再 `-Remove`（输出「本来就不存在」）。**测完已还原：当前开机自启是未注册状态。** |

`install_autostart.ps1 -Status` 的实际输出：

```
==================================================================
 摄像头自动亮度 —— 开机自启状态
==================================================================
  注册表 Run 项  : 未注册
  启动文件夹     : 没有快捷方式
  存在 : ...\camera-auto-brightness\dist\CamabTray.exe
  存在 : ...\camera-auto-brightness\.venv\Scripts\pythonw.exe
```

---

## 6. 常见报错（含真实踩过的坑）

### 6.1 `[camab.spec] 找不到源码目录：...\10-projects\src`（真实踩过）

第一次跑 `build_exe.ps1 -Clean` 就是这个报错，退出码 1。
原因：PyInstaller 注入的 `SPECPATH` 是 spec 所在**目录**（`packaging\`），我按「spec 文件路径」用了，
于是往上找到项目根的上一级。**已修**：spec 现在从锚点往上找「谁里面有 `src\camab` 谁就是项目根」。

### 6.2 `Foreign Python environment's site-packages paths added to --paths/pathex`（真实踩过）

原因是手动把 `.venv\Lib\site-packages` 塞进了 `pathex`。**已修**：`pathex` 只留 `src`
（PyInstaller 自己就是这个 venv 启动的，site-packages 本来就在搜索路径里）。
不改的话 PyInstaller 7.0 会把它变成**错误**。

### 6.3 pip 装不上 / 超时

本机 PyPI 官方源不通，必须走镜像：

```powershell
.\.venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com pyinstaller
```

`build_exe.ps1` 已内置；`requirements.txt` 里也写了 `--index-url` / `--trusted-host`，直接
`pip install -r packaging\requirements.txt` 即可。

### 6.4 `WARNING: get_package_paths - package pywin32_system32 has multiple paths`

pywin32 的已知无害警告（同一个目录被列了两次），不影响产物。

### 6.5 中文乱码（三种情况，别混）

| 场景 | 现象 | 结论 |
| --- | --- | --- |
| **在 cmd 窗口里直接跑 exe** | 中文**正常** | 实测截图确认：控制台输出走 `WriteConsoleW`，跟代码页无关，**不需要 `chcp 65001`** |
| **把输出重定向/管道**（`dist\Camab.exe preflight > out.txt`，再 `type out.txt`） | 乱码 | 这时是 UTF-8 字节，cp936 的控制台看不懂。**先 `chcp 65001`**，或用 UTF-8 编辑器打开文件 |
| **`scripts\start.cmd`** | 中文**正常** | 脚本本身是 GBK/ANSI 保存的，见 6.6 |

### 6.6 `scripts\start.cmd` 的两条硬规矩（血泪，改这个文件前必读）

`start.cmd` 踩了 cmd.exe 的两个坑，写在这里防止后人重复踩：

1. **文件里有中文时，绝对不能用 `goto :标签`。**
   cmd.exe 是按字节偏移找行的，多字节字符会让它算错位置、跳到行中间。
   实测报错长这样：`'heck' is not recognized as an internal or external command`（从 `check` 中间开始执行）、
   `'ray' is not recognized`（`tray` 少了首字母）、`rem` 注释的后半截被当成命令跑。
   → 所以 `start.cmd` **一个 goto 都没有**，只用 `if / else` 顺序执行。
2. **行尾必须是 CRLF，且文件必须是 GBK/ANSI。**
   只有 LF 换行 + 中文同样会让 cmd 解析错位（实测一大片 `'RUN' is not recognized`）；
   存成 UTF-8 则在 cp936 控制台里显示乱码。**不要在里面 `chcp`**（中途改代码页正是坑 1 的诱因）。
   用记事本编辑请选「ANSI」编码保存。

> 另外 `chcp 65001` + UTF-8 中文的组合在有 `goto` 时是**必炸**的，别再试了。

### 6.7 双击 exe 后要等一两秒才出窗口

onefile 每次启动都要把约 72 MB 解压到 `%TEMP%\_MEIxxxx`，首次启动 2–5 秒属正常。
想要秒开就用 `-OneDir`。
另外 onefile 运行期间任务管理器里会看到**两个同名进程**（引导进程 + 真正的程序进程），也是正常的。

### 6.8 杀毒软件报毒

PyInstaller 打包的 exe 被误报是常见现象（尤其国产杀软）。本项目源码可读，
不放心就把 `dist\` 加白名单，或者直接用 `.venv` 源码方式跑。

### 6.9 改了 spec 但没生效 / `packaging\camab.spec` 提交不上

- spec 改动后要 `-Clean` 重新构建才会重跑分析缓存。
- 项目 `.gitignore` 里有 `*.spec`，所以 `packaging\camab.spec` **默认不会入库**，
  要提交得 `git add -f packaging\camab.spec`。

---

## 7. 已知问题 / 没做完的

1. **冻结版的摄像头探测依赖运行时钩子** `rthook_frozen_c.py`。如果以后 `camera.py` 不再用
   `sys.executable -c` 探活，这个钩子就多余了（留着无害）；反之若换了别的方式起子进程，要重新核对。
2. **两个 onefile exe 各含一份完整依赖**（各约 72 MB，合计约 144 MB）。想省空间只能改成一个 exe 带子命令，或改用 `-OneDir`。
3. **没有自定义图标和版本资源**（用的是 PyInstaller 默认图标）。要做的话在 spec 的 `EXE(...)` 里加 `icon=`，
   再准备一个 `.ico`。
4. **onedir 模式只验证了 `preflight`**，没跑 `tray --selftest`（onefile 两种都跑过）。
5. exe 是**源码快照**：本次打包对应 `src\camab\*.py` 最后修改时间 **2026-10-07 15:34:56**（`devices.py`）。
   之后源码再改，必须重新跑一次 `build_exe.ps1` 才能进 exe。
6. 项目路径含全角字符（`ｄｓｈ`、`ｗｏｒｋ`），PyInstaller 6.22.3 实测正常；换更老的版本可能有编码问题。
7. `packaging\*.ps1` 用 **UTF-8 with BOM** 保存（Windows PowerShell 5.1 才不会把中文读成乱码），
   `scripts\start.cmd` 用 **GBK/ANSI + CRLF**。两者编码不同是**故意的**，别统一。
