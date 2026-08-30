#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$BootstrapScript,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$Branch
)

$ErrorActionPreference = 'Continue'

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$LogPath = Join-Path $InstallDir 'install.log'

"=== SWM installer bootstrap $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
    Set-Content -LiteralPath $LogPath -Encoding UTF8
"InstallDir: $InstallDir" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"Branch: $Branch" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"PowerShell: $($PSVersionTable.PSVersion)" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"User: $env:USERDOMAIN\$env:USERNAME" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"" | Add-Content -LiteralPath $LogPath -Encoding UTF8

# The bootstrap intentionally uses exit 0/1. Running it as a child
# PowerShell process lets this wrapper retain control so stdout/stderr can be
# written to a persistent file even when the bootstrap exits with an error.
$psExe = Join-Path $PSHOME 'powershell.exe'
$arguments = @(
    '-NoLogo',
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', $BootstrapScript,
    '-InstallDir', $InstallDir,
    '-Branch', $Branch
)

try {
    & $psExe @arguments 2>&1 | Tee-Object -FilePath $LogPath -Append
    $exitCode = $LASTEXITCODE
} catch {
    $_ | Out-String | Tee-Object -FilePath $LogPath -Append | Write-Host
    $exitCode = 1
}

"" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"Bootstrap exit code: $exitCode" | Add-Content -LiteralPath $LogPath -Encoding UTF8

exit $exitCode
