# 验证记录 / Verification

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
