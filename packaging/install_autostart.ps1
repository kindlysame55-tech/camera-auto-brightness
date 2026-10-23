#Requires -Version 5.1
<#
.SYNOPSIS
    给「摄像头自动亮度」托盘版注册 / 取消开机自启（只动当前用户，不需要管理员）。

.DESCRIPTION
    默认写注册表 HKCU\Software\Microsoft\Windows\CurrentVersion\Run，值名 CamabTray。
    加 -UseStartupFolder 改成在「启动」文件夹放快捷方式（等价效果，二选一）。
    两个位置都幂等：内容一样就什么都不改，只打印结论。

    没有打包 exe 也能用：自动退回 `pythonw.exe -m camab tray`（需要项目内 .venv）。

.PARAMETER Remove
    取消开机自启（注册表值和启动文件夹快捷方式都会清掉）。

.PARAMETER Status
    只看当前状态，不做任何修改。

.PARAMETER ExePath
    手动指定要自启的 CamabTray.exe。默认按 dist\CamabTray.exe →
    dist\CamabTray\CamabTray.exe → .venv\pythonw.exe 的顺序自动找。

.PARAMETER UseStartupFolder
    用「启动」文件夹快捷方式，而不是注册表 Run 项。

.PARAMETER StartNow
    注册完立刻启动一次（不影响已有的运行实例）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\install_autostart.ps1
    注册开机自启（注册表 Run 项）

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\install_autostart.ps1 -Status
    查看当前状态

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\install_autostart.ps1 -Remove
    取消开机自启
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [switch]$Remove,
    [switch]$Status,
    [string]$ExePath = '',
    [switch]$UseStartupFolder,
    [switch]$StartNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------------------- #
# 控制台 UTF-8（项目路径含全角字符 ｄｓｈ / ｗｏｒｋ）
# --------------------------------------------------------------------------- #
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
} catch {
    Write-Verbose "控制台编码设置跳过：$($_.Exception.Message)"
}

$ValueName = 'CamabTray'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Write-Head {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 66) -ForegroundColor DarkGray
    Write-Host " $Text" -ForegroundColor Cyan
    Write-Host ('=' * 66) -ForegroundColor DarkGray
}

# --------------------------------------------------------------------------- #
# 1. 决定自启命令
# --------------------------------------------------------------------------- #
function Resolve-AutostartTarget {
    param([string]$Explicit)

    $candidates = @(
        $Explicit,
        (Join-Path $ProjectRoot 'dist\CamabTray.exe'),
        (Join-Path $ProjectRoot 'dist\CamabTray\CamabTray.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) {
            return [pscustomobject]@{ Exe = $c; Args = ''; Kind = 'exe' }
        }
    }

    $pythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $pythonw -PathType Leaf) {
        return [pscustomobject]@{ Exe = $pythonw; Args = '-m camab tray'; Kind = 'pythonw' }
    }

    throw ("没找到可自启的程序。请看下面两条路：`n" +
           "  ① 先打包：packaging\build_exe.ps1  → 生成 dist\CamabTray.exe`n" +
           "  ② 或者先建虚拟环境（要有 .venv\Scripts\pythonw.exe）")
}

function Build-CommandLine {
    param([pscustomobject]$Target)
    if ($Target.Args) {
        return '"{0}" {1}' -f $Target.Exe, $Target.Args
    }
    return '"{0}"' -f $Target.Exe
}

# --------------------------------------------------------------------------- #
# 2. 注册表位置
# --------------------------------------------------------------------------- #
function Get-RunEntry {
    if (-not (Test-Path -LiteralPath $RunKey)) { return $null }
    $item = Get-ItemProperty -LiteralPath $RunKey -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    $prop = $item.PSObject.Properties[$ValueName]
    if ($null -eq $prop) { return $null }
    return [string]$prop.Value
}

