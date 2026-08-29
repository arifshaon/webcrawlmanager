#requires -Version 5.1
<#
.SYNOPSIS
    Build and Authenticode-sign the SWM Windows setup executable.

.DESCRIPTION
    Compiles install/SWM-Windows-Setup.iss with Inno Setup 6 and signs the
    resulting EXE with a code-signing certificate already present in the
    Windows certificate store.

    Release builds are signed by default. The script refuses to produce an
    unsigned release unless -AllowUnsigned is explicitly supplied.

    A proper Windows publisher signature requires a trusted code-signing
    certificate with a private key. Do not store private keys or PFX passwords
    in this repository.

.EXAMPLE
    .\install\build-windows-installer.ps1 `
      -CertificateThumbprint "0123456789ABCDEF0123456789ABCDEF01234567"

.EXAMPLE
    .\install\build-windows-installer.ps1 `
      -CertificateThumbprint "0123456789ABCDEF0123456789ABCDEF01234567" `
      -CertificateStore LocalMachine
#>

[CmdletBinding()]
param(
    [string]$Branch = "feature/record-session",
    [string]$CertificateThumbprint,
    [ValidateSet("CurrentUser", "LocalMachine")]
    [string]$CertificateStore = "CurrentUser",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [string]$OutputDir = (Join-Path $PSScriptRoot "dist"),
    [switch]$AllowUnsigned
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$IssPath = Join-Path $PSScriptRoot "SWM-Windows-Setup.iss"
$PyProjectPath = Join-Path $RepoRoot "pyproject.toml"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "[OK] $Text" -ForegroundColor Green
}

function Find-InnoCompiler {
    $candidates = @()
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    }
    foreach ($path in $candidates | Select-Object -Unique) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $roots = @()
    if (${env:ProgramFiles(x86)}) {
        $roots += (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin")
    }
    if ($env:ProgramFiles) {
        $roots += (Join-Path $env:ProgramFiles "Windows Kits\10\bin")
    }

    $matches = @()
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $matches += Get-ChildItem -Path $root -Filter signtool.exe -File -Recurse `
            -ErrorAction SilentlyContinue | Where-Object {
                $_.FullName -match "\\x64\\signtool\.exe$"
            }
    }
    if (-not $matches) { return $null }
    return ($matches | Sort-Object FullName -Descending | Select-Object -First 1).FullName
}

function Invoke-External {
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [Parameter(Mandatory=$true)][string]$Description
    )
    Write-Host "[INFO] $Description" -ForegroundColor Gray
    & $Exe @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Get-AppVersion {
    if (-not (Test-Path -LiteralPath $PyProjectPath)) {
        throw "pyproject.toml not found at $PyProjectPath"
    }
    $match = Select-String -Path $PyProjectPath `
        -Pattern '^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?)"\s*$' |
        Select-Object -First 1
    if (-not $match) {
        throw "Could not read [project] version from pyproject.toml."
    }
    return $match.Matches[0].Groups[1].Value
}

function Get-SigningCertificate([string]$Thumbprint, [string]$StoreScope) {
    if (-not $Thumbprint) { return $null }
    $clean = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    $path = "Cert:\$StoreScope\My\$clean"
    $cert = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    if (-not $cert) {
        throw "Code-signing certificate $clean was not found in $StoreScope\\My."
    }
    if (-not $cert.HasPrivateKey) {
        throw "Certificate $clean does not have an accessible private key."
    }
    if ($cert.NotAfter -le (Get-Date)) {
        throw "Certificate $clean expired on $($cert.NotAfter)."
    }

    $codeSigningOid = "1.3.6.1.5.5.7.3.3"
    $eku = @($cert.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId.Value })
    if ($eku -notcontains $codeSigningOid) {
        throw "Certificate $clean is not valid for Code Signing (EKU $codeSigningOid)."
    }
    return $cert
}

if (-not (Test-Path -LiteralPath $IssPath)) {
    throw "Inno Setup definition is missing: $IssPath"
}

$version = Get-AppVersion
$inno = Find-InnoCompiler
if (-not $inno) {
    throw "Inno Setup 6 (ISCC.exe) is required to build the installer. Install Inno Setup 6, then rerun this script."
}

$signTool = Find-SignTool
$certificate = Get-SigningCertificate -Thumbprint $CertificateThumbprint `
    -StoreScope $CertificateStore

if (-not $certificate -and -not $AllowUnsigned) {
    throw @"
A signed release was requested but no code-signing certificate was supplied.
Install your trusted code-signing certificate (with private key) in the Windows
certificate store and rerun with -CertificateThumbprint <thumbprint>.

Use -AllowUnsigned only for a local development build; do not distribute it.
"@
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path

Write-Step "Compile installer"
Invoke-External -Exe $inno -ArgumentList @(
    "/Qp",
    "/DAppVersion=$version",
    "/DSourceBranch=$Branch",
    "/O$OutputDir",
    $IssPath
) -Description "Compiling SWM $version with Inno Setup"

$exe = Join-Path $OutputDir "SWM-Setup-$version.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Inno Setup completed but $exe was not created."
}
Write-Ok "Built $exe"

if ($certificate) {
    if (-not $signTool) {
        throw "Windows SDK SignTool (signtool.exe) is required for Authenticode signing. Install the Windows SDK and rerun."
    }

    Write-Step "Authenticode sign installer"
    $thumbprint = $certificate.Thumbprint
    $signArgs = @(
        "sign",
        "/sha1", $thumbprint,
        "/s", "My",
        "/fd", "SHA256",
        "/tr", $TimestampUrl,
        "/td", "SHA256",
        "/d", "Simple Webcrawl Manager $version",
        "/du", "https://github.com/arifshaon/webcrawlmanager"
    )
    if ($CertificateStore -eq "LocalMachine") {
        $signArgs += "/sm"
    }
    $signArgs += $exe

    Invoke-External -Exe $signTool -ArgumentList $signArgs `
        -Description "Signing installer with $($certificate.Subject)"

    Write-Step "Verify signature"
    Invoke-External -Exe $signTool -ArgumentList @(
        "verify", "/pa", "/all", "/v", $exe
    ) -Description "Verifying Authenticode signature and trust chain"

    $signature = Get-AuthenticodeSignature -FilePath $exe
    if ($signature.Status -ne "Valid") {
        throw "PowerShell signature verification returned: $($signature.Status) - $($signature.StatusMessage)"
    }
    Write-Ok "Signature valid. Publisher certificate: $($signature.SignerCertificate.Subject)"
    Write-Ok "RFC3161 timestamp applied via $TimestampUrl"
} else {
    Write-Warning "UNSIGNED DEVELOPMENT BUILD: $exe"
}

Write-Step "Build complete"
Write-Host "Installer: $exe" -ForegroundColor Green
Write-Host "Version:   $version"
Write-Host "Source:    $Branch"
if ($certificate) {
    Write-Host "Signed:    Yes (SHA-256 Authenticode + RFC3161 timestamp)" -ForegroundColor Green
} else {
    Write-Host "Signed:    NO - development build only" -ForegroundColor Yellow
}
