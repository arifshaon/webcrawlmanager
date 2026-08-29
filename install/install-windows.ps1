#requires -Version 5.1
<#
.SYNOPSIS
    Windows installer/bootstrapper for Simple Webcrawl Manager (SWM).

.DESCRIPTION
    Installs the current feature/record-session version of SWM into a per-user
    application directory by default. The installer:

      * downloads/updates the requested GitHub ref (git clone/pull when Git is
        available, otherwise a GitHub ZIP download);
      * detects a supported Python (3.10+);
      * with permission, installs Python 3.13 through winget when Python is
        missing/too old, using machine scope when already elevated and user
        scope otherwise;
      * creates an isolated .venv and installs SWM + dashboard dependencies;
      * installs the Playwright Chromium runtime;
      * checks for Google Chrome (required by interactive headed/native record
        mode) and can install it with winget with user permission;
      * verifies the SWM CLI;
      * checks the dashboard and replay ports before reporting completion.

    The application itself is installed per-user even when this script is run
    elevated. This keeps SWM's writable WARC/replay/state folders out of
    Program Files. Python's installation scope is what follows the caller's
    privilege level.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install\install-windows.ps1

.EXAMPLE
    .\install\install-windows.ps1 -InstallDir "D:\Apps\SWM" -Yes
#>

[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "SimpleWebcrawlManager"),
    [string]$Ref = "feature/record-session",
    [int]$DashboardPort = 8080,
    [int]$ReplayPort = 8091,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# GitHub source for this installer/version.
$RepoOwner = "arifshaon"
$RepoName = "webcrawlmanager"
$RepoUrl = "https://github.com/$RepoOwner/$RepoName.git"
$RepoBaseUrl = "https://github.com/$RepoOwner/$RepoName"
$MinimumPython = [Version]"3.10"
$WingetPythonId = "Python.Python.3.13"
$WingetChromeId = "Google.Chrome"

# Windows PowerShell 5.1 can otherwise negotiate an obsolete TLS version on
# older systems when downloading from GitHub/PyPI.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # PowerShell 7+ / modern .NET does not need this.
}

function Write-Title([string]$Text) {
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "[OK] $Text" -ForegroundColor Green
}

function Write-Info([string]$Text) {
    Write-Host "[INFO] $Text" -ForegroundColor Gray
}

function Confirm-InstallAction([string]$Prompt) {
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

function Test-IsAdministrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [Parameter(Mandatory=$true)][string]$Description
    )
    Write-Info $Description
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Get-DirectoryIsEmpty([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    return @((Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue)).Count -eq 0
}

function Get-PythonCandidate {
    $candidates = @()

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidates += [pscustomobject]@{
            Exe = $py.Source
            Prefix = @("-3")
            Label = "py -3"
        }
    }

    foreach ($name in @("python.exe", "python3.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            $candidates += [pscustomobject]@{
                Exe = $cmd.Source
                Prefix = @()
                Label = $name
            }
        }
    }

    # winget installs may not be visible to this already-running PowerShell
    # process immediately, so also inspect the standard install locations.
    $known = @()
    if ($env:LOCALAPPDATA) {
        $known += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe") `
            -File -ErrorAction SilentlyContinue
    }
    if ($env:ProgramFiles) {
        $known += Get-ChildItem -Path (Join-Path $env:ProgramFiles "Python*\python.exe") `
            -File -ErrorAction SilentlyContinue
    }
    if (${env:ProgramFiles(x86)}) {
        $known += Get-ChildItem -Path (Join-Path ${env:ProgramFiles(x86)} "Python*\python.exe") `
            -File -ErrorAction SilentlyContinue
    }
    foreach ($item in $known) {
        $candidates += [pscustomobject]@{
            Exe = $item.FullName
            Prefix = @()
            Label = $item.FullName
        }
    }

    $seen = @{}
    $best = $null
    foreach ($candidate in $candidates) {
        $key = $candidate.Exe + "|" + ($candidate.Prefix -join " ")
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true

        try {
            $args = @($candidate.Prefix) + @(
                "-c",
                "import sys; print('%d.%d.%d' % sys.version_info[:3])"
            )
            $raw = (& $candidate.Exe @args 2>$null | Select-Object -First 1)
            if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
            $version = [Version]($raw.Trim())
            $entry = [pscustomobject]@{
                Exe = $candidate.Exe
                Prefix = @($candidate.Prefix)
                Label = $candidate.Label
                Version = $version
                Supported = ($version -ge $MinimumPython)
            }
            if ($entry.Supported) {
                if ($null -eq $best -or $entry.Version -gt $best.Version) {
                    $best = $entry
                }
            } elseif ($null -eq $best) {
                $best = $entry
            }
        } catch {
            continue
        }
    }
    return $best
}

