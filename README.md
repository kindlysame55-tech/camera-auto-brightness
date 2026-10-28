# Camera Auto Brightness · 摄像头自动亮度

用摄像头估算环境光，平滑调节 **Windows、Linux 和 macOS** 的屏幕亮度。可以随时拖动滑块手动接管，也能根据房间光线校准曲线。
Estimate ambient light with your webcam and smoothly adjust display brightness on Windows, Linux and macOS. A manual slider and calibration let you tune it for your room.

[介绍网站 / Website](https://kindlysame55-tech.github.io/camera-auto-brightness/) · [下载 / Downloads](https://github.com/kindlysame55-tech/camera-auto-brightness/releases) · [跨平台指南 / Platform guide](docs/cross-platform.md) · [测试与构建 / CI](https://github.com/kindlysame55-tech/camera-auto-brightness/actions)

网站与文档中英双语；桌面界面和多数命令行提示目前使用中文。
The website and documentation are bilingual. The desktop interface and most CLI messages currently use Chinese.

## 下载与运行 / Download and run

| 系统 / System | 安装包 / Package | 运行条件 / Requirements |
| --- | --- | --- |
| Windows 10/11 x64 | [Windows ZIP](https://github.com/kindlysame55-tech/camera-auto-brightness/releases/latest/download/camera-auto-brightness-windows.zip) | 解压后双击 start-camab.exe，其余文件保留在同目录 / extract and launch |
| Linux x86_64 | [Linux TAR.GZ](https://github.com/kindlysame55-tech/camera-auto-brightness/releases/latest/download/camera-auto-brightness-linux-x86_64.tar.gz) | 图形桌面、背光权限或 brightnessctl；外屏使用 ddcutil / graphical session and permissions |
| macOS Apple Silicon | [ARM64 ZIP](https://github.com/kindlysame55-tech/camera-auto-brightness/releases/latest/download/camera-auto-brightness-macos-arm64.zip) | brew install brightness，打开 .app 并允许摄像头访问 / helper and camera permission |
| macOS Intel | [Intel ZIP](https://github.com/kindlysame55-tech/camera-auto-brightness/releases/latest/download/camera-auto-brightness-macos-x86_64.zip) | 同上 / same requirements |

便携包包含 Python。Linux 需要系统 GUI 库和中文字体；macOS 应用未经过 Apple 公证，系统拒绝运行时可使用源码方案。硬件和桌面环境会影响兼容性，详见[平台指南](docs/cross-platform.md)。校验值见每个版本的 SHA256SUMS.txt。
Portable builds include Python. Linux needs system GUI libraries and a CJK font. The macOS app is not Apple-notarized; source execution is available if the system refuses the bundle. Hardware compatibility varies. Checksums are included in each release.

![主界面 / Main interface](docs/images/main.png)

*界面示例使用模拟状态。 / Screenshot shows illustrative diagnostic data.*

## 功能 / Features

- 环境光估算、校准曲线、屏幕反光补偿、防抖与逐步平滑过渡。
  Ambient-light estimation, calibration, screen-reflection compensation and gradual transitions.
- 自动调节每步最多 2 个百分点，写入间隔至少 0.5 秒，并遵守更慢的设置。
  Automatic transitions use steps of at most two percentage points, at least 0.5 seconds apart, respecting slower settings.
- 手动滑块立即接管，默认 60 秒后恢复自动；支持暂停和立即采样。
  Manual override defaults to 60 seconds; pause and sample-on-demand are supported.
- 高级设置可选择摄像头、视频源、亮度接口和显示器，修改设备后自动重连。
  Select a camera, video source, brightness backend and monitor in Advanced settings; device edits reconnect automatically.
- 可缩放、可滚动、键盘可操作；托盘不支持时仍可最小化窗口。
  Scrollable, scale-aware and keyboard-accessible UI, with safe minimization when a tray is unavailable.
- 图像在本机处理，正常运行不保存或上传图像；指定网络源时会连接该源。
  Images are processed locally and not saved or uploaded during normal operation. A selected network source is contacted.

| 平台 / Platform | 调光接口 / Brightness control | 相机接口 / Camera |
| --- | --- | --- |
| Windows | WMI / DDC/CI | DirectShow / MSMF |
| Linux | sysfs、brightnessctl、ddcutil | V4L2 |
| macOS | nriley/brightness（兼容的内屏及 Apple 显示器） | AVFoundation |

相机估算受曝光、画面内容和屏幕反光影响，并非专业照度计。Mac 的普通第三方外屏不保证可调；Linux 需要合适的背光/I²C 权限。
Camera estimates depend on exposure, scene content and screen reflection; this is not a calibrated lux meter. Ordinary third-party Mac monitors are not guaranteed to work. Linux needs appropriate backlight/I²C permissions.

## 从源码运行 / Run from source

Python 3.10+（建议 3.12），需要 Tk。 / Python 3.10+ (3.12 recommended), with Tk.

Windows PowerShell:

```powershell
git clone https://github.com/kindlysame55-tech/camera-auto-brightness.git
cd camera-auto-brightness
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m camab tray
```

Linux / macOS:

```sh
git clone https://github.com/kindlysame55-tech/camera-auto-brightness.git
cd camera-auto-brightness
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m camab tray
```

Linux 可用 sh scripts/start.sh；Mac 可将 scripts/start.command 设为可执行后双击。
Linux can use sh scripts/start.sh; Mac can run scripts/start.command after making it executable.

## 自检与排查 / Diagnostics

```sh
camab preflight --no-camera --json  # 只读亮度接口 / read-only display check
camab preflight                    # 包含摄像头探测 / include camera probe
camab devices
camab config path
camab run --backend dryrun         # 模拟调光 / simulated brightness
camab tray --selftest              # 不访问硬件的界面自检 / hardware-free GUI smoke
camab stop
```

源码环境未激活时，在前面加 .venv/bin/python -m；Windows 使用 .venv\Scripts\python.exe -m。冻结的命令行程序叫 Camab（Windows 为 Camab.exe）。全局参数放在子命令前，例如 camab --config ./my-config.json tray。
Prefix commands with the environment's Python and -m when it is not activated. The frozen CLI is Camab (Camab.exe on Windows). Global options precede the subcommand.

没有自动调光时，先检查“仅模拟调光”、暂停状态、接管倒计时和摄像头有效画面；再看“设备详情”中的具体错误。不要把模拟模式误当成硬件已调节。
If brightness does not change, check simulation, pause, override countdown and camera input, then inspect device details. Simulation does not prove hardware control.

## 开发与性能 / Development and performance

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
python scripts/benchmark_analysis.py --iterations 80
python -m PyInstaller packaging/camab.spec --noconfirm
```

[性能记录](docs/performance.md)比较测光函数的时间与缓冲区内存。GitHub Actions 在 Windows、Linux、Mac ARM64 和 Mac Intel 上测试与构建；真实相机、显示器、权限和睡眠唤醒仍需在目标设备验证。
The [performance report](docs/performance.md) measures frame-analysis time and buffer memory. CI tests and builds on four native targets; real hardware, permissions and sleep/wake behavior still require validation on the target device.
