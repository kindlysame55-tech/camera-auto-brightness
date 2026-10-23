#Requires -Version 5.1
<#
.SYNOPSIS
    给「摄像头自动亮度」创建**无命令框**的桌面快捷方式。

.DESCRIPTION
    指向 scripts\start-silent.vbs（WScript 隐藏窗口启动），所以双击时
    不会出现任何黑底命令框，只会看到右下角托盘图标。

    快捷方式只动当前用户桌面，不需要管理员。

.PARAMETER Remove
    删除创建过的快捷方式。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Remove,
    [string]$Name = '摄像头自动亮度'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Target = Join-Path $ProjectRoot 'scripts\start-silent.vbs'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Link = Join-Path $Desktop "$Name.lnk"

if ($Remove) {
    if (Test-Path -LiteralPath $Link) {
        Remove-Item -LiteralPath $Link -Force
        Write-Host "已删除快捷方式：$Link"
    } else {
        Write-Host "桌面本来就没有这个快捷方式。"
    }
    return
}

if (-not (Test-Path -LiteralPath $Target)) {
    throw "找不到启动器：$Target"
}

if ($PSCmdlet.ShouldProcess($Link, '创建桌面快捷方式')) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($Link)
    $sc.TargetPath = 'wscript.exe'
    $sc.Arguments = '"' + $Target + '"'
    $sc.WorkingDirectory = $ProjectRoot
    $sc.Description = '用摄像头当环境光传感器，自动调节屏幕亮度（无命令框）'
    $sc.WindowStyle = 7          # 最小化启动，配合 vbs 的隐藏窗口
    $sc.Save()
    Write-Host "已创建：$Link"
    Write-Host "双击它即可启动（不会有命令框，只在右下角托盘出现图标）。"
    Write-Host "想开机自启请用：packaging\install_autostart.ps1"
}