function Invoke-Python {
    param(
        [Parameter(Mandatory=$true)]$Python,
        [Parameter(Mandatory=$true)][string[]]$Arguments,
        [string]$Description = "Python command"
    )
    $args = @($Python.Prefix) + $Arguments
    Invoke-Checked -FilePath $Python.Exe -Arguments $args -Description $Description
}

function Install-PythonWithWinget([bool]$IsAdmin) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Warning "winget is not available on this Windows installation."
        Write-Host "Install Python 3.10 or later from: https://www.python.org/downloads/windows/"
        if (Confirm-InstallAction "Open the official Python download page now?") {
            Start-Process "https://www.python.org/downloads/windows/"
        }
        throw "Python installation is required. Install Python and run this installer again."
    }

    $scope = if ($IsAdmin) { "machine" } else { "user" }
    Write-Info "Installing Python 3.13 with winget ($scope scope)."
    if (-not $IsAdmin) {
        Write-Info "No administrator rights detected; Python will be installed for the current user."
    } else {
        Write-Info "Administrator rights detected; Python will be installed machine-wide."
    }

    $args = @(
        "install", "--id", $WingetPythonId, "-e",
        "--scope", $scope,
        "--accept-package-agreements", "--accept-source-agreements",
        "--disable-interactivity"
    )
    Invoke-Checked -FilePath $winget.Source -Arguments $args `
        -Description "Installing Python 3.13"
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

function Install-ChromeWithWinget {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Warning "winget is unavailable; install Google Chrome manually for interactive recording."
        Write-Host "https://www.google.com/chrome/"
        return
    }
    try {
        $args = @(
            "install", "--id", $WingetChromeId, "-e",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity"
        )
        Invoke-Checked -FilePath $winget.Source -Arguments $args `
            -Description "Installing Google Chrome"
    } catch {
        # Chrome is useful for interactive recording, but failure to install it
        # must not make the whole SWM installation unusable: headless crawling
        # can still use Playwright Chromium.
        Write-Warning $_.Exception.Message
        Write-Warning "SWM core installation will continue. Install Chrome manually before using 'swm record'."
    }
}