function Set-RunEntry {
    param([string]$CommandLine)
    if (-not (Test-Path -LiteralPath $RunKey)) {
        New-Item -Path $RunKey -Force | Out-Null
    }
    New-ItemProperty -LiteralPath $RunKey -Name $ValueName -Value $CommandLine `
        -PropertyType String -Force | Out-Null
}

function Remove-RunEntry {
    if (-not (Test-Path -LiteralPath $RunKey)) { return $false }
    $item = Get-ItemProperty -LiteralPath $RunKey -ErrorAction SilentlyContinue
    if ($null -eq $item -or $null -eq $item.PSObject.Properties[$ValueName]) { return $false }
    Remove-ItemProperty -LiteralPath $RunKey -Name $ValueName -Force
    return $true
}

# --------------------------------------------------------------------------- #
# 3. 「启动」文件夹位置
# --------------------------------------------------------------------------- #
function Get-StartupLnkPath {
    $startup = [Environment]::GetFolderPath('Startup')
    if (-not $startup) { throw '拿不到「启动」文件夹路径。' }
    return (Join-Path $startup '摄像头自动亮度.lnk')
}

function Get-LnkInfo {
    param([string]$LnkPath)
    if (-not (Test-Path -LiteralPath $LnkPath)) { return $null }
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LnkPath)
    return [pscustomobject]@{ Target = $lnk.TargetPath; Args = $lnk.Arguments }
}

function New-LnkFile {
    param([string]$LnkPath, [pscustomobject]$Target)
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LnkPath)
    $lnk.TargetPath = $Target.Exe
    $lnk.Arguments = $Target.Args
    $lnk.WorkingDirectory = (Split-Path -Parent $Target.Exe)
    $lnk.Description = '摄像头自动亮度（托盘）'
    $lnk.Save()
}

# --------------------------------------------------------------------------- #
# 4. 执行
# --------------------------------------------------------------------------- #
$lnkPath = Get-StartupLnkPath

if ($Status) {
    Write-Head '摄像头自动亮度 —— 开机自启状态'
    $run = Get-RunEntry
    if ($run) {
        Write-Host "  注册表 Run 项  : 已注册" -ForegroundColor Green
        Write-Host "    值名  : $ValueName"
        Write-Host "    命令  : $run"
    } else {
        Write-Host '  注册表 Run 项  : 未注册' -ForegroundColor Yellow
    }
    $info = Get-LnkInfo -LnkPath $lnkPath
    if ($info) {
        Write-Host "  启动文件夹     : 已有快捷方式" -ForegroundColor Green
        Write-Host "    路径  : $lnkPath"
        Write-Host "    目标  : $($info.Target) $($info.Args)"
    } else {
        Write-Host '  启动文件夹     : 没有快捷方式' -ForegroundColor Yellow
    }
    foreach ($p in @((Join-Path $ProjectRoot 'dist\CamabTray.exe'),
                     (Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'))) {
        $mark = if (Test-Path -LiteralPath $p) { '存在' } else { '不存在' }
        Write-Host "  $mark : $p"
    }
    Write-Host ''
    exit 0
}

if ($Remove) {
    Write-Head '取消开机自启'
    $removed = 0
    if (Remove-RunEntry) {
        Write-Host "  已删除注册表项：HKCU\...\Run\$ValueName" -ForegroundColor Green
        $removed++
    } else {
        Write-Host '  注册表项本来就不存在（无需处理）' -ForegroundColor DarkGray
    }
    if (Test-Path -LiteralPath $lnkPath) {
        if ($PSCmdlet.ShouldProcess($lnkPath, '删除启动文件夹快捷方式')) {
            Remove-Item -LiteralPath $lnkPath -Force
            Write-Host "  已删除快捷方式：$lnkPath" -ForegroundColor Green
            $removed++
        }
    } else {
        Write-Host '  启动文件夹本来就没有快捷方式（无需处理）' -ForegroundColor DarkGray
    }
    Write-Host ''
    if ($removed -eq 0) {
        Write-Host '  结论：本来就没开自启，现在是干净的关闭状态。' -ForegroundColor Green
    } else {
        Write-Host '  结论：已取消开机自启（下次开机不会再自动启动）。' -ForegroundColor Green
    }
    Write-Host ''
    exit 0
}

# ---- 注册（默认动作）------------------------------------------------------- #
Write-Head '注册开机自启（仅当前用户，不需要管理员）'

$target = Resolve-AutostartTarget -Explicit $ExePath
$cmdline = Build-CommandLine -Target $target
Write-Host "  自启目标 : $($target.Exe)"
if ($target.Args) { Write-Host "  附加参数 : $($target.Args)" }
if ($target.Kind -eq 'pythonw') {
    Write-Host '  注意     : 没找到打包好的 CamabTray.exe，退回 pythonw 方式（依赖项目内 .venv，' -ForegroundColor Yellow
    Write-Host '             移动项目文件夹后需重新运行本脚本）。' -ForegroundColor Yellow
}

$changed = $false

if ($UseStartupFolder) {
    $info = Get-LnkInfo -LnkPath $lnkPath
    $same = $false
    if ($info) {
        $same = ($info.Target -eq $target.Exe) -and (([string]$info.Args) -eq ([string]$target.Args))
    }
    if ($same) {
        Write-Host "  启动文件夹快捷方式已是最新，无需改动：$lnkPath" -ForegroundColor Green
    } elseif ($PSCmdlet.ShouldProcess($lnkPath, '创建/更新启动快捷方式')) {
        New-LnkFile -LnkPath $lnkPath -Target $target
        Write-Host "  已写入快捷方式：$lnkPath" -ForegroundColor Green
        $changed = $true
    }
    # 换位置时顺手清掉注册表项，避免两处都自启
    if (Remove-RunEntry) {
        Write-Host "  已清掉旧的注册表 Run 项（避免重复自启）" -ForegroundColor Green
        $changed = $true
    }
} else {
    $existing = Get-RunEntry
    if ($existing -eq $cmdline) {
        Write-Host "  注册表里已经是同样的命令，无需改动（幂等）" -ForegroundColor Green
        Write-Host "    $existing"
    } elseif ($PSCmdlet.ShouldProcess("$RunKey\$ValueName", '写入自启命令')) {
        if ($existing) { Write-Host "  旧值：$existing" -ForegroundColor DarkGray }
        Set-RunEntry -CommandLine $cmdline
        Write-Host "  已写入注册表：HKCU\...\Run\$ValueName" -ForegroundColor Green
        Write-Host "    $cmdline"
        $changed = $true
    }
}

if ($StartNow) {
    if ($PSCmdlet.ShouldProcess($target.Exe, '立即启动')) {
        $spArgs = @{ FilePath = $target.Exe }
        if ($target.Args) { $spArgs['ArgumentList'] = $target.Args.Split(' ') }
        Start-Process @spArgs | Out-Null
        Write-Host '  已尝试启动托盘版。' -ForegroundColor Green
    }
}

Write-Host ''
if ($changed) {
    Write-Host '  结论：开机自启已就绪。' -ForegroundColor Green
} else {
    Write-Host '  结论：无需改动（已经是想要的状态）。' -ForegroundColor Green
}
Write-Host '  查看方式：任务管理器 → 启动 应用，应该能看到 CamabTray'
Write-Host '  取消自启：packaging\install_autostart.ps1 -Remove'
Write-Host ''
exit 0
