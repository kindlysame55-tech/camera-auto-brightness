# v2.3.0 · 开机自启动、便携包瘦身与性能优化 / Auto-start, smaller package & faster analysis

新增开机自启动开关，大幅缩小便携安装压缩包体积并消除运行期解压开销，同时降低测光内存占用。所有原有调光、校准与硬件兼容功能保持不变。

Adds built-in launch-at-startup controls, cuts the portable package footprint in half while removing runtime decompression overhead, and lowers frame-analysis memory usage. All existing brightness control, calibration and device features remain intact.

## 更新内容 / What's new

- **开机自启动 / Launch at startup**
  - 主界面「过渡速度」下方直接提供显眼的「开机自启动」开关按钮，托盘右键菜单与「设置 → 日常」页面同步联动。
  - 新增命令行子命令 `camab autostart [status|on|off]`。
  - Windows 使用内置 `winreg` 写入当前用户 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\CamabTray`（无需管理员权限，无后台脚本弹窗）；Linux 写入 `$XDG_CONFIG_HOME/autostart/*.desktop`；macOS 写入 `~/Library/LaunchAgents/*.plist`。
  - Prominent toggle in the main window, tray context menu, Settings (`日常` tab), and `camab autostart [status|on|off]` CLI using native per-user registration.

- **体积瘦身与纯净便携包 / Smaller portable package**
  - Windows 与 Linux 打包改为共享目录 `onedir` 布局，`Camab` 与 `CamabTray` 共用同一份 `_internal/` 运行时，彻底消除 `onefile` 每次启动及摄像头探活时解压 ~183 MB 临时目录的开销。
  - 剔除未使用的 PIL 编解码器、MFC/pythonwin 残留、`tcltest`、`tzdata`、多语言消息及 `.dist-info`，Windows 便携包压缩至约 69.1 MB，且包内不含任何测试文件。
  - Shared-runtime packaging eliminates ~183 MB `_MEI` extraction on launch/subprocess probe and removes unused codecs, test modules and metadata, shrinking the Windows portable ZIP to ~69.1 MB with zero test folders inside.

- **测光与黑帧性能提升 / Lower runtime overhead**
  - `luma_ratio` 融合 uint8 灰度/通道归一化与原地标准差计算，峰值缓冲内存下降约 62.5%，中位耗时降低约 29%–30%。
  - `_check_black` 在帧峰值 `< 1.0` 时短路跳过冗余扫描。
  - In-place buffer reuse in `luma_ratio` cuts peak buffer memory by ~62.5% and median frame-analysis time by ~29%–30%; black-frame checks short-circuit zero-peak frames.

Windows: 完整解压后双击 `start-camab.exe`（保留同目录下的 `Camab.exe`、`CamabTray.exe` 与 `_internal/`）。Linux/macOS 的系统工具与硬件要求见[平台指南](https://github.com/kindlysame55-tech/camera-auto-brightness/blob/main/docs/cross-platform.md)。Mac 应用未经过 Apple 公证。

Download checksums are provided in `SHA256SUMS.txt`. Native CI checks do not establish compatibility with all physical camera/display devices.