function Download-RepositoryZip([string]$TargetDir, [string]$GitRef) {
    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("swm-install-" + [Guid]::NewGuid().ToString("N"))
    $zipPath = Join-Path $tempRoot "source.zip"
    $extractPath = Join-Path $tempRoot "source"
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

    # refs/heads URLs work for branch names containing '/'. Escape each path
    # segment but preserve branch slashes as ref separators.
    $escapedRef = (($GitRef -split "/") | ForEach-Object { [Uri]::EscapeDataString($_) }) -join "/"
    $archiveUrl = "$RepoBaseUrl/archive/refs/heads/$escapedRef.zip"

    try {
        Write-Info "Git is unavailable (or this is a ZIP-based install); downloading $GitRef from GitHub."
        Invoke-WebRequest -Uri $archiveUrl -OutFile $zipPath -UseBasicParsing
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
        $sourceRoot = Get-ChildItem -LiteralPath $extractPath -Directory | Where-Object {
            Test-Path -LiteralPath (Join-Path $_.FullName "pyproject.toml")
        } | Select-Object -First 1
        if (-not $sourceRoot) {
            throw "Downloaded archive did not contain pyproject.toml."
        }

        New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

        # Preserve a locally edited config during ZIP-based refreshes. Git-based
        # installs use git's own dirty-tree protection instead.
        $targetConfig = Join-Path $TargetDir "config.yaml"
        if (Test-Path -LiteralPath $targetConfig) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            Copy-Item -LiteralPath $targetConfig -Destination "$targetConfig.$stamp.bak" -Force
            Write-Info "Existing config.yaml backed up before refresh."
        }

        foreach ($item in Get-ChildItem -LiteralPath $sourceRoot.FullName -Force) {
            Copy-Item -LiteralPath $item.FullName -Destination $TargetDir -Recurse -Force
        }
    } finally {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Sync-Repository([string]$TargetDir, [string]$GitRef) {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    $gitDir = Join-Path $TargetDir ".git"

    if ($git -and (Test-Path -LiteralPath $gitDir)) {
        Write-Info "Existing Git checkout found at $TargetDir."
        $dirty = & $git.Source -C $TargetDir status --porcelain
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect existing Git checkout." }
        if ($dirty) {
            throw "The existing SWM checkout has local changes. Commit/stash them before rerunning the installer so they are not overwritten."
        }
        Invoke-Checked -FilePath $git.Source -Arguments @("-C", $TargetDir, "fetch", "origin", $GitRef) `
            -Description "Fetching $GitRef from GitHub"
        Invoke-Checked -FilePath $git.Source -Arguments @("-C", $TargetDir, "checkout", $GitRef) `
            -Description "Checking out $GitRef"
        Invoke-Checked -FilePath $git.Source -Arguments @("-C", $TargetDir, "pull", "--ff-only", "origin", $GitRef) `
            -Description "Updating SWM from GitHub"
        return
    }

    if ($git -and (Get-DirectoryIsEmpty $TargetDir)) {
        $parent = Split-Path -Parent $TargetDir
        if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Invoke-Checked -FilePath $git.Source `
            -Arguments @("clone", "--branch", $GitRef, "--single-branch", $RepoUrl, $TargetDir) `
            -Description "Cloning SWM $GitRef from GitHub"
        return
    }

    Download-RepositoryZip -TargetDir $TargetDir -GitRef $GitRef
}

function Test-PortAvailable([int]$Port) {
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener(
            [System.Net.IPAddress]::Loopback, $Port)
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
        $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
            Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) { return "$($proc.ProcessName) (PID $($proc.Id))" }
            return "PID $($conn.OwningProcess)"
        }
    } catch {
        # Get-NetTCPConnection is unavailable on some older Windows builds.
    }
    return "another process"
}

function Find-FreePort([int]$StartPort) {
    for ($port = $StartPort; $port -lt ($StartPort + 100); $port++) {
        if (Test-PortAvailable $port) { return $port }
    }
    return $null
}

function Write-PortStatus([string]$Name, [int]$Port) {
    if (Test-PortAvailable $Port) {
        Write-Ok "$Name port $Port is free for binding on 127.0.0.1."
        return
    }
    $owner = Get-PortOwner $Port
    Write-Warning "$Name port $Port is already in use by $owner."
    $alternative = Find-FreePort ($Port + 1)
    if ($alternative) {
        Write-Host "      Suggested free port: $alternative" -ForegroundColor Yellow
    }
}

function Write-Launcher([string]$TargetDir) {
    $launcher = Join-Path $TargetDir "swm.cmd"
    $content = @'
@echo off
"%~dp0.venv\Scripts\swm.exe" %*
'@
    Set-Content -LiteralPath $launcher -Value $content -Encoding ASCII
}

try {
    Write-Host "Simple Webcrawl Manager (SWM) - Windows Installer" -ForegroundColor White
    Write-Host "Source: $RepoBaseUrl  ref: $Ref"
    Write-Host "Install directory: $InstallDir"

    $isAdmin = Test-IsAdministrator
    Write-Info ("Privilege level: " + $(if ($isAdmin) { "Administrator" } else { "Standard user" }))

    Write-Title "1. Download / update SWM"
    Sync-Repository -TargetDir $InstallDir -GitRef $Ref
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "pyproject.toml"))) {
        throw "SWM source download completed but pyproject.toml is missing from $InstallDir."
    }
    Write-Ok "SWM source is present at $InstallDir."

    Write-Title "2. Check Python"
    $python = Get-PythonCandidate
    if ($python -and $python.Supported) {
        Write-Ok "Python $($python.Version) found via $($python.Label)."
    } else {
        if ($python) {
            Write-Warning "Python $($python.Version) was found, but SWM requires Python 3.10 or later."
        } else {
            Write-Warning "Python 3.10 or later was not found."
        }
        if (-not (Confirm-InstallAction "Install Python 3.13 now using winget?")) {
            throw "Python 3.10+ is required. Installation cancelled by user."
        }
        Install-PythonWithWinget -IsAdmin $isAdmin
        Start-Sleep -Seconds 2
        $python = Get-PythonCandidate
        if (-not $python -or -not $python.Supported) {
            throw "Python installation completed but a usable Python 3.10+ could not be located. Open a new terminal and rerun the installer."
        }
        Write-Ok "Python $($python.Version) is ready."
    }

    Write-Title "3. Create isolated environment and install Python packages"
    $venvDir = Join-Path $InstallDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if (Test-Path -LiteralPath $venvPython) {
        try {
            $venvVersionText = (& $venvPython -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null | Select-Object -First 1)
            $venvVersion = [Version]$venvVersionText.Trim()
            if ($venvVersion -lt $MinimumPython) {
                Write-Warning "Existing .venv uses unsupported Python $venvVersion; recreating it."
                Remove-Item -LiteralPath $venvDir -Recurse -Force
            } else {
                Write-Ok "Existing .venv uses Python $venvVersion; reusing it."
            }
        } catch {
            Write-Warning "Existing .venv is not usable; recreating it."
            Remove-Item -LiteralPath $venvDir -Recurse -Force
        }
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-Python -Python $python -Arguments @("-m", "venv", $venvDir) `
            -Description "Creating SWM virtual environment"
    }

    Invoke-Checked -FilePath $venvPython `
        -Arguments @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel") `
        -Description "Updating pip/setuptools/wheel"

    # pyproject.toml declares the core dependencies and the dashboard extra.
    # Editable installation keeps the installed CLI tied to the GitHub checkout,
    # so a later installer update immediately updates the executable as well.
    $editableTarget = "$InstallDir[dashboard]"
    Invoke-Checked -FilePath $venvPython `
        -Arguments @("-m", "pip", "install", "-e", $editableTarget) `
        -Description "Installing SWM and dashboard dependencies"

    Invoke-Checked -FilePath $venvPython `
        -Arguments @("-m", "playwright", "install", "chromium") `
        -Description "Installing Playwright Chromium"
    Write-Ok "Python dependencies and Playwright Chromium are installed."

    Write-Title "4. Check Google Chrome for interactive recording"
    $chrome = Get-ChromePath
    if ($chrome) {
        Write-Ok "Google Chrome found: $chrome"
    } else {
        Write-Warning "Google Chrome was not found. 'swm record --browser headed/native' requires Chrome."
        if (Confirm-InstallAction "Install Google Chrome now using winget?") {
            Install-ChromeWithWinget
            $chrome = Get-ChromePath
            if ($chrome) {
                Write-Ok "Google Chrome found: $chrome"
            } else {
                Write-Warning "Chrome is still not detectable in this terminal. A sign-out/new terminal may be required."
            }
        } else {
            Write-Info "Skipping Chrome. Headless crawling can still use Playwright Chromium."
        }
    }

    Write-Title "5. Verify SWM"
    $swmExe = Join-Path $venvDir "Scripts\swm.exe"
    if (-not (Test-Path -LiteralPath $swmExe)) {
        throw "SWM console executable was not created at $swmExe."
    }
    & $swmExe --help *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "SWM CLI smoke test failed with exit code $LASTEXITCODE."
    }
    Write-Launcher -TargetDir $InstallDir
    Write-Ok "SWM CLI smoke test passed."

    Write-Title "6. Check local ports"
    Write-PortStatus -Name "Dashboard" -Port $DashboardPort
    Write-PortStatus -Name "Replay" -Port $ReplayPort

    Write-Title "Installation complete"
    Write-Host "Installed to: $InstallDir" -ForegroundColor Green
    Write-Host ""
    Write-Host "Run SWM from any Command Prompt/PowerShell with:"
    Write-Host "  $InstallDir\swm.cmd --help"
    Write-Host ""
    Write-Host "Start the dashboard (default port $DashboardPort):"
    Write-Host "  $InstallDir\swm.cmd serve --port $DashboardPort"
    Write-Host ""
    Write-Host "Replay an archive (default port $ReplayPort):"
    Write-Host "  $InstallDir\swm.cmd replay <warc-folder> --port $ReplayPort"
    Write-Host ""
    Write-Host "Interactive recording:"
    Write-Host "  $InstallDir\swm.cmd record https://example.org"
    Write-Host ""
    exit 0
} catch {
    Write-Host ""
    Write-Host "INSTALLATION FAILED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Nothing is silently elevated by this installer. If a machine-wide"
    Write-Host "Python install is required, run PowerShell as Administrator and retry."
    exit 1
}
