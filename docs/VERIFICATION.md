# 验证记录 / Verification

## 2026-10-28 · v2.2.0

- 四个平台原生测试与打包均通过：[CI 记录](https://github.com/kindlysame55-tech/camera-auto-brightness/actions/runs/40640078439)。Windows 700 通过、1 跳过；Linux 690 通过、11 跳过；Mac ARM64 与 Intel 各 689 通过、12 跳过。跳过项是平台限定检查。
  Native tests and packaging passed on all four targets. Windows: 700 passed/1 skipped; Linux: 690/11; each Mac architecture: 689/12. Skips are platform-specific checks.
- 四个平台均通过冻结程序的界面自检。Mac 还运行 `codesign --verify --deep --strict` 并直接测试 .app 内的窗口程序；打包不再修改签名后的资源。
  Frozen GUI smoke checks passed on all targets. Mac bundles passed signature-integrity verification and the bundled GUI executable's smoke check. Packaging preserves signed resources.
- 本机 Windows：真实摄像头可取有效画面（非黑帧）；WMI 调光 51% → 47%，回读正确并恢复 51%。未保存或上传相机图像。
  Local Windows: valid camera frames; WMI wrote and read back 51% → 47%, then restored 51%. No camera images were saved or uploaded.
- 界面测试覆盖小窗口、大字体、校准按钮与提示可达性。网页本轮检查 390 和 1440 像素宽度、中英文切换，无横向溢出、丢失图片或控制台错误；保留原 Windows 下载地址。
  UI regressions cover compact windows, enlarged fonts and reachable calibration actions/status. Website checked at 390/1440 px in Chinese and English, without horizontal overflow, missing images or console errors. The Windows download URL is preserved.
- [测光函数基准](performance.md)：中位耗时减少约 21%–26%，峰值缓冲内存减少约 29%–32%；不代表整机 CPU 或功耗同比下降。
  Frame-analysis benchmarks improve median time by about 21–26% and peak buffer memory by 29–32%; these are not whole-system CPU or energy results.
- Linux/Mac 的真实摄像头、显示器、权限、睡眠唤醒及各桌面托盘行为仍需在目标硬件上验证。Mac 应用的本地签名校验不等于 Apple 公证。要求见[跨平台指南](cross-platform.md)。
  Real Linux/Mac hardware, permissions, sleep/wake and desktop integration need target-device validation. Bundle signature integrity is not Apple notarization. See the platform guide.

以下为旧版本记录。 / Earlier release records follow.

## 2026-10-23

- 源码回归测试：655 项通过、1 项跳过；配置与日志隔离。此次发布准备没有重新测试真实摄像头或物理显示器调光。
  Source regression suite: 655 passed, 1 skipped, with isolated configuration and logs. Publication preparation did not repeat live camera or physical display brightness tests.
- 便携包：使用 2026-10-13 已构建程序；ZIP CRC 检查通过，包内 CamabTray.exe 与本地构建的 SHA-256 一致。
  Portable package: uses the build from 2026-10-13; ZIP CRC passed, and the included CamabTray.exe SHA-256 matches the local build.
- 网站：检查中英文桌面和手机排版；320、390、664、1440 像素宽度无横向溢出。检查语言切换、键盘滑块操作及 FAQ 展开。
  Website: checked Chinese and English desktop/mobile layouts; no horizontal overflow at widths of 320, 390, 664 and 1440 pixels. Language switching, keyboard slider operation and FAQ expansion were checked.
- 网站不访问摄像头或系统亮度接口。网页滑块仅调整截图预览明暗，图片明确标注模拟状态。
  The website does not access the camera or system brightness APIs. Its slider changes only screenshot preview brightness; illustrative state is explicitly labeled.
- 独立界面审查结论：可发布，无需修正的实质问题。初始自动检查发现的手机溢出已修复并复验。
  Independent interface review: ship, with no material fixes required. Mobile overflow found during initial checks was fixed and rechecked.

## 范围 / Scope

测试通过不代表所有摄像头、外接屏、扩展坞或混合 DPI 显示器都已验证。设备兼容性需在对应硬件上测试；没有进行整机节能或健康效果测量。
Passing tests does not establish compatibility with every camera, external monitor, dock or mixed-DPI setup. Verify against the actual hardware. No whole-system power-saving or health-effect measurements were performed.

ZIP SHA-256:

```text
109557d759e353a5f4cebfe2fab67a14539f6c2b7297dfd75d5fb969215526e7
```
