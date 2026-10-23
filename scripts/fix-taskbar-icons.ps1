# 修复任务栏通知区域（网络/音量/电池图标显示异常）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File fix-taskbar-icons.ps1            # 只体检，不动任何东西
#   powershell -ExecutionPolicy Bypass -File fix-taskbar-icons.ps1 -Repair    # 重启资源管理器（会关掉已打开的文件资源管理器窗口）
#
# 说明：
#   * 只重启 explorer.exe（Windows 外壳），不碰网络设置、驱动、服务。
#   * 重启前会把通知区域设置导出成 .reg 备份，可随时回滚。

param(
    [switch]$Repair
)

$ErrorActionPreference = 'Continue'
$base = 'HKCU:\Control Panel\NotifyIconSettings'
$backupDir = Join-Path $env:LOCALAPPDATA 'camera-auto-brightness'

Write-Output '================ 1) 网络本身通不通 ================'
$ping = Test-Connection -ComputerName 223.5.5.5 -Count 2 -Quiet -ErrorAction SilentlyContinue
Write-Output ("  外网 ping            : " + $(if ($ping) { '通（说明网络没坏，是图标显示问题）' } else { '不通（这是真网络问题，先查网络）' }))
Get-NetConnectionProfile -ErrorAction SilentlyContinue |
    Select-Object InterfaceAlias, IPv4Connectivity |
    Format-Table -AutoSize

Write-Output '================ 2) Windows 自己认为有没有网 ================'
$k = 'HKLM:\SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet'
$q = Get-ItemProperty $k -ErrorAction SilentlyContinue
Write-Output ("  NCSI 主动探测 : " + $(if ($null -ne $q.EnableActiveProbing) { $q.EnableActiveProbing } else { '未设置(默认开)' }))
foreach ($h in @('www.msftconnecttest.com', 'dns.msftncsi.com')) {
    $ok = Test-NetConnection -ComputerName $h -Port 80 -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
    Write-Output ("  探测 {0} : {1}" -f $h, $ok)
}

Write-Output '================ 3) 通知区域图标状态 ================'
$all = Get-ChildItem $base -ErrorAction SilentlyContinue | ForEach-Object { Get-ItemProperty $_.PSPath }
Write-Output ("  通知区域条目总数 : " + ($all | Measure-Object).Count)
Write-Output ("  常显(IsPromoted) : " + (($all | Where-Object { $_.IsPromoted -eq 1 }) | Measure-Object).Count)
$sys = $all | Where-Object { $_.ExecutablePath -like '*explorer.exe' }
Write-Output ("  系统内置图标槽   : " + ($sys | Measure-Object).Count)
$stale = $all | Where-Object { $_.ExecutablePath -like '*camera-auto-brightness*' -or $_.ExecutablePath -like '*Camab*' }
Write-Output ("  自动亮度残留条目 : " + ($stale | Measure-Object).Count + "（下面会顺手清掉）")
foreach ($s in $stale) { Write-Output ("      " + $s.ExecutablePath) }

Write-Output '================ 4) 备份 + 清理残留 ================'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $backupDir "NotifyIconSettings-backup-$stamp.reg"
& reg.exe export 'HKCU\Control Panel\NotifyIconSettings' $backup /y | Out-Null
Write-Output ("  已备份 -> " + $backup)
foreach ($s in $stale) {
    Remove-Item -LiteralPath $s.PSPath -Recurse -Force -ErrorAction SilentlyContinue
    Write-Output ("  已清理 -> " + $s.ExecutablePath)
}

if (-not $Repair) {
    Write-Output ''
    Write-Output '================ 体检完成 ================'
    Write-Output '  加 -Repair 参数可重启资源管理器，让通知区域重新注册所有图标：'
    Write-Output '      powershell -ExecutionPolicy Bypass -File fix-taskbar-icons.ps1 -Repair'
    Write-Output '  注意：重启资源管理器会关掉已打开的文件资源管理器窗口（其它程序不受影响）。'
    exit 0
}

Write-Output ''
Write-Output '================ 5) 重启资源管理器 ================'
Get-Process -Name explorer -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Output ("  重启 explorer PID " + $_.Id)
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
if (-not (Get-Process -Name explorer -ErrorAction SilentlyContinue)) {
    Write-Output '  重新拉起资源管理器…'
    Start-Process explorer.exe
    Start-Sleep -Seconds 5
}
$n = (Get-Process -Name explorer -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Output ("  当前 explorer 进程数 : " + $n)

Write-Output ''
Write-Output '================ 完成 ================'
Write-Output '  通知区域已重新注册。请看一下网络图标是否正常。'
Write-Output '  如果图标只是被收进了"^"溢出菜单：'
Write-Output '      设置 → 个性化 → 任务栏 → 其他系统托盘图标 → 把「网络」打开。'
Write-Output ("  回滚通知区域设置：双击 " + $backup)
