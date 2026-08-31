#requires -Version 5.1
<#
.SYNOPSIS
    Windows bootstrap installer for Simple Webcrawl Manager (SWM).

.DESCRIPTION
    Installs SWM as a self-contained application beneath the user-selected
    installation directory.

    The installer does not install or depend on a system-wide Python. A pinned
    CPython 3.13 runtime is installed at:

        <InstallDir>\.runtime\python\python.exe

    SWM's Python packages are installed directly into that private interpreter,
    and Playwright browsers are kept under <InstallDir>\.runtime as well.

    The generated launchers explicitly call the Python executable inside the
    SWM installation, so another Python installation on the computer is never
    selected accidentally.
#>

[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Simple Webcrawl Manager"),
    [string]$Branch = "feature/record-session",
    [int]$DashboardPort = 8080,
    [int]$ReplayPort = 8091,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoOwner = "arifshaon"
$RepoName = "webcrawlmanager"
$RepoBaseUrl = "https://github.com/$RepoOwner/$RepoName"

$RuntimeRoot = Join-Path $InstallDir ".runtime"
$PythonDir = Join-Path $RuntimeRoot "python"
$PythonExe = Join-Path $PythonDir "python.exe"
$UvDir = Join-Path $RuntimeRoot "uv"
$UvExe = Join-Path $UvDir "uv.exe"
$UvCacheDir = Join-Path $RuntimeRoot "uv-cache"
$PlaywrightDir = Join-Path $RuntimeRoot "ms-playwright"

$UvVersion = "0.11.29"
$UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvSha256 = "a047d55651bc3e0ca24595b25ec4cfcb10f9dca9fb56514e661269b37d4fae68"

$PythonVersion = "3.13.14"
$PythonBuildRelease = "20260804"
$PythonArchiveUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/20260804/cpython-3.13.14%2B20260804-x86_64-pc-windows-msvc-install_only.tar.gz"
$PythonArchiveSha256 = "84012b1c9d4bff00e2989e47c41c8ee74f43d4cee061df45d2f6c8459627cb28"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {}

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "[OK] $Text" -ForegroundColor Green
}

function Write-Info([string]$Text) {
    Write-Host "[INFO] $Text" -ForegroundColor Gray
}

function Invoke-External {
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [Parameter(Mandatory=$true)][string]$Description
    )

    Write-Info $Description
    $output = & $Exe @ArgumentList 2>&1
    $exitCode = $LASTEXITCODE
    foreach ($line in @($output)) {
        Write-Host $line
    }
    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode."
    }
}

