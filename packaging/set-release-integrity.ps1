#Requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string[]]$Path)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
foreach ($ReleasePath in $Path) {
    $ReleaseFile = Get-Item -LiteralPath $ReleasePath
    if ($ReleaseFile.PSIsContainer -or $ReleaseFile.Extension -ine '.exe') {
        throw "Expected an EXE release file: $ReleasePath"
    }
    # Fix only release files. A Low label inherited from the build workspace
    # otherwise lowers Explorer-launched programs and denies WMI/device writes.
    & "$env:WINDIR\System32\icacls.exe" $ReleaseFile.FullName /setintegritylevel M /q
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot set normal user integrity on $($ReleaseFile.FullName). Run the build from a normal PowerShell window."
    }
}
