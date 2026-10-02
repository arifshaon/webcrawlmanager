# Windows installer

SWM's Windows installer sets SWM up as a self-contained application in a
folder of your choice, with its own Python and browser. This page explains
how it works and how it is built. For installing SWM, see the
[README](../README.md#install).

## The parts

| File | What it is |
|---|---|
| `SWM-Windows-Setup.iss` | The Inno Setup wizard: install folder, install mode, source, dashboard port and shortcuts. It builds `SWM-Setup-<version>.exe`. |
| `install-windows.ps1` | The installation engine the wizard runs. |
| `run-bootstrap.ps1` | Starts the engine from the wizard with the choices made. |
| `SWM-Setup-<version>.ps1` | The engine published on its own, an exact copy of `install-windows.ps1`, for installing without the `.exe`. |
| `build-windows-installer.ps1` | Builds (and signs) the `.exe` locally. |
| `SWM-Setup-0.*.zip`, `SWM-Setup-1.0.*` | Earlier installers, kept for reference. |

## What gets installed

Everything lives under the installation folder,
`%LOCALAPPDATA%\Programs\Simple Webcrawl Manager` by default:

```text
<install>\
  .runtime\
    python\python.exe        private CPython 3.13
    uv\uv.exe                package installer
    ms-playwright\...        Playwright's Chromium
    tools\ffmpeg.exe         Playwright's FFmpeg, for yt-dlp
  webarc\...                 SWM itself
  Start SWM Server.cmd       starts the dashboard and opens it in the browser
  swm.cmd                    the command line
  server-port.txt            the dashboard's port
  START-HERE.txt             how to start SWM and change its port
  .swm-install.json          what was installed, from where, and when
  install.log                the installation's log (when run from the wizard)
```

No system-wide Python is installed or used: the launchers call the Python
inside `.runtime`. Downloads of uv and CPython are checked against pinned
SHA-256 values. SWM is installed with every optional extra declared in
`pyproject.toml` (Instagram listing with gallery-dl, YouTube with yt-dlp,
the AI theme judge). Deno or Node.js, which some YouTube videos need, is
not included.

The wizard creates a Start menu shortcut, and a desktop shortcut if you
ask for one; both run `Start SWM Server.cmd`.

## Choices

The wizard and the script offer the same choices:

| Choice | Script option | Meaning |
|---|---|---|
| Install mode | `-InstallMode Fresh` or `Update` | **Update** (selected when SWM is already in the folder) keeps the configuration, jobs, settings and private runtime where possible and refreshes the source and dependencies; `config.yaml` is backed up first. **Fresh** replaces the private runtime. |
| Source | `-SourceMode LatestRelease`, or `-SourceMode Branch -Branch <name>` | The latest published GitHub release (recommended), or a branch of the repository. The wizard loads the branch list from GitHub. |
| Ports | `-DashboardPort`, `-ReplayPort` | 8080 and 8091 by default. If a port is taken, the next free one is used. |
| Folder | `-InstallDir` | Where to install. |
| Offline source | `-SourceArchivePath` | Install from a source zip you downloaded. |
| Unattended | `-NonInteractive` | Never ask; a failed download then stops the installation. |

To change the dashboard's port later, put another port in
`server-port.txt` and restart SWM.

## When a download fails

For the SWM source, uv, CPython and Playwright's browser, the installer
shows the exact address and offers:

- **Yes**: open the address, download the file yourself, then select it;
- **No**: try the automatic download again;
- **Cancel**: stop the installation.

Files you select by hand are checked against the same SHA-256 values. If
installing Python packages fails, you can select a folder of packages you
downloaded yourself. If Playwright's browser download fails, the installer
asks Playwright which files it needs, lets you download and select each
one, puts it in place and checks that Chromium starts.

## Building and releasing

Releases are built on GitHub's Windows runners by
`.github/workflows/release-windows-<version>.yml`. The workflow checks
that `SWM-Setup-<version>.ps1` is an exact copy of `install-windows.ps1`,
compiles the wizard with Inno Setup 6, signs the `.exe`, and publishes a
GitHub release with the `.exe`, the `.ps1` and their SHA-256 checksums,
plus build provenance:

```powershell
gh attestation verify SWM-Setup-1.1.1.exe --repo arifshaon/webcrawlmanager
```

The signing certificate is self-signed, so Windows does not trust it by
default and may warn before the installer runs. A production release
should be signed with a trusted code-signing certificate and an RFC 3161
timestamp. Never store private keys or certificate passwords in this
repository.

To build locally (Inno Setup 6 needed):

```powershell
.\install\build-windows-installer.ps1 -AllowUnsigned
```

`.github/workflows/build-unsigned-windows-installer.yml` builds an unsigned
installer for testing.
