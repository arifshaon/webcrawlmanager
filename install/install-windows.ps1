#requires -Version 5.1
<#
.SYNOPSIS
    Windows bootstrap installer for Simple Webcrawl Manager (SWM).

.DESCRIPTION
    Installs the feature/record-session build of SWM. The application source,
    launchers, virtual environment, and Playwright browser files are kept in the
    user-selected installation directory.

    SWM does NOT install Python system-wide. A private CPython 3.13 runtime is
    installed under the current user's LocalAppData directory. This avoids
    administrator rights, PATH changes, Python registry registration, and the
    Windows "untrusted mount point" failure that can occur when uv creates
    Python launcher links in some user-selected folders.

    The installer creates a double-clickable "Start SWM Server.cmd" launcher
    which starts the dashboard and opens it in the user's default browser.
#>

[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Simple Webcrawl Manager"),
    [string]$Branch = "feature/record-session",
    [int]$DashboardPort = 8080,
    [int]$ReplayPort = 8091,
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA "SimpleWebcrawlManager\runtime"),
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoOwner = "arifshaon"
$RepoName = "webcrawlmanager"
$RepoUrl = "https://github.com/$RepoOwner/$RepoName.git"
$RepoBaseUrl = "https://github.com/$RepoOwner/$RepoName"

$UvVersion = "0.11.29"
$UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$UvSha256 = "a047d55651bc3e0ca24595b25ec4cfcb10f9dca9fb56514e661269b37d4fae68"
$ManagedPython = "3.13"

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

function Confirm-Action([string]$Prompt) {
    if ($Yes) {
        Write-Info "$Prompt -> yes (-Yes)"
        return $true
    }
    while ($true) {
        $answer = (Read-Host "$Prompt [Y/N]").Trim().ToLowerInvariant()
        if ($answer -in @("y", "yes")) { return $true }
        if ($answer -in @("n", "no")) { return $false }
    }
}

function Test-IsAdmin {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [Parameter(Mandatory=$true)][string]$Description
    )

    Write-Info $Description
    $commandOutput = & $Exe @ArgumentList
    $exitCode = $LASTEXITCODE
    foreach ($line in @($commandOutput)) {
        Write-Host $line
    }
    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode."
    }
}

