#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $Compiler)) {
    $Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $Compiler)) {
    throw '.NET Framework 4 compiler was not found.'
}
$Source = Join-Path $PSScriptRoot 'launcher.cs'
$Output = Join-Path $ProjectRoot 'start-camab.exe'
$CompilerTemp = Join-Path $ProjectRoot 'tmp\launcher-compiler-runtime'
New-Item -ItemType Directory -Path $CompilerTemp -Force | Out-Null
$PreviousTemp = $env:TEMP
$PreviousTmp = $env:TMP
try {
    $env:TEMP = $CompilerTemp
    $env:TMP = $CompilerTemp
    & $Compiler /nologo /target:winexe /optimize+ /platform:anycpu /reference:System.Windows.Forms.dll "/out:$Output" $Source
    if ($LASTEXITCODE -ne 0) { throw "Launcher build failed: $LASTEXITCODE" }
} finally {
    $env:TEMP = $PreviousTemp
    $env:TMP = $PreviousTmp
}
& (Join-Path $PSScriptRoot 'set-release-integrity.ps1') -Path @($Output)
Write-Host "Built: $Output"
