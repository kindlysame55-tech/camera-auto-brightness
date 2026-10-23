#Requires -Version 5.1
<#
.SYNOPSIS
    把 camera-auto-brightness 打包成 dist\Camab.exe（控制台版）+ dist\CamabTray.exe（托盘版）。

.DESCRIPTION
    流程：① 检查项目内 .venv 的 Python；② 用阿里云镜像装 PyInstaller（已装则跳过）；
    ③ 调 PyInstaller 跑 packaging\camab.spec；④ 打印产物体积；
    ⑤ 真的跑一次 dist\Camab.exe preflight --no-camera 验证产物能用。

    本机 PyPI 官方源不通，所有 pip 安装都走：
        -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

.PARAMETER PythonExe
    指定 Python 解释器。默认用 <项目>\.venv\Scripts\python.exe，找不到才退回 PATH 里的 python。

.PARAMETER OneDir
    打成 onedir（dist\Camab\Camab.exe）而不是默认的 onefile。
    启动更快、不必每次解压；缺点是产物是一个文件夹，不能只拷一个 exe。

.PARAMETER Clean
    先删掉旧的 build\camab 与 dist 产物，再做干净构建。

.PARAMETER SkipInstall
    跳过 pip 安装步骤（仅当确定 PyInstaller 已就绪时用）。

.PARAMETER NoVerify
    打包后不做验证（默认会跑 preflight --no-camera + 冻结版 -c 冒烟测试）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build_exe.ps1 -Clean -OneDir
#>
[CmdletBinding()]
param(
    [string]$PythonExe = '',
    [switch]$OneDir,
    [switch]$Clean,
    [switch]$SkipInstall,
    [switch]$NoVerify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$LASTEXITCODE = 0

# --------------------------------------------------------------------------- #
# 控制台按 UTF-8 工作（路径里有全角字符 ｄｓｈ / ｗｏｒｋ，中文输出别乱码）
# --------------------------------------------------------------------------- #
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
} catch {
    Write-Verbose "控制台编码设置跳过：$($_.Exception.Message)"
}

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
$PackagingDir = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $PackagingDir
$SpecFile = Join-Path $PackagingDir 'camab.spec'
$SrcDir = Join-Path $ProjectRoot 'src'
$DistDir = Join-Path $ProjectRoot 'dist'
$WorkDir = Join-Path $ProjectRoot 'build'

# 清掉外部环境塞进来的 PYTHONPATH / PYTHONHOME：空的 PYTHONPATH 会把「当前目录」
# 塞进模块搜索路径，指向别处的 PYTHONPATH 还可能让 PyInstaller 收错包。
# 本项目靠 .venv\Lib\site-packages\_camab_src.pth 指到 src，不需要这两个变量。
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue

# 统一工作目录：不管在哪个目录双击/调用，行为一致
Set-Location -LiteralPath $ProjectRoot

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 70) -ForegroundColor DarkGray
    Write-Host " $Text" -ForegroundColor Cyan
    Write-Host ('=' * 70) -ForegroundColor DarkGray
}

function Write-Ok {
    param([string]$Text)
    Write-Host "  [OK] $Text" -ForegroundColor Green
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host "  [!!] $Text" -ForegroundColor Yellow
}

# 跑外部命令：输出直接透传到控制台（pip / PyInstaller 是分钟级的，不能缓冲），
# 非 0 退出码一律抛异常，绝不当成功。
function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [string]$ErrorMessage = '外部命令失败',
        # pip / PyInstaller 把日志写到 stderr，PowerShell 5.1 会把它们渲染成红色
        # 报错样式（其实不是错误）。打开这个开关当普通文本打印，别吓人。
        [switch]$MergeStderr
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 0
    try {
        if ($MergeStderr) {
            & $FilePath @ArgumentList 2>&1 | ForEach-Object { Write-Host ([string]$_) }
        } else {
            & $FilePath @ArgumentList
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) {
        $cmd = '"' + $FilePath + '" ' + ($ArgumentList -join ' ')
        throw ("$ErrorMessage（退出码 $code）`n  命令：$cmd")
    }
}

# --------------------------------------------------------------------------- #
# 步 0：基本情况
# --------------------------------------------------------------------------- #
Write-Step '步 0 / 6  检查项目与环境'

if (-not (Test-Path -LiteralPath $SpecFile)) {
    throw "找不到 spec 文件：$SpecFile"
}
if (-not (Test-Path -LiteralPath $SrcDir)) {
    throw "找不到源码目录：$SrcDir"
}