function Test-DirectoryEmpty([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    return @((Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue)).Count -eq 0
}

function Download-SourceZip([string]$TargetDir, [string]$BranchName) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("swm-source-" + [Guid]::NewGuid().ToString("N"))
    $zip = Join-Path $tmp "source.zip"
    $expanded = Join-Path $tmp "expanded"
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null

    $escapedBranch = (($BranchName -split "/") | ForEach-Object {
        [Uri]::EscapeDataString($_)
    }) -join "/"
    $url = "$RepoBaseUrl/archive/refs/heads/$escapedBranch.zip"

    try {
        Write-Info "Downloading branch $BranchName from GitHub (ZIP fallback)."
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force
        $root = Get-ChildItem -LiteralPath $expanded -Directory | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName "pyproject.toml")
        } | Select-Object -First 1
        if (-not $root) {
            throw "The downloaded GitHub archive is not a valid SWM source tree."
        }

        New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
        $config = Join-Path $TargetDir "config.yaml"
        if (Test-Path -LiteralPath $config) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            Copy-Item -LiteralPath $config -Destination "$config.$stamp.bak" -Force
            Write-Info "Existing config.yaml backed up before ZIP refresh."
        }

        foreach ($item in Get-ChildItem -LiteralPath $root.FullName -Force) {
            Copy-Item -LiteralPath $item.FullName -Destination $TargetDir -Recurse -Force
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Sync-Source([string]$TargetDir, [string]$BranchName) {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    $gitDir = Join-Path $TargetDir ".git"

    if ($git -and (Test-Path -LiteralPath $gitDir)) {
        Write-Info "Existing Git checkout detected."
        $dirty = & $git.Source -C $TargetDir status --porcelain --untracked-files=no
        if ($LASTEXITCODE -ne 0) {
            throw "Could not inspect the existing Git checkout."
        }
        if ($dirty) {
            throw "Tracked files in $TargetDir have local changes. Commit or stash them before updating."
        }
        Invoke-External -Exe $git.Source -ArgumentList @("-C", $TargetDir, "fetch", "origin", $BranchName) -Description "Fetching $BranchName from GitHub"
        Invoke-External -Exe $git.Source -ArgumentList @("-C", $TargetDir, "checkout", $BranchName) -Description "Checking out $BranchName"
        Invoke-External -Exe $git.Source -ArgumentList @("-C", $TargetDir, "pull", "--ff-only", "origin", $BranchName) -Description "Updating SWM from GitHub"
        return
    }

    if ($git -and (Test-DirectoryEmpty $TargetDir)) {
        $parent = Split-Path -Parent $TargetDir
        if ($parent) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Invoke-External -Exe $git.Source -ArgumentList @(
            "clone", "--branch", $BranchName, "--single-branch", $RepoUrl, $TargetDir
        ) -Description "Cloning SWM $BranchName from GitHub"
        return
    }

    Download-SourceZip -TargetDir $TargetDir -BranchName $BranchName
}

function Install-PortableUv([string]$TargetDir) {
    $runtimeDir = Join-Path $TargetDir ".runtime"
    $uvDir = Join-Path $runtimeDir "uv"
    $uvExe = Join-Path $uvDir "uv.exe"

    if (Test-Path -LiteralPath $uvExe) {
        try {
            $versionText = (& $uvExe --version 2>$null | Select-Object -First 1)
            if ($LASTEXITCODE -eq 0 -and $versionText -match [regex]::Escape($UvVersion)) {
                Write-Ok "Portable uv $UvVersion is already present."
                return $uvExe
            }
        } catch {}
        Write-Info "Replacing an old/unusable portable uv runtime."
        Remove-Item -LiteralPath $uvDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not (Confirm-Action "Download SWM's private Python runtime tools (no administrator rights or system Python installation)?")) {
        throw "A private Python runtime is required to run SWM. Installation cancelled by user."
    }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("swm-uv-" + [Guid]::NewGuid().ToString("N"))
    $zip = Join-Path $tmp "uv.zip"
    $expanded = Join-Path $tmp "expanded"
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null

    try {
        Write-Info "Downloading portable uv $UvVersion from the official Astral GitHub release."
        Invoke-WebRequest -Uri $UvUrl -OutFile $zip -UseBasicParsing
        $actualHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $UvSha256) {
            throw "uv download SHA-256 verification failed. Expected $UvSha256 but received $actualHash."
        }
        Write-Ok "uv download SHA-256 verified."

        Expand-Archive -LiteralPath $zip -DestinationPath $expanded -Force
        $downloadedUv = Get-ChildItem -LiteralPath $expanded -Filter "uv.exe" -File -Recurse | Select-Object -First 1
        if (-not $downloadedUv) {
            throw "The verified uv archive did not contain uv.exe."
        }

        New-Item -ItemType Directory -Path $uvDir -Force | Out-Null
        Copy-Item -LiteralPath $downloadedUv.FullName -Destination $uvExe -Force
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    & $uvExe --version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Portable uv could not run on this computer."
    }
    Write-Ok "Portable uv $UvVersion is ready at $uvExe."
    return $uvExe
}

