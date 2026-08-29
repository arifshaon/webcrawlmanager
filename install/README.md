# Windows installer

The Windows release installer is built from `SWM-Windows-Setup.iss` with Inno Setup 6 and then Authenticode-signed with `build-windows-installer.ps1`.

## End-user installer

The final distributable is:

```text
install/dist/SWM-Setup-<version>.exe
```

The EXE embeds `install-windows.ps1`. During installation it downloads or updates SWM from the configured GitHub branch, checks Python 3.10+, asks before installing Python when needed, installs SWM and its dashboard dependencies into an isolated `.venv`, installs Playwright Chromium, checks Google Chrome, verifies the CLI, and checks the dashboard/replay ports.

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
signtool verify /pa /all /v .\install\dist\SWM-Setup-0.2.0.exe
Get-AuthenticodeSignature .\install\dist\SWM-Setup-0.2.0.exe | Format-List *
```

For a release, `Get-AuthenticodeSignature` must report `Status : Valid` and `signtool verify` must succeed.

## Unsigned development build

Only for local testing:

```powershell
.\install\build-windows-installer.ps1 -AllowUnsigned
```

Do not distribute that build. A self-signed certificate is also not a substitute for a trusted release signature unless every target Windows computer explicitly trusts that private CA/certificate.