function Download-SourceZip([string]$TargetDir, [string]$BranchName) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("swm-source-" + [Guid]::NewGuid().ToString("N"))
    $zip = Join-Path $tmp "source.zip"
    $expanded = Join-Path $tmp "expanded"
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null

    $escapedBranch = (($BranchName -split "/") | ForEach-Object {
        [Uri]::EscapeDataString($_)
    }) -join "/"
    $url = "$RepoBaseUrl/archive/refs/heads/$escapedBranch.zip"

    try {
        Write-Info "Downloading SWM branch $BranchName from GitHub."
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force

        $sourceRoot = Get-ChildItem -LiteralPath $expanded -Directory | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName "pyproject.toml")
        } | Select-Object -First 1
        if (-not $sourceRoot) {
            throw "The downloaded GitHub archive is not a valid SWM source tree."
        }

        New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

        $config = Join-Path $TargetDir "config.yaml"
        if (Test-Path -LiteralPath $config) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            Copy-Item -LiteralPath $config -Destination "$config.$stamp.bak" -Force
            Write-Info "Existing config.yaml backed up before source refresh."
        }

        foreach ($item in Get-ChildItem -LiteralPath $sourceRoot.FullName -Force) {
            # Runtime files are generated locally and are never supplied by the
            # source archive, but explicitly protect them if that ever changes.
            if ($item.Name -in @('.runtime', 'install.log', 'server-port.txt')) {
                continue
            }
            Copy-Item -LiteralPath $item.FullName -Destination $TargetDir -Recurse -Force
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-PortableUv {
    if (Test-Path -LiteralPath $UvExe) {
        try {
            $versionText = @(& $UvExe --version 2>$null)
            if ($LASTEXITCODE -eq 0 -and ($versionText -join " ") -match [regex]::Escape($UvVersion)) {
                Write-Ok "Portable uv $UvVersion is already present at $UvExe."
                return
            }
        } catch {}

        Write-Info "Replacing an old or unusable local uv runtime."
        Remove-Item -LiteralPath $UvDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("swm-uv-" + [Guid]::NewGuid().ToString("N"))
    $zip = Join-Path $tmp "uv.zip"
    $expanded = Join-Path $tmp "expanded"
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null

    try {
        Write-Info "Downloading portable uv $UvVersion."
        Invoke-WebRequest -Uri $UvUrl -OutFile $zip -UseBasicParsing
        $actualHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $UvSha256) {
            throw "uv SHA-256 verification failed. Expected $UvSha256 but received $actualHash."
        }
        Write-Ok "uv download SHA-256 verified."

        Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force
        $downloadedUv = Get-ChildItem -LiteralPath $expanded -Filter "uv.exe" -File -Recurse | Select-Object -First 1
        if (-not $downloadedUv) {
            throw "The verified uv archive did not contain uv.exe."
        }

        New-Item -ItemType Directory -Path $UvDir -Force | Out-Null
        Copy-Item -LiteralPath $downloadedUv.FullName -Destination $UvExe -Force
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Invoke-External -Exe $UvExe -ArgumentList @("--version") -Description "Verifying local uv"
    Write-Ok "Portable uv is installed inside SWM: $UvExe"
}

function Get-LocalPythonVersion([string]$ExePath) {
    if (-not (Test-Path -LiteralPath $ExePath)) {
        return $null
    }

    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $ExePath
        $startInfo.Arguments = '-c "import sys; print(sys.version_info.major, sys.version_info.minor, sys.version_info.micro, sep=chr(46))"'
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEnd().Trim()
        $stderr = $process.StandardError.ReadToEnd().Trim()
        $process.WaitForExit()
        $exitCode = $process.ExitCode
        $process.Dispose()

        if ($exitCode -eq 0 -and $stdout -match '^3\.13\.') {
            return $stdout
        }

        if ($stderr) {
            Write-Info "Local Python probe failed: exit=$exitCode output='$stdout' error='$stderr'"
        }
    } catch {
        Write-Info "Local Python probe failed: $($_.Exception.Message)"
    }
    return $null
}

function Get-TarExe {
    if ($env:SystemRoot) {
        $systemTar = Join-Path $env:SystemRoot "System32\tar.exe"
        if (Test-Path -LiteralPath $systemTar) {
            return $systemTar
        }
    }

    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if ($tar) {
        return $tar.Source
    }
    return $null
}

function Install-LocalPython {
    $existingVersion = Get-LocalPythonVersion -ExePath $PythonExe
    if ($existingVersion) {
        Write-Ok "Local CPython $existingVersion already exists inside SWM: $PythonExe"
        return
    }

    if (Test-Path -LiteralPath $PythonDir) {
        Write-Info "Replacing incomplete or unusable local Python runtime."
        Remove-Item -LiteralPath $PythonDir -Recurse -Force
    }

    $tarExe = Get-TarExe
    if (-not $tarExe) {
        throw "Windows tar.exe is required to unpack the local CPython runtime but was not found."
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("swm-python-" + [Guid]::NewGuid().ToString("N"))
    $archive = Join-Path $tmp "python.tar.gz"
    $expanded = Join-Path $tmp "expanded"
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null

    try {
        Write-Info "Downloading CPython $PythonVersion for the SWM installation."
        Invoke-WebRequest -Uri $PythonArchiveUrl -OutFile $archive -UseBasicParsing

        $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $PythonArchiveSha256) {
            throw "CPython SHA-256 verification failed. Expected $PythonArchiveSha256 but received $actualHash."
        }
        Write-Ok "CPython download SHA-256 verified."

        Invoke-External -Exe $tarExe -ArgumentList @("-xzf", $archive, "-C", $expanded) -Description "Extracting local CPython runtime"

        $archivePythonDir = Join-Path $expanded "python"
        $archivePythonExe = Join-Path $archivePythonDir "python.exe"
        if (-not (Test-Path -LiteralPath $archivePythonExe)) {
            throw "The verified CPython archive did not contain python\python.exe."
        }

        New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
        Move-Item -LiteralPath $archivePythonDir -Destination $PythonDir
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    $installedVersion = $null
    for ($attempt = 1; $attempt -le 5 -and -not $installedVersion; $attempt++) {
        $installedVersion = Get-LocalPythonVersion -ExePath $PythonExe
        if (-not $installedVersion -and $attempt -lt 5) {
            Start-Sleep -Milliseconds (250 * $attempt)
        }
    }
    if (-not $installedVersion) {
        throw "CPython was extracted but could not be started at $PythonExe."
    }

    Write-Ok "Local CPython $installedVersion is installed inside SWM: $PythonExe"
}

function Install-SwmIntoLocalPython([string]$TargetDir) {
    New-Item -ItemType Directory -Path $UvCacheDir -Force | Out-Null
    New-Item -ItemType Directory -Path $PlaywrightDir -Force | Out-Null

    $env:UV_CACHE_DIR = $UvCacheDir
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightDir

    # --system means "install into the interpreter supplied by --python".
    # That interpreter is SWM's private <InstallDir>\.runtime\python\python.exe;
    # no Windows/system Python is touched.
    Invoke-External -Exe $UvExe -ArgumentList @(
        "pip", "install",
        "--python", $PythonExe,
        "--system",
        "--reinstall",
        "-e", "$TargetDir[dashboard]"
    ) -Description "Installing SWM packages into the local SWM Python"

    Invoke-External -Exe $PythonExe -ArgumentList @(
        "-m", "playwright", "install", "chromium"
    ) -Description "Installing Playwright Chromium inside the SWM installation"
}

function Test-PortAvailable([int]$Port) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            try { $listener.Stop() } catch {}
        }
    }
}