function Install-PrivatePythonEnvironment([string]$TargetDir, [string]$UvExe, [string]$PrivateRuntimeRoot) {
    $pythonDir = Join-Path $PrivateRuntimeRoot "python"
    $uvDataDir = Join-Path $PrivateRuntimeRoot "uv-data"
    $uvCacheDir = Join-Path $PrivateRuntimeRoot "uv-cache"
    $venvDir = Join-Path $TargetDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    New-Item -ItemType Directory -Path $PrivateRuntimeRoot -Force | Out-Null

    # Keep uv's managed Python and state in LocalAppData. Explicitly disable
    # launcher/bin creation and registry integration. This avoids Windows
    # STATUS_UNTRUSTED_MOUNT_POINT / os error 448 on affected systems.
    $env:UV_DATA_DIR = $uvDataDir
    $env:UV_CACHE_DIR = $uvCacheDir
    $env:UV_PYTHON_INSTALL_DIR = $pythonDir
    $env:UV_PYTHON_NO_REGISTRY = "1"
    $env:UV_PYTHON_INSTALL_BIN = "0"

    Write-Info "Python will be private to SWM: $pythonDir"
    Invoke-External -Exe $UvExe -ArgumentList @(
        "python", "install", $ManagedPython,
        "--install-dir", $pythonDir,
        "--managed-python",
        "--no-bin",
        "--no-registry"
    ) -Description "Downloading private CPython $ManagedPython runtime"

    $privatePythonExe = Get-ChildItem -LiteralPath $pythonDir -Filter "python.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $privatePythonExe) {
        throw "Private CPython was installed but python.exe could not be found under $pythonDir."
    }
    Write-Ok "Private CPython executable: $($privatePythonExe.FullName)"

    if (Test-Path -LiteralPath $venvDir) {
        Write-Info "Recreating SWM virtual environment from the private runtime."
        Remove-Item -LiteralPath $venvDir -Recurse -Force
    }

    Invoke-External -Exe $UvExe -ArgumentList @(
        "venv", $venvDir,
        "--python", $privatePythonExe.FullName,
        "--seed"
    ) -Description "Creating isolated SWM virtual environment"

    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "Private SWM Python environment was not created at $venvPython."
    }

    Invoke-External -Exe $UvExe -ArgumentList @(
        "pip", "install",
        "--python", $venvPython,
        "-e", "$TargetDir[dashboard]"
    ) -Description "Installing SWM and dashboard dependencies"

    $playwrightDir = Join-Path $TargetDir ".runtime\ms-playwright"
    $env:PLAYWRIGHT_BROWSERS_PATH = $playwrightDir
    Invoke-External -Exe $venvPython -ArgumentList @(
        "-m", "playwright", "install", "chromium"
    ) -Description "Installing Playwright Chromium into SWM's private runtime"

    return $venvPython
}

