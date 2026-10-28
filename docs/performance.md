# v2.2.0 性能与验证 / Performance and validation

测量日期：2026-10-28。Windows、Python 3.12.14、NumPy 2.5.3；固定随机种子的合成 BGR 图像，每种分辨率预热 5 次、测量 80 次。数字是测光函数的基准，不代表整机 CPU 或功耗的同等下降。
Measured on Windows with synthetic BGR frames, a fixed seed, five warm-up calls and 80 measured calls. These are function benchmarks, not whole-system CPU or energy measurements.

| 分辨率 / Resolution | 中位时间：之前 → 之后 / Median time | 峰值缓冲内存：之前 → 之后 / Peak buffer memory |
| --- | --- | --- |
| 320×240（默认 / default） | 2.2511 → 1.6578 ms（−26.4%） | 1,843,856 → 1,253,852 bytes（−32.0%） |
| 1920×1080 | 68.7557 → 54.1345 ms（−21.3%） | 49,767,056 → 35,334,652 bytes（−29.0%） |

优化把裁剪放在浮点缓冲区和通道运算之前，跳过无符号整数无需执行的范围裁剪。浮点输入仍沿用整帧峰值确定归一化范围；分位数、均值、标准差和调用方帧的不变性均保留。
The analysis crops before allocating float/channel buffers and skips unnecessary clipping of unsigned integer inputs. Float normalization still uses the original full-frame scale. Percentiles, mean, deviation and input immutability are preserved.

同一合成帧的统计结果保持一致。脚本 scripts/benchmark_analysis.py 可在其他系统复测，实际结果会受 CPU、负载、NumPy 与相机驱动影响。
Statistics are unchanged for the benchmark frames. Re-run the script on other systems; CPU, load, NumPy and camera drivers affect timings.

前端空闲时仍保持低频刷新，有后台操作时缩短结果轮询间隔；Linux 外屏写入使用固定 I²C 总线和一次明确回读，不在每次写入时重复枚举。
The UI keeps a low idle refresh rate and polls completions promptly during background operations. Linux DDC/CI writes use stable bus identities and one explicit readback, without rediscovering displays for every write.

自动测试覆盖合成明暗闭环、黑帧保持、手动接管、滑块与缩放、权限失败、超时、平台目录、显示器断开和数值换算。原生 CI 与打包不构成真实硬件兼容认证。
Tests cover synthetic light changes, invalid-frame holding, overrides, UI scaling, permissions, timeouts, platform paths, hotplug and unit conversion. Native CI and packaging are not real hardware certification.
