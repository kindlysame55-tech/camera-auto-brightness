# v2.2.0 · 跨平台与性能 / Cross-platform and performance

- Windows：保留 WMI 内屏、DDC/CI 外屏与现有 EXE 启动方式。
- Linux：新增内屏 sysfs 背光接口、brightnessctl 权限辅助与 ddcutil 外屏支持，摄像头优先 V4L2。
- macOS：新增 `brightness` 工具适配，支持兼容的内屏/Apple 显示器，摄像头优先 AVFoundation。提供 Apple Silicon 与 Intel 独立构建。
- 界面：高级设置可选摄像头、视频源、亮度接口与显示器；调整设备后自动重连。支持 Linux 滚轮、macOS 字体与快捷键；没有可用托盘时最小化按钮不再退出。
- 性能：先裁剪再分配测光缓冲区，保留相同的亮度统计；后台操作完成反馈更及时。DDC/CI 保留明确回读，避免重复验证与写入时反复枚举。

Windows retains its WMI/DDC/CI backends and portable EXE launcher. Linux adds sysfs/brightnessctl and ddcutil; macOS uses the nriley/brightness helper with Intel and Apple Silicon builds. Platform-native camera APIs, configuration directories, fonts, wheel handling, keyboard shortcuts and device settings improve portability. Frame analysis now allocates cropped buffers while preserving its statistics.

## 使用 / Setup

- Windows：解压 `camera-auto-brightness-windows.zip`，双击 `start-camab.exe`。
- Linux x86_64：解压 TAR.GZ，在图形桌面运行 `./CamabTray`。内屏需要背光写权限或 `brightnessctl`；外屏需要 `ddcutil`、DDC/CI 与 I²C 权限。建议 Ubuntu 22.04 或更新发行版。
- macOS：按 CPU 选择 ZIP，先执行 `brew install brightness`，再打开 `.app` 并允许摄像头访问。应用未经过 Apple 公证；若系统拒绝运行，可按源码指南启动。

Portable builds include Python. Linux still needs system GUI libraries, a CJK font and device permissions; macOS needs the brightness helper. See [cross-platform setup](https://github.com/kindlysame55-tech/camera-auto-brightness/blob/main/docs/cross-platform.md).

## 验证边界 / Validation limits

发布流程为每个系统运行自动测试、原生打包与不访问硬件的界面冒烟检查。云端虚拟机没有真实摄像头和显示器，因此这些检查不等于真实硬件兼容认证。macOS 普通第三方外屏不保证可调；Linux 的权限与桌面托盘支持因发行版而异。

The release pipeline runs automated tests, native packaging and hardware-free GUI smoke checks on each OS. Hosted runners have no real webcam or monitor, so this is not real hardware certification. Ordinary third-party Mac monitors and Linux desktop tray integrations vary.

所有压缩包的 SHA-256 均见 `SHA256SUMS.txt`。 / SHA-256 checksums are in `SHA256SUMS.txt`.