function Get-ChromePath {
    $cmd = Get-Command chrome.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $paths = @()
    if ($env:ProgramFiles) {
        $paths += (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe")
    }
    if (${env:ProgramFiles(x86)}) {
        $paths += (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe")
    }
    if ($env:LOCALAPPDATA) {
        $paths += (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    }
    foreach ($path in $paths) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
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

function Get-PortOwner([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop | Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) { return "$($proc.ProcessName) (PID $($proc.Id))" }
            return "PID $($conn.OwningProcess)"
        }
    } catch {}
    return "another process"
}

function Find-FreePort([int]$StartPort) {
    for ($port = $StartPort; $port -lt ($StartPort + 100); $port++) {
        if (Test-PortAvailable $port) { return $port }
    }
    return $null
}

function Resolve-Port([string]$Name, [int]$PreferredPort) {
    if (Test-PortAvailable $PreferredPort) {
        Write-Ok "$Name port $PreferredPort is free for binding on 127.0.0.1."
        return $PreferredPort
    }

    Write-Warning "$Name port $PreferredPort is already in use by $(Get-PortOwner $PreferredPort)."
    $next = Find-FreePort ($PreferredPort + 1)
    if (-not $next) {
        throw "No free $Name port was found between $($PreferredPort + 1) and $($PreferredPort + 99)."
    }
    Write-Info "$Name will use free port $next instead."
    return $next
}

function Write-Launchers([string]$TargetDir, [int]$ServerPort) {
    $cliLauncher = Join-Path $TargetDir "swm.cmd"
    @'
@echo off
setlocal
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.runtime\ms-playwright"
"%~dp0.venv\Scripts\swm.exe" %*
endlocal
'@ | Set-Content -LiteralPath $cliLauncher -Encoding ASCII

    $serverLauncher = Join-Path $TargetDir "Start SWM Server.cmd"
    $serverText = @"
@echo off
setlocal
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.runtime\ms-playwright"
set "SWM_PORT=$ServerPort"
echo Starting Simple Webcrawl Manager on http://127.0.0.1:%SWM_PORT%
start "SWM Server" /D "%~dp0" "%~dp0.venv\Scripts\swm.exe" serve --host 127.0.0.1 --port %SWM_PORT%
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:%SWM_PORT%"
endlocal
"@
    $serverText | Set-Content -LiteralPath $serverLauncher -Encoding ASCII

    Set-Content -LiteralPath (Join-Path $TargetDir "server-port.txt") -Value $ServerPort -Encoding ASCII
    Write-Ok "Created double-click server launcher: $serverLauncher"
}

try {
    Write-Host "Simple Webcrawl Manager (SWM) - Windows Installer" -ForegroundColor White
    Write-Host "GitHub: $RepoBaseUrl"
    Write-Host "Branch: $Branch"
    Write-Host "Install directory: $InstallDir"
    Write-Host "Private runtime directory: $RuntimeRoot"

    $isAdmin = Test-IsAdmin
    Write-Info ("Privilege level: " + $(if ($isAdmin) { "Administrator" } else { "Standard user" }))
    Write-Info "SWM uses a per-user private Python runtime; administrator rights are not required for Python."

    Write-Step "1. Download / update SWM"
    Sync-Source -TargetDir $InstallDir -BranchName $Branch
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "pyproject.toml"))) {
        throw "pyproject.toml is missing after source download."
    }
    Write-Ok "SWM source is ready at $InstallDir."

    Write-Step "2. Prepare private Python runtime"
    $uvExe = Install-PortableUv -TargetDir $InstallDir
    $venvPython = Install-PrivatePythonEnvironment -TargetDir $InstallDir -UvExe $uvExe -PrivateRuntimeRoot $RuntimeRoot
    $pythonVersion = (& $venvPython -c "import sys; print(sys.version.split()[0])" | Select-Object -First 1)
    Write-Ok "Private Python $pythonVersion is ready. No system Python was installed."

    Write-Step "3. Check Google Chrome"
    $chrome = Get-ChromePath
    if ($chrome) {
        Write-Ok "Google Chrome found: $chrome"
    } else {
        Write-Warning "Google Chrome was not found. SWM's dashboard and headless crawling will work using the private Playwright Chromium runtime."
        Write-Warning "Interactive 'swm record' headed/native modes currently require Google Chrome. No administrator-level Chrome installation will be attempted."
    }

    Write-Step "4. Verify SWM"
    $swmExe = Join-Path $InstallDir ".venv\Scripts\swm.exe"
    if (-not (Test-Path -LiteralPath $swmExe)) {
        throw "SWM executable was not created at $swmExe."
    }
    & $swmExe --help *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "SWM CLI smoke test failed."
    }
    Write-Ok "SWM CLI smoke test passed."

    Write-Step "5. Check local ports"
    $actualDashboardPort = Resolve-Port -Name "Dashboard" -PreferredPort $DashboardPort
    $actualReplayPort = Resolve-Port -Name "Replay" -PreferredPort $ReplayPort

    Write-Step "6. Create launchers"
    Write-Launchers -TargetDir $InstallDir -ServerPort $actualDashboardPort

    Write-Step "Installation complete"
    Write-Host "Installed to: $InstallDir" -ForegroundColor Green
    Write-Host "Private Python runtime: $RuntimeRoot\python"
    Write-Host ""
    Write-Host "To start SWM, double-click:"
    Write-Host "  $InstallDir\Start SWM Server.cmd" -ForegroundColor Green
    Write-Host ""
    Write-Host "The dashboard will open at:"
    Write-Host "  http://127.0.0.1:$actualDashboardPort" -ForegroundColor Green
    Write-Host ""
    Write-Host "CLI:"
    Write-Host "  $InstallDir\swm.cmd --help"
    Write-Host ""
    Write-Host "Replay default port: $actualReplayPort"
    exit 0
} catch {
    Write-Host ""
    Write-Host "INSTALLATION FAILED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "SWM does not require a system-wide Python installation."
    Write-Host "Private Python runtime location: $RuntimeRoot"
    exit 1
}
