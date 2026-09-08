# Windows installer

SWM 0.5.1 deliberately returns to the proven 0.2.0 Windows installer architecture.

## Runtime layout

The user chooses the installation directory. SWM keeps its private runtime beneath that directory:

```text
<install>\
  .runtime\
    python\python.exe
    uv\uv.exe
    ms-playwright\...
  webarc\...
  Start SWM Server.cmd
  swm.cmd
  install.log
```

No system-wide Python is installed and no separate LocalAppData Python runtime is used. The generated launchers explicitly call `<install>\.runtime\python\python.exe`.

## Download failure behaviour in 0.5.1

0.5.1 also updates the Playwright manual-download parser for the output format introduced in Playwright 1.58 and used by Playwright 1.62.

The 0.2.0 automatic download path is retained. When a required download fails, the installer no longer immediately terminates.

For direct downloads (SWM source ZIP, portable uv, CPython, and Playwright browser components), the user is shown the exact download URL and can choose:

- **YES** — open the URL, download the file manually, then select the downloaded file in a Windows file picker;
- **NO** — retry the automatic download;
- **CANCEL** — abort the installation.

Manually selected uv and CPython archives are checked against the same pinned SHA-256 values as automatically downloaded files.

If Python package installation fails, the installer offers the same retry/abort choice and allows the user to select a folder containing manually downloaded wheel/source packages for offline installation.

If Playwright Chromium download fails, the installer asks Playwright for its own current `--dry-run` download plan. The user can manually download each missing browser/support archive, select it, and the installer extracts it into the exact Playwright runtime location. The installer then verifies that Chromium launches before continuing.

## Build

The setup is compiled with Inno Setup 6:

```powershell
.\install\build-windows-installer.ps1 -AllowUnsigned
```

The development workflow builds and verifies:

```text
install/dist/SWM-Setup-0.5.1.exe
```

The repository distribution package is:

```text
install/SWM-Setup-0.5.1.zip
```

Repository ZIPs are treated as checked packages: the CI publishing step will not silently overwrite an existing ZIP for the same version.

## Signing

Unsigned builds are for development/testing. A production release should be Authenticode-signed with a trusted Code Signing certificate and RFC3161 timestamp, using `build-windows-installer.ps1`.
