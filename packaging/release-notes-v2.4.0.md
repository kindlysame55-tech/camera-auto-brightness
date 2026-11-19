# v2.4.0 · 亮度记忆与稳定调光 / Brightness memory and steadier adjustment

- 光照稳定时，主窗口滑块手动设定的亮度会保存到本地配置。相近的光照会参考该偏好，离开该范围后逐渐回归原来的曲线。设置 → 校准提供关闭与清除入口。
- 较大的照度跳变需要两次方向一致、数值接近的采样才会接纳。随后仍按原有的平滑、迟滞和步进限制调光。界面显示用于调光的平滑估算值。
- Windows 提供标准 ZIP 和内容相同的 7z 两种离线包。完整解压后双击 start-camab.exe。保留所有 EXE 与 _internal 文件夹。

- Manual slider adjustments in stable light save a local preference. The app applies it around the recorded light level and gradually returns to the base curve farther away. Settings → Calibration includes controls to disable or clear preferences.
- Large readings must be consistent across two samples before entering the existing smoothed, rate-limited control loop. The UI reports the smoothed light estimate used for adjustment.
- Windows offers both ZIP and 7z archives with the same offline app files. Extract the archive in full and run start-camab.exe.

SHA256SUMS.txt lists checksums for all archives.
