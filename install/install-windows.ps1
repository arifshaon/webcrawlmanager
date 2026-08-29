#requires -Version 5.1
<#
.SYNOPSIS
    Windows bootstrap installer for Simple Webcrawl Manager (SWM).

.DESCRIPTION
    Installs the feature/record-session build of SWM. It downloads or updates
    the source from GitHub, checks for Python 3.10+, offers to install Python
    3.13 with the user's permission, installs SWM into an isolated virtual
    environment, installs Playwright Chromium, checks Google Chrome for
    interactive recording, verifies the CLI, and checks the dashboard/replay
    ports before finishing.

    Python installation scope follows the caller's privileges:
      - elevated PowerShell -> machine-wide Python installation
      - standard PowerShell -> current-user Python installation

    SWM itself defaults to LOCALAPPDATA so its WARC/replay/state directories
    remain writable without administrator rights.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install\install-windows.ps1

.EXAMPLE
    .\install\install-windows.ps1 -InstallDir "D:\Apps\SWM" -Yes
#>

[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "SimpleWebcrawlManager"),
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
$RepoUrl = "https://github.com/$RepoOwner/$RepoName.git"
$RepoBaseUrl = "https://github.com/$RepoOwner/$RepoName"
$MinimumPython = [Version]"3.10"
$PythonWingetId = "Python.Python.3.13"
$ChromeWingetId = "Google.Chrome"

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
        return $principal.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
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
    & $Exe @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Test-DirectoryEmpty([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    return @((Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue)).Count -eq 0
}

function Get-PythonInfo {
    $candidates = @()

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidates += [pscustomobject]@{ Exe=$py.Source; Prefix=@("-3"); Label="py -3" }
    }

    foreach ($name in @("python.exe", "python3.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) {
            $candidates += [pscustomobject]@{ Exe=$cmd.Source; Prefix=@(); Label=$name }
        }
    }

    # winget can install Python successfully without refreshing PATH in this
    # already-running shell, so also search the normal install locations.
    $patterns = @()
    if ($env:LOCALAPPDATA) {
        $patterns += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe")
    }
    if ($env:ProgramFiles) {
        $patterns += (Join-Path $env:ProgramFiles "Python*\python.exe")
    }
    if (${env:ProgramFiles(x86)}) {
        $patterns += (Join-Path ${env:ProgramFiles(x86)} "Python*\python.exe")
    }
    foreach ($pattern in $patterns) {
        foreach ($item in @(Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue)) {
            $candidates += [pscustomobject]@{
                Exe=$item.FullName; Prefix=@(); Label=$item.FullName
            }
        }
    }

    $seen = @{}
    $bestSupported = $null
    $bestUnsupported = $null

    foreach ($candidate in $candidates) {
        $key = $candidate.Exe + "|" + ($candidate.Prefix -join " ")
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true

        try {
            $pythonArgs = @($candidate.Prefix) + @(
                "-c",
                "import sys; print('%d.%d.%d' % sys.version_info[:3])"
            )
            $raw = (& $candidate.Exe @pythonArgs 2>$null | Select-Object -First 1)
            if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
            $version = [Version]($raw.Trim())
            $info = [pscustomobject]@{
                Exe=$candidate.Exe
                Prefix=@($candidate.Prefix)
                Label=$candidate.Label
                Version=$version
                Supported=($version -ge $MinimumPython)
            }
            if ($info.Supported) {
                if ($null -eq $bestSupported -or $version -gt $bestSupported.Version) {
                    $bestSupported = $info
                }
            } elseif ($null -eq $bestUnsupported -or $version -gt $bestUnsupported.Version) {
                $bestUnsupported = $info
            }
        } catch {}
    }

    if ($bestSupported) { return $bestSupported }
    return $bestUnsupported
}

function Invoke-Python {
    param(
        [Parameter(Mandatory=$true)]$Python,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [Parameter(Mandatory=$true)][string]$Description
    )
    $allArguments = @($Python.Prefix) + $ArgumentList
    Invoke-External -Exe $Python.Exe -ArgumentList $allArguments -Description $Description
}

function Install-Python([bool]$IsAdmin) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Warning "winget is not available."
        Write-Host "Install Python 3.10+ from https://www.python.org/downloads/windows/"
        if (Confirm-Action "Open the official Python download page now?") {
            Start-Process "https://www.python.org/downloads/windows/"
        }
        throw "Python is required. Install it and rerun this installer."
    }

    $scope = if ($IsAdmin) { "machine" } else { "user" }
    if ($IsAdmin) {
        Write-Info "Administrator rights detected: Python will be installed machine-wide."
    } else {
        Write-Info "Standard-user session detected: Python will be installed for this user only."
    }

    Invoke-External -Exe $winget.Source -ArgumentList @(
        "install", "--id", $PythonWingetId, "-e",
        "--scope", $scope,
        "--accept-package-agreements", "--accept-source-agreements"
    ) -Description "Installing Python 3.13 ($scope scope)"
}