function Find-FreePort([int]$StartPort) {
    for ($port = $StartPort; $port -lt ($StartPort + 100); $port++) {
        if (Test-PortAvailable $port) { return $port }
    }
    return $null
}

function Resolve-Port([string]$Name, [int]$PreferredPort) {
    if (Test-PortAvailable $PreferredPort) {
        Write-Ok "$Name port $PreferredPort is free on 127.0.0.1."
        return $PreferredPort
    }

    $next = Find-FreePort ($PreferredPort + 1)
    if (-not $next) {
        throw "No free $Name port was found between $($PreferredPort + 1) and $($PreferredPort + 99)."
    }
    Write-Info "$Name port $PreferredPort is busy; using $next instead."
    return $next
}

function Write-Launchers([string]$TargetDir, [int]$ServerPort) {
    $cliLauncher = Join-Path $TargetDir "swm.cmd"
    @'
@echo off
setlocal
cd /d "%~dp0"
set "SWM_PYTHON=%~dp0.runtime\python\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.runtime\ms-playwright"
if not exist "%SWM_PYTHON%" (
  echo SWM local Python was not found: "%SWM_PYTHON%"
  exit /b 1
)
"%SWM_PYTHON%" -m webarc.cli %*
exit /b %ERRORLEVEL%
'@ | Set-Content -LiteralPath $cliLauncher -Encoding ASCII

    $serverLauncher = Join-Path $TargetDir "Start SWM Server.cmd"
    $serverText = @"
@echo off
setlocal
cd /d "%~dp0"
set "SWM_PYTHON=%~dp0.runtime\python\python.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.runtime\ms-playwright"
set "SWM_PORT=$ServerPort"
if not exist "%SWM_PYTHON%" (
  echo SWM local Python was not found: "%SWM_PYTHON%"
  pause
  exit /b 1
)
echo Starting Simple Webcrawl Manager on http://127.0.0.1:%SWM_PORT%
start "SWM Server" /D "%~dp0" "%SWM_PYTHON%" -m webarc.cli serve --host 127.0.0.1 --port %SWM_PORT%
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:%SWM_PORT%"
endlocal
"@
    $serverText | Set-Content -LiteralPath $serverLauncher -Encoding ASCII

    Set-Content -LiteralPath (Join-Path $TargetDir "server-port.txt") -Value $ServerPort -Encoding ASCII
    Write-Ok "Created local-runtime server launcher: $serverLauncher"
}

try {
    Write-Host "Simple Webcrawl Manager (SWM) - Windows Installer" -ForegroundColor White
    Write-Host "Branch: $Branch"
    Write-Host "Install directory: $InstallDir"
    Write-Host "Local Python: $PythonExe"

    Write-Step "1. Download / update SWM"
    Download-SourceZip -TargetDir $InstallDir -BranchName $Branch
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "pyproject.toml"))) {
        throw "pyproject.toml is missing after source download."
    }
    Write-Ok "SWM source is ready at $InstallDir."

    Write-Step "2. Install local runtime"
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    Install-PortableUv
    Install-LocalPython
    Install-SwmIntoLocalPython -TargetDir $InstallDir

    Write-Step "3. Verify local SWM runtime"
    $version = Get-LocalPythonVersion -ExePath $PythonExe
    if (-not $version) {
        throw "Local SWM Python verification failed."
    }

    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightDir
    Invoke-External -Exe $PythonExe -ArgumentList @("-m", "webarc.cli", "--help") -Description "Running SWM CLI smoke test with local Python"
    Write-Ok "SWM is running from local Python $version at $PythonExe."

    Write-Step "4. Check local ports"
    $actualDashboardPort = Resolve-Port -Name "Dashboard" -PreferredPort $DashboardPort
    $actualReplayPort = Resolve-Port -Name "Replay" -PreferredPort $ReplayPort

    Write-Step "5. Create launchers"
    Write-Launchers -TargetDir $InstallDir -ServerPort $actualDashboardPort

    Write-Step "Installation complete"
    Write-Host "Installed to: $InstallDir" -ForegroundColor Green
    Write-Host "Local Python: $PythonExe" -ForegroundColor Green
    Write-Host "Playwright: $PlaywrightDir"
    Write-Host ""
    Write-Host "Start SWM by double-clicking:"
    Write-Host "  $InstallDir\Start SWM Server.cmd" -ForegroundColor Green
    Write-Host ""
    Write-Host "Dashboard: http://127.0.0.1:$actualDashboardPort"
    Write-Host "Replay default port: $actualReplayPort"
    exit 0
} catch {
    Write-Host ""
    Write-Host "INSTALLATION FAILED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Expected local Python location: $PythonExe"
    Write-Host "Nothing under a separate system or LocalAppData runtime is required."
    exit 1
}
