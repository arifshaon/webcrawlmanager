#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$BootstrapScript,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$Branch,
    [Parameter(Mandatory=$true)][string]$SourceZip
)

$ErrorActionPreference = 'Continue'

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$LogPath = Join-Path $InstallDir 'install.log'

"=== SWM installer bootstrap $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
    Set-Content -LiteralPath $LogPath -Encoding UTF8
"InstallDir: $InstallDir" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"Branch: $Branch" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"Bundled source: $SourceZip" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"PowerShell: $($PSVersionTable.PSVersion)" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"User: $env:USERDOMAIN\$env:USERNAME" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"RuntimeRoot: $(Join-Path $InstallDir '.runtime')" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"" | Add-Content -LiteralPath $LogPath -Encoding UTF8

# The bootstrap intentionally uses exit 0/1. Running it as a child PowerShell
# process lets this wrapper retain control so stdout/stderr can be persisted to
# install.log even when the bootstrap exits with an error.
$psExe = Join-Path $PSHOME 'powershell.exe'
$arguments = @(
    '-NoLogo',
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', $BootstrapScript,
    '-InstallDir', $InstallDir,
    '-Branch', $Branch,
    '-SourceZip', $SourceZip
)

try {
    # Do not use Tee-Object here. Windows PowerShell 5.1 appends Tee output as
    # UTF-16LE, which corrupts a log initialized as UTF-8.
    & $psExe @arguments 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-Host $line
        $line | Add-Content -LiteralPath $LogPath -Encoding UTF8
    }
    $exitCode = $LASTEXITCODE
} catch {
    $failure = $_ | Out-String
    Write-Host $failure
    $failure | Add-Content -LiteralPath $LogPath -Encoding UTF8
    $exitCode = 1
}

"" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"Bootstrap exit code: $exitCode" | Add-Content -LiteralPath $LogPath -Encoding UTF8

exit $exitCode
