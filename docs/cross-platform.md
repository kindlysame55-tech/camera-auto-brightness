# Windows / Linux / macOS

此页说明 v2.2.0 的运行条件与硬件边界。自动测试和界面自检不会证明真实摄像头或显示器可用，请在目标设备运行只读自检。
This guide describes v2.2.0 requirements. Automated tests and GUI smoke checks cannot establish real hardware compatibility; run read-only preflight checks on the target device.

| 系统 / OS | 摄像头 / Camera | 屏幕调光 / Display control | 要求 / Requirements |
| --- | --- | --- | --- |
| Windows 10/11 x64 | DirectShow → MSMF | WMI / DDC/CI | 支持的内屏或开启 DDC/CI 的外屏 / supported internal or DDC/CI display |
| Linux x86_64 | V4L2 | sysfs + brightnessctl / ddcutil | 图形桌面、设备权限、GUI 库 / graphical session, permissions and GUI libraries |
| macOS Apple Silicon / Intel | AVFoundation | nriley/brightness | brightness helper，摄像头许可，兼容的内屏/Apple 显示器 / helper, camera access, compatible display |

## 便携版 / Portable builds

从 [Releases](https://github.com/kindlysame55-tech/camera-auto-brightness/releases) 选择与系统和 CPU 对应的包。压缩包自带 Python；Linux/macOS 的系统工具与设备权限仍需配置。
Choose the matching OS/CPU package from Releases. Python is bundled; Unix device tools and permissions remain system requirements.

Linux 预编译包在 Ubuntu 22.04 上构建，Mac 包在 macOS 15 上构建。更旧的系统需自行验证；可优先使用下方源码安装方案。
Linux binaries are built on Ubuntu 22.04 and Mac bundles on macOS 15. Older systems require validation; source installation is an alternative.

Windows：解压后双击 `start-camab.exe`，保留同目录的 `CamabTray.exe` 和 `Camab.exe`。
Linux：解压后在图形会话执行 `./CamabTray`；使用 `./Camab preflight --no-camera --json` 检查亮度接口。
macOS：先安装 `brew install brightness`，再打开 `.app`。系统可能要求允许摄像头访问。应用未经过 Apple 公证；系统拒绝运行时，可以使用下方源码方案。

Windows: extract and open `start-camab.exe`. Linux: extract and run `./CamabTray` in a graphical session. macOS: install the brightness helper, then open the app and approve camera access. The app is not Apple-notarized; source execution is available if the system refuses the bundle.

## Linux 环境 / Linux setup

Ubuntu/Debian 示例（由用户在目标设备执行）：

```sh
sudo apt install brightnessctl ddcutil python3-tk libgl1 libglib2.0-0 fonts-noto-cjk
```

优先直接读写 `/sys/class/backlight`；无写权限时调用 `brightnessctl`，由其使用系统已配置的设备权限或 logind。程序不会运行 sudo，也不会自动修改组、udev 或设备权限。外屏需要显示器菜单启用 DDC/CI，以及当前用户可访问相应 I²C 总线。安装工具后仍有权限错误时，按发行版工具说明配置权限。
Backlight control uses sysfs first, then brightnessctl with the system's existing permissions/logind. The app never invokes sudo or edits groups, udev rules or permissions. External monitors require DDC/CI and access to their I²C bus. Follow the distribution/tool documentation if access is denied.

V4L2 需要 `/dev/video*` 访问权限。Wayland/X11 均可使用硬件背光接口；托盘是否可用取决于桌面。托盘不可用时，“最小化窗口”保留窗口，关闭窗口会退出。
V4L2 needs camera-device access. Hardware brightness control works independently of Wayland/X11; tray support depends on the desktop. Without a usable tray, the minimize action keeps the window available and closing quits.

## macOS 环境 / macOS setup

```sh
brew install brightness
```

`brightness` 由 [上游项目](https://github.com/nriley/brightness) 维护 Intel/Apple Silicon 的系统 API 兼容性。普通第三方外屏不保证可用；未识别的显示器不会被当作可控制设备。系统的自动亮度、True Tone 或其他调光工具可能与本软件同时调整屏幕，可按使用需要选择一个自动亮度控制方。
The upstream helper maintains macOS API compatibility. Ordinary third-party external monitors are not guaranteed to work. Unrecognized displays are not treated as controllable. System automatic brightness or other control apps can compete with this app; choose a preferred controller for your setup.

相机权限：系统设置 → 隐私与安全性 → 摄像头。源码运行时，权限可能属于 Terminal/Python；调整权限后重新启动程序。
Camera access: System Settings → Privacy & Security → Camera. Source execution may use Terminal/Python's permission; restart after changing it.

## 从源码安装 / Source installation

需要 Python 3.10+（建议 3.12）及 Tk。macOS 可使用 python.org 的 Python，或 `brew install python@3.12 python-tk@3.12`。
Requires Python 3.10+ (3.12 recommended) and Tk.

```sh
git clone https://github.com/kindlysame55-tech/camera-auto-brightness.git
cd camera-auto-brightness
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m camab preflight --no-camera --json
.venv/bin/python -m camab tray
```

Linux 可运行 `sh scripts/start.sh`。macOS 可先 `chmod +x scripts/start.command`，以后双击该文件。
Linux can use `sh scripts/start.sh`; macOS can make `scripts/start.command` executable once and double-click it later.

高级设置支持摄像头序号、视频源、亮度接口和显示器标识。留空标识会自动选择；指定的显示器断开时不会改写另一块屏幕。改变设备后自动重新连接。
Advanced settings include camera index, optional video source, brightness backend and monitor key. An empty key chooses automatically; a missing explicit display never redirects writes to another display. Device changes trigger reconnection.

```sh
.venv/bin/python -m camab run --backend sysfs --monitor sysfs:intel_backlight
.venv/bin/python -m camab run --backend ddcutil --monitor ddcutil:6
.venv/bin/python -m camab run --backend macos
```

## 配置 / Configuration

| 系统 | 默认目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%/camera-auto-brightness/` |
| Linux | `$XDG_CONFIG_HOME/camera-auto-brightness/`，默认 `~/.config/camera-auto-brightness/` |
| macOS | `~/Library/Application Support/camera-auto-brightness/` |

`CAMAB_CONFIG` 或 `--config` 可指定配置文件。运行 `camab config path` 查看实际路径。图像在本机处理；正常运行不保存或上传相机图像。
Use `CAMAB_CONFIG` or `--config` for an explicit file; `camab config path` prints its location. Camera images are processed locally and are not saved or uploaded during normal operation.
