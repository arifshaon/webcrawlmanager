#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$BootstrapScript,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$Branch
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$LogPath = Join-Path $InstallDir 'install.log'

"=== SWM installer bootstrap $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
    Set-Content -LiteralPath $LogPath -Encoding UTF8
"InstallDir: $InstallDir" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"Branch: $Branch" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"PowerShell: $($PSVersionTable.PSVersion)" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"User: $env:USERDOMAIN\$env:USERNAME" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"" | Add-Content -LiteralPath $LogPath -Encoding UTF8

$PrivateRuntimeRoot = Join-Path $env:LOCALAPPDATA 'SimpleWebcrawlManager\runtime'
$PrivatePythonDir = Join-Path $PrivateRuntimeRoot 'python'
$PrivatePythonExe = Join-Path $PrivatePythonDir 'python.exe'
$PythonEmbedUrl = 'https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip'
$PythonEmbedSha256 = '90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907'
$VcRuntimeDlls = @('vcruntime140.dll', 'vcruntime140_1.dll')

function Write-InstallerLine([string]$Text) {
    Write-Host $Text
    $Text | Add-Content -LiteralPath $LogPath -Encoding UTF8
}

function Add-PrivatePythonToPath {
    if (-not (Test-Path -LiteralPath $PrivatePythonDir)) {
        return
    }

    $parts = @($env:PATH -split ';')
    if ($parts -notcontains $PrivatePythonDir) {
        $env:PATH = "$PrivatePythonDir;$env:PATH"
    }
}

function Test-PrivatePython {
    if (-not (Test-Path -LiteralPath $PrivatePythonExe)) {
        return $false
    }

    try {
        $version = (& $PrivatePythonExe -c "import sys; print(sys.version.split()[0])" 2>&1 | Select-Object -First 1)
        return ($LASTEXITCODE -eq 0 -and $version -match '^3\.13\.')
    } catch {
        return $false
    }
}

function Repair-PrivatePythonRuntime {
    if (-not (Test-Path -LiteralPath $PrivatePythonExe)) {
        return $false
    }

    if (Test-PrivatePython) {
        Add-PrivatePythonToPath
        return $false
    }

    Write-InstallerLine '[INFO] Extracted private Python exists but could not start on this Windows installation.'
    Write-InstallerLine '[INFO] Adding app-local Microsoft Visual C++ runtime DLLs from the official Python.org 3.13.14 embeddable package.'

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("swm-vcruntime-" + [Guid]::NewGuid().ToString('N'))
    $zip = Join-Path $tmp 'python-embed.zip'
    $expanded = Join-Path $tmp 'expanded'
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null

    try {
        Invoke-WebRequest -Uri $PythonEmbedUrl -OutFile $zip -UseBasicParsing
        $actualHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $PythonEmbedSha256) {
            Write-InstallerLine "[ERROR] Python.org runtime package SHA-256 verification failed. Expected $PythonEmbedSha256 but received $actualHash."
            return $false
        }
        Write-InstallerLine '[OK] Python.org runtime package SHA-256 verified.'

        Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force

        foreach ($dll in $VcRuntimeDlls) {
            $source = Join-Path $expanded $dll
            $target = Join-Path $PrivatePythonDir $dll
            if (-not (Test-Path -LiteralPath $source)) {
                Write-InstallerLine "[ERROR] The verified Python.org package did not contain $dll."
                return $false
            }
            Copy-Item -LiteralPath $source -Destination $target -Force
        }

        Add-PrivatePythonToPath

        if (Test-PrivatePython) {
            Write-InstallerLine '[OK] Private Python now starts with the app-local Visual C++ runtime.'
            return $true
        }

        try {
            $probe = (& $PrivatePythonExe --version 2>&1 | Out-String).Trim()
            $probeExit = $LASTEXITCODE
            if ($probe) {
                Write-InstallerLine "[ERROR] Private Python still failed after runtime repair (exit $probeExit): $probe"
            } else {
                Write-InstallerLine "[ERROR] Private Python still failed after runtime repair (exit $probeExit)."
            }
        } catch {
            Write-InstallerLine "[ERROR] Private Python still could not be launched after runtime repair: $($_.Exception.Message)"
        }
        return $false
    } catch {
        Write-InstallerLine "[ERROR] Could not stage the app-local Visual C++ runtime: $($_.Exception.Message)"
        return $false
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Stage-VcRuntimeForInstalledVenv {
    $venvScripts = Join-Path $InstallDir '.venv\Scripts'
    if (-not (Test-Path -LiteralPath $venvScripts)) {
        return
    }

    foreach ($dll in $VcRuntimeDlls) {
        $source = Join-Path $PrivatePythonDir $dll
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $venvScripts $dll) -Force
        }
    }
}

# On some Windows 11 systems (notably systems with OneDrive Files On-Demand /
# RedirectionGuard), uv can fail with os error 448 while creating its optional
# python3.13 minor-version launcher directory. SWM never uses those launchers:
# it keeps Python private and creates its own .venv. Disable the bin-link step
# entirely. uv documents this as the environment equivalent of
# `uv python install --no-bin`.
$env:UV_PYTHON_INSTALL_BIN = '0'

# If a previous installer attempt already staged the app-local runtime DLLs,
# make them visible to child processes immediately. This also lets the venv
# launchers resolve the same runtime while the bootstrap is running.
$hasLocalVcRuntime = $true
foreach ($dll in $VcRuntimeDlls) {
    if (-not (Test-Path -LiteralPath (Join-Path $PrivatePythonDir $dll))) {
        $hasLocalVcRuntime = $false
        break
    }
}
if ($hasLocalVcRuntime) {
    Add-PrivatePythonToPath
}

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

function Invoke-BootstrapChild {
    try {
        # Do not use Tee-Object here. Windows PowerShell 5.1 appends Tee output
        # as UTF-16LE, which corrupts a log initialized as UTF-8. Convert each
        # pipeline item to text and append explicitly as UTF-8 instead.
        & $psExe @arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-Host $line
            $line | Add-Content -LiteralPath $LogPath -Encoding UTF8
        }
        return [int]$LASTEXITCODE
    } catch {
        $failure = $_ | Out-String
        Write-Host $failure
        $failure | Add-Content -LiteralPath $LogPath -Encoding UTF8
        return 1
    }
}

$exitCode = Invoke-BootstrapChild

# python-build-standalone deliberately does not ship the Microsoft Visual C++
# runtime used by its Windows interpreter. GitHub's Windows runners already
# have that runtime, but a clean end-user machine may not. If the first run
# leaves an extracted Python that cannot start, repair it app-locally from the
# official Python.org embeddable package and retry automatically. No admin or
# system-wide Python/VC runtime installation is required.
if ($exitCode -ne 0) {
    $repaired = Repair-PrivatePythonRuntime
    if ($repaired) {
        Write-InstallerLine ''
        Write-InstallerLine '[INFO] Retrying SWM bootstrap with the repaired private Python runtime.'
        $exitCode = Invoke-BootstrapChild
    }
}

if ($exitCode -eq 0) {
    # Keep the installed venv runnable after this wrapper exits even on systems
    # without a system-wide Visual C++ Redistributable.
    Stage-VcRuntimeForInstalledVenv
}

"" | Add-Content -LiteralPath $LogPath -Encoding UTF8
"Bootstrap exit code: $exitCode" | Add-Content -LiteralPath $LogPath -Encoding UTF8

exit $exitCode