function Download-SourceZip([string]$TargetDir, [string]$BranchName) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("swm-" + [Guid]::NewGuid().ToString("N"))
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
        if (-not $root) { throw "The downloaded GitHub archive is not a valid SWM source tree." }

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
        # Ignore untracked runtime/install artefacts such as .venv, WARC output
        # and swm.cmd, but protect actual edits to tracked source files.
        $dirty = & $git.Source -C $TargetDir status --porcelain --untracked-files=no
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect the existing Git checkout." }
        if ($dirty) {
            throw "Tracked files in $TargetDir have local changes. Commit or stash them before updating."
        }
        Invoke-External -Exe $git.Source -ArgumentList @("-C", $TargetDir, "fetch", "origin", $BranchName) `
            -Description "Fetching $BranchName from GitHub"
        Invoke-External -Exe $git.Source -ArgumentList @("-C", $TargetDir, "checkout", $BranchName) `
            -Description "Checking out $BranchName"
        Invoke-External -Exe $git.Source -ArgumentList @("-C", $TargetDir, "pull", "--ff-only", "origin", $BranchName) `
            -Description "Updating SWM from GitHub"
        return
    }

    if ($git -and (Test-DirectoryEmpty $TargetDir)) {
        $parent = Split-Path -Parent $TargetDir
        if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Invoke-External -Exe $git.Source -ArgumentList @(
            "clone", "--branch", $BranchName, "--single-branch", $RepoUrl, $TargetDir
        ) -Description "Cloning SWM $BranchName from GitHub"
        return
    }

    Download-SourceZip -TargetDir $TargetDir -BranchName $BranchName
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

function Install-Chrome {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Warning "winget is unavailable. Install Chrome manually before using interactive recording."
        Write-Host "https://www.google.com/chrome/"
        return
    }
    try {
        Invoke-External -Exe $winget.Source -ArgumentList @(
            "install", "--id", $ChromeWingetId, "-e",
            "--accept-package-agreements", "--accept-source-agreements"
        ) -Description "Installing Google Chrome"
    } catch {
        Write-Warning $_.Exception.Message
        Write-Warning "Chrome installation failed, but SWM core installation can continue."
    }
}

function Test-PortAvailable([int]$Port) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
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
    } catch {}
    return "another process"
}

function Find-FreePort([int]$StartPort) {
    for ($port = $StartPort; $port -lt ($StartPort + 100); $port++) {
        if (Test-PortAvailable $port) { return $port }
    }
    return $null
}

function Show-PortStatus([string]$Name, [int]$Port) {
    if (Test-PortAvailable $Port) {
        Write-Ok "$Name port $Port is free for binding on 127.0.0.1."
        return
    }
    Write-Warning "$Name port $Port is already in use by $(Get-PortOwner $Port)."
    $next = Find-FreePort ($Port + 1)
    if ($next) { Write-Host "      Suggested free port: $next" -ForegroundColor Yellow }
}

function Write-Launcher([string]$TargetDir) {
    $launcher = Join-Path $TargetDir "swm.cmd"
    @'
@echo off
"%~dp0.venv\Scripts\swm.exe" %*
'@ | Set-Content -LiteralPath $launcher -Encoding ASCII
}

try {
    Write-Host "Simple Webcrawl Manager (SWM) - Windows Installer" -ForegroundColor White
    Write-Host "GitHub: $RepoBaseUrl"
    Write-Host "Branch: $Branch"
    Write-Host "Install directory: $InstallDir"

    $isAdmin = Test-IsAdmin
    Write-Info ("Privilege level: " + $(if ($isAdmin) { "Administrator" } else { "Standard user" }))

    Write-Step "1. Download / update SWM"
    Sync-Source -TargetDir $InstallDir -BranchName $Branch
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir "pyproject.toml"))) {
        throw "pyproject.toml is missing after source download."
    }
    Write-Ok "SWM source is ready at $InstallDir."

    Write-Step "2. Check Python"
    $python = Get-PythonInfo
    if ($python -and $python.Supported) {
        Write-Ok "Python $($python.Version) found via $($python.Label)."
    } else {
        if ($python) {
            Write-Warning "Python $($python.Version) is installed, but SWM requires Python 3.10+."
        } else {
            Write-Warning "Python 3.10+ was not found."
        }
        if (-not (Confirm-Action "Install Python 3.13 now using winget?")) {
            throw "Python 3.10+ is required. Installation cancelled by user."
        }
        Install-Python -IsAdmin $isAdmin
        Start-Sleep -Seconds 2
        $python = Get-PythonInfo
        if (-not $python -or -not $python.Supported) {
            throw "Python was installed but is not visible to this shell. Open a new PowerShell window and rerun the installer."
        }
        Write-Ok "Python $($python.Version) is ready."
    }

    Write-Step "3. Install SWM Python environment"
    $venvDir = Join-Path $InstallDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if (Test-Path -LiteralPath $venvPython) {
        try {
            $raw = (& $venvPython -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null |
                Select-Object -First 1)
            $venvVersion = [Version]($raw.Trim())
            if ($venvVersion -lt $MinimumPython) {
                Write-Warning "Existing .venv uses Python $venvVersion; recreating it."
                Remove-Item -LiteralPath $venvDir -Recurse -Force
            } else {
                Write-Ok "Existing .venv uses Python $venvVersion; reusing it."
            }
        } catch {
            Write-Warning "Existing .venv is unusable; recreating it."
            Remove-Item -LiteralPath $venvDir -Recurse -Force
        }
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-Python -Python $python -ArgumentList @("-m", "venv", $venvDir) `
            -Description "Creating isolated SWM virtual environment"
    }

    Invoke-External -Exe $venvPython -ArgumentList @(
        "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"
    ) -Description "Updating pip/setuptools/wheel"

    # Installs core dependencies plus FastAPI/Uvicorn dashboard support from
    # pyproject.toml. Editable mode keeps the CLI tied to the checked-out code.
    Invoke-External -Exe $venvPython -ArgumentList @(
        "-m", "pip", "install", "-e", "$InstallDir[dashboard]"
    ) -Description "Installing SWM and dashboard dependencies"

    Invoke-External -Exe $venvPython -ArgumentList @(
        "-m", "playwright", "install", "chromium"
    ) -Description "Installing Playwright Chromium"
    Write-Ok "SWM Python dependencies are installed."

    Write-Step "4. Check Google Chrome"
    $chrome = Get-ChromePath
    if ($chrome) {
        Write-Ok "Google Chrome found: $chrome"
    } else {
        Write-Warning "Google Chrome was not found. Interactive 'swm record' headed/native modes require Chrome."
        if (Confirm-Action "Install Google Chrome now using winget?") {
            Install-Chrome
            $chrome = Get-ChromePath
            if ($chrome) {
                Write-Ok "Google Chrome found: $chrome"
            } else {
                Write-Warning "Chrome is not visible yet. A new terminal/sign-in may be required."
            }
        } else {
            Write-Info "Skipping Chrome. Headless crawling can still use Playwright Chromium."
        }
    }

    Write-Step "5. Verify SWM"
    $swmExe = Join-Path $venvDir "Scripts\swm.exe"
    if (-not (Test-Path -LiteralPath $swmExe)) {
        throw "SWM executable was not created at $swmExe."
    }
    & $swmExe --help *> $null
    if ($LASTEXITCODE -ne 0) { throw "SWM CLI smoke test failed." }
    Write-Launcher -TargetDir $InstallDir
    Write-Ok "SWM CLI smoke test passed."

    Write-Step "6. Check local ports"
    Show-PortStatus -Name "Dashboard" -Port $DashboardPort
    Show-PortStatus -Name "Replay" -Port $ReplayPort

    Write-Step "Installation complete"
    Write-Host "Installed to: $InstallDir" -ForegroundColor Green
    Write-Host ""
    Write-Host "CLI:"
    Write-Host "  $InstallDir\swm.cmd --help"
    Write-Host ""
    Write-Host "Dashboard:"
    Write-Host "  $InstallDir\swm.cmd serve --port $DashboardPort"
    Write-Host ""
    Write-Host "Replay:"
    Write-Host "  $InstallDir\swm.cmd replay <warc-folder> --port $ReplayPort"
    Write-Host ""
    Write-Host "Interactive recording:"
    Write-Host "  $InstallDir\swm.cmd record https://example.org"
    exit 0
} catch {
    Write-Host ""
    Write-Host "INSTALLATION FAILED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "The installer never silently elevates itself. When Python is missing,"
    Write-Host "it asks before using winget and selects machine/user scope from the"
    Write-Host "privileges of the PowerShell session."
    exit 1
}
