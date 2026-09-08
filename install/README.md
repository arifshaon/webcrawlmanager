# Windows installer

The Windows release installer is built from `SWM-Windows-Setup.iss` with Inno Setup 6 and then Authenticode-signed with `build-windows-installer.ps1`.

## End-user installer

The final distributable is:

```text
install/dist/SWM-Setup-<version>.exe
```

The installer shows a normal **Select Destination Location** page. For a standard-user installation the default resolves to the current user's Programs area; an administrator/all-users installation can use Program Files. The user can browse to another writable location.

The EXE embeds `install-windows.ps1`. During installation it:

- downloads or updates SWM from the configured GitHub branch;
- does **not** install Python system-wide;
- downloads a pinned portable `uv` binary and verifies its published SHA-256;
- installs a pinned private CPython 3.13 runtime under `<install>/.runtime/python` with no Windows Python-registry registration and no PATH changes;
- installs SWM directly into that private interpreter (no `.venv`), including dashboard, Instagram `gallery-dl` listing support, and YouTube `yt-dlp[default,curl-cffi]` support;
- installs Playwright Chromium under `<install>/.runtime/ms-playwright`;
- attempts to install `ffmpeg` and Deno through `winget` for full YouTube download/challenge support;
- checks whether Google Chrome is present for the interactive headed/native recorder, but does not attempt an administrator-level Chrome installation;
- verifies the SWM CLI;
- checks the dashboard and replay ports, selecting the next free local port when a preferred port is occupied;
- creates `Start SWM Server.cmd`, which can be double-clicked to start the dashboard and open the default browser.

The Inno installer also creates a Start Menu shortcut and, by default, a desktop shortcut pointing to `Start SWM Server.cmd`. The Finish page offers to start SWM immediately.

### Why the private Python runtime

The application must be installable on managed Windows desktops where the user cannot install Python machine-wide. SWM therefore treats Python as an application runtime, not as a system prerequisite. The managed Python files remain inside the selected SWM directory and can be removed with the application files.

The portable bootstrapper is pinned to `uv 0.11.29` for reproducibility. Its Windows x64 archive SHA-256 is checked before extraction. The installer also downloads the pinned CPython 3.13 runtime from Astral's `python-build-standalone` release, verifies its SHA-256, and keeps the complete runtime inside the selected SWM directory.

## Proper Windows signature

A public/enterprise-trusted Windows publisher identity cannot be created from source code alone. The build machine must have a trusted **Code Signing** certificate with an accessible private key. The private key must never be committed to this repository.

The certificate subject is what Windows displays as the publisher. For example, Windows will only display `Qatar National Library` as the verified publisher if the signing certificate itself is issued to that organisation.

The release build script uses:

- SHA-256 Authenticode signing;
- RFC3161 timestamping;
- Windows SDK `signtool.exe` verification with the Authenticode policy;
- PowerShell `Get-AuthenticodeSignature` as a second verification step.

The timestamp is important: a correctly timestamped executable can continue to validate after the signing certificate itself expires, provided the certificate was valid when it was signed.

## Build prerequisites

On the Windows release/build machine install:

1. Inno Setup 6 (`ISCC.exe`).
2. Windows SDK signing tools (`signtool.exe`).
3. A trusted code-signing certificate in either:
   - `Cert:\CurrentUser\My`, or
   - `Cert:\LocalMachine\My`.

The certificate must contain the Code Signing EKU (`1.3.6.1.5.5.7.3.3`) and have an accessible private key.

## Build a signed release

Find the certificate thumbprint:

```powershell
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Select-Object Subject, Thumbprint, NotAfter
```

Then build and sign:

```powershell
.\install\build-windows-installer.ps1 `
  -CertificateThumbprint "YOUR_CERTIFICATE_THUMBPRINT"
```

For a machine-store certificate:

```powershell
.\install\build-windows-installer.ps1 `
  -CertificateThumbprint "YOUR_CERTIFICATE_THUMBPRINT" `
  -CertificateStore LocalMachine
```

The script refuses to produce an unsigned release by default.

## Verify the finished EXE

The build script already performs both checks below, but they can also be run manually:

```powershell
signtool verify /pa /all /v .\install\dist\SWM-Setup-0.4.0.exe
Get-AuthenticodeSignature .\install\dist\SWM-Setup-0.4.0.exe | Format-List *
```

For a release, `Get-AuthenticodeSignature` must report `Status : Valid` and `signtool verify` must succeed.

## Unsigned development build

Only for local testing:

```powershell
.\install\build-windows-installer.ps1 -AllowUnsigned
```

Do not distribute that build. A self-signed certificate is also not a substitute for a trusted release signature unless every target Windows computer explicitly trusts that private CA/certificate.
