# v2.2.1 · 启动修复 / Startup fix

修复 v2.2.0 在启动托盘时出现 `Icon has no attribute HAS_DEFAULT`，导致程序打不开的问题。正确使用 pystray 的 `HAS_DEFAULT_ACTION` 能力标志，并在托盘初始化失败时保留主窗口。

Fixes the v2.2.0 startup crash caused by reading the nonexistent `Icon.HAS_DEFAULT` attribute. The app now uses the documented `HAS_DEFAULT_ACTION` capability, and keeps its main window available if tray initialization fails.

- 新增正常托盘启动、默认动作支持/缺失、构造失败和启动失败的回归检查。
- 界面自检现在也初始化并清理托盘，不再跳过导致此次崩溃的路径。
- Added regressions for the real tray API contract, optional capabilities, construction failures and startup failures.
- GUI smoke checks now initialize and clean up the tray, covering the path missed by previous checks.

请下载新包完整解压，替换 v2.2.0。相机、亮度曲线和配置格式保持兼容。
Extract the new package in full to replace v2.2.0. Camera settings, brightness curves and configuration remain compatible.

Windows: 解压后双击 start-camab.exe。Linux/macOS 的系统工具与硬件要求见[平台指南](https://github.com/kindlysame55-tech/camera-auto-brightness/blob/main/docs/cross-platform.md)。Mac 应用仍未经过 Apple 公证。

Download checksums are provided in SHA256SUMS.txt. Native CI checks do not establish compatibility with all physical camera/display devices.