if (-not $PythonExe) {
    $venvPy = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPy) {
        $PythonExe = $venvPy
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $cmd) {
            throw "找不到 Python：项目内没有 .venv\Scripts\python.exe，PATH 里也没有 python。请先建虚拟环境。"
        }
        $PythonExe = $cmd.Source
    }
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "指定的 Python 解释器不存在：$PythonExe"
}
$LocalPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if ([IO.Path]::GetFullPath($PythonExe) -ieq [IO.Path]::GetFullPath($LocalPython)) {
    $LocalRuntime = @($LocalPython)
    $LocalPythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $LocalPythonw) { $LocalRuntime += $LocalPythonw }
    & (Join-Path $PSScriptRoot 'set-release-integrity.ps1') -Path $LocalRuntime
}

Write-Host "  项目根   : $ProjectRoot"
Write-Host "  Python   : $PythonExe"
Write-Host "  spec     : $SpecFile"
Write-Host "  布局     : $(if ($OneDir) { 'onedir（dist\Camab\Camab.exe）' } else { 'onefile（dist\Camab.exe，单个文件）' })"

$prevEap0 = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $pyVer = (& $PythonExe -c "import sys; sys.stdout.write(sys.version.split()[0])" 2>&1 | Out-String).Trim()
} finally {
    $ErrorActionPreference = $prevEap0
}
Write-Host "  Python 版本: $pyVer"

# --------------------------------------------------------------------------- #
# 步 1：清干净（可选）
# --------------------------------------------------------------------------- #
if ($Clean) {
    Write-Step '步 1 / 6  清理旧产物'
    foreach ($p in @((Join-Path $WorkDir 'camab'), (Join-Path $DistDir 'Camab.exe'),
                     (Join-Path $DistDir 'CamabTray.exe'), (Join-Path $DistDir 'Camab'),
                     (Join-Path $DistDir 'CamabTray'))) {
        if (Test-Path -LiteralPath $p) {
            Write-Host "  删除 $p"
            Remove-Item -LiteralPath $p -Recurse -Force
        }
    }
} else {
    Write-Step '步 1 / 6  清理旧产物（跳过，未指定 -Clean）'
}

# --------------------------------------------------------------------------- #
# 步 2：装 PyInstaller
# --------------------------------------------------------------------------- #
Write-Step '步 2 / 6  PyInstaller'

$pyiVersion = ''
if (-not $SkipInstall) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $pyiVersion = (& $PythonExe -c "import PyInstaller, sys; sys.stdout.write(PyInstaller.__version__)" 2>$null | Out-String).Trim()
    } catch {
        $pyiVersion = ''
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

if ($pyiVersion) {
    Write-Ok "PyInstaller 已安装：$pyiVersion（如需强制重装请用 -Clean 后手动 pip install）"
} elseif ($SkipInstall) {
    throw '指定了 -SkipInstall，但当前环境没有 PyInstaller。'
} else {
    Write-Host '  未检测到 PyInstaller，开始安装（阿里云镜像）...' -ForegroundColor Yellow
    Invoke-External -FilePath $PythonExe -ErrorMessage 'PyInstaller 安装失败' -MergeStderr -ArgumentList @(
        '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
        '-i', 'https://mirrors.aliyun.com/pypi/simple/',
        '--trusted-host', 'mirrors.aliyun.com',
        'pyinstaller'
    )
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $pyiVersion = (& $PythonExe -c "import PyInstaller, sys; sys.stdout.write(PyInstaller.__version__)" 2>$null | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if (-not $pyiVersion) { throw 'PyInstaller 安装后仍无法 import，请检查 pip 输出。' }
    Write-Ok "PyInstaller 安装完成：$pyiVersion"
}

# --------------------------------------------------------------------------- #
# 步 3：打包
# --------------------------------------------------------------------------- #
Write-Step '步 3 / 6  PyInstaller 打包'

if ($OneDir) {
    $env:CAMAB_PYI_ONEDIR = '1'
} else {
    Remove-Item Env:\CAMAB_PYI_ONEDIR -ErrorAction SilentlyContinue
}

$pyiArgs = @(
    '-m', 'PyInstaller', $SpecFile,
    '--noconfirm',
    '--distpath', $DistDir,
    '--workpath', $WorkDir
)
if ($Clean) { $pyiArgs += '--clean' }

Write-Host "  $PythonExe $($pyiArgs -join ' ')" -ForegroundColor DarkGray
Write-Host ''
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Invoke-External -FilePath $PythonExe -ArgumentList $pyiArgs -ErrorMessage 'PyInstaller 打包失败' -MergeStderr
$sw.Stop()
Write-Ok ("打包完成，用时 {0:N1} 秒" -f $sw.Elapsed.TotalSeconds)

# --------------------------------------------------------------------------- #
# 步 4：产物
# --------------------------------------------------------------------------- #
Write-Step '步 4 / 6  产物'

if ($OneDir) {
    $exeConsole = Join-Path $DistDir 'Camab\Camab.exe'
    $exeTray = Join-Path $DistDir 'CamabTray\CamabTray.exe'
} else {
    $exeConsole = Join-Path $DistDir 'Camab.exe'
    $exeTray = Join-Path $DistDir 'CamabTray.exe'
}

foreach ($exe in @($exeConsole, $exeTray)) {
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "打包流程没报错，但产物不存在：$exe"
    }
}
& (Join-Path $PSScriptRoot 'set-release-integrity.ps1') -Path @($exeConsole, $exeTray)

$rows = @()
foreach ($exe in @($exeConsole, $exeTray)) {
    $item = Get-Item -LiteralPath $exe
    $rows += [pscustomobject]@{
        产物   = Split-Path -Leaf $exe
        路径   = $exe
        大小MB = [math]::Round($item.Length / 1MB, 1)
    }
}
$rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

if ($OneDir) {
    foreach ($d in @((Join-Path $DistDir 'Camab'), (Join-Path $DistDir 'CamabTray'))) {
        $mb = [math]::Round(((Get-ChildItem -LiteralPath $d -Recurse -File |
            Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
        Write-Host ("  目录 {0} 合计 {1} MB" -f (Split-Path -Leaf $d), $mb)
    }
}

# --------------------------------------------------------------------------- #
# 步 5：验证
# --------------------------------------------------------------------------- #
$verifyFailed = $false
$logDir = Join-Path $WorkDir 'verify'
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

if ($NoVerify) {
    Write-Step '步 5 / 6  验证（跳过，指定了 -NoVerify）'
} else {
    Write-Step '步 5 / 6  验证产物'

    # 5a. 冻结版 `-c` 冒烟：runtime hook 是否生效（camab 的摄像头探测依赖它）
    $shimLog = Join-Path $logDir 'frozen-c-shim.log'
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $shimOut = ((& $exeConsole -c "import sys; sys.stdout.write('PYI_C_SHIM_OK'); sys.stderr.write('pyi-c-stderr-ok\n')" 2>&1 |
            ForEach-Object { [string]$_ }) -join "`n")
        $shimCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    $shimOut | Set-Content -LiteralPath $shimLog -Encoding utf8
    if ($shimCode -eq 0 -and $shimOut -match 'PYI_C_SHIM_OK' -and $shimOut -match 'pyi-c-stderr-ok') {
        Write-Ok "冻结版 `-c` 可用（runtime hook 生效，退出码 $shimCode）"
    } else {
        Write-Warn2 "冻结版 `-c` 冒烟测试未通过：退出码 $shimCode，输出见 $shimLog"
        Write-Host $shimOut
        $verifyFailed = $true
    }

    # 5b. 必测项：dist\Camab.exe preflight --no-camera
    $pfLog = Join-Path $logDir 'preflight-no-camera.log'
    Write-Host ''
    Write-Host "  > `"$exeConsole`" preflight --no-camera" -ForegroundColor DarkGray
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $pfOut = ((& $exeConsole preflight --no-camera 2>&1 | ForEach-Object { [string]$_ }) -join "`n")
        $pfCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
    }
    $pfOut | Set-Content -LiteralPath $pfLog -Encoding utf8
    Write-Host $pfOut
    Write-Host "  （完整输出已存到 $pfLog）" -ForegroundColor DarkGray
    if ($pfCode -eq 0) {
        Write-Ok "preflight --no-camera 通过（退出码 0）"
    } else {
        Write-Warn2 "preflight --no-camera 退出码 $pfCode —— 打包本身成功，但自检没通过，请照上面的 ❌ 项处理"
        $verifyFailed = $true
    }
}

# --------------------------------------------------------------------------- #
# 步 6：收尾
# --------------------------------------------------------------------------- #
Write-Step '步 6 / 6  结束'

if ($verifyFailed) {
    Write-Host '  结论：打包完成，但验证有未通过项（详见上面输出与 build\verify\ 下的日志）。' -ForegroundColor Yellow
} else {
    Write-Host '  结论：打包 + 验证全部通过。' -ForegroundColor Green
}
Write-Host ''
Write-Host '  下一步：'
Write-Host "    · 双击启动（托盘）      : scripts\start.cmd"
Write-Host "    · 直接跑托盘版          : `"$exeTray`""
Write-Host "    · 命令行自检            : `"$exeConsole`" preflight"
Write-Host "    · 注册开机自启          : powershell -ExecutionPolicy Bypass -File packaging\install_autostart.ps1"
Write-Host ''

if ($verifyFailed) { exit 1 }
exit 0
