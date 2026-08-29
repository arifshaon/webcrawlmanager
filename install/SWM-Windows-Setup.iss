; Simple Webcrawl Manager (SWM) Windows bootstrap installer
; Built with Inno Setup 6. The resulting EXE should be Authenticode-signed
; before release distribution. Unsigned builds are supported for testing.

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef SourceBranch
  #define SourceBranch "feature/record-session"
#endif

#define AppName "Simple Webcrawl Manager"
#define AppPublisher "Arif Shaon"
#define AppURL "https://github.com/arifshaon/webcrawlmanager"
#define BootstrapScript "install-windows.ps1"

[Setup]
AppId={{E131A061-383B-4C35-A5DF-B5C944552F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Windows Installer
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
; Inno Setup's {autopf} maps to Program Files for an all-users/admin install
; and to the current user's Programs folder for a per-user install. The user
; can change this path on the Select Destination Location page.
DefaultDirName={autopf}\Simple Webcrawl Manager
DisableProgramGroupPage=yes
DisableDirPage=no
DisableReadyMemo=no
DisableReadyPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=dist
OutputBaseFilename=SWM-Setup-{#AppVersion}
Uninstallable=no
SetupLogging=yes
CloseApplications=no
RestartApplications=no

[Files]
Source: "{#BootstrapScript}"; Flags: dontcopy

[Code]
var
  BootstrapExitCode: Integer;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ScriptPath: String;
  Params: String;
  Ok: Boolean;
begin
  if CurStep <> ssInstall then
    Exit;

  ExtractTemporaryFile('{#BootstrapScript}');
  ScriptPath := ExpandConstant('{tmp}\{#BootstrapScript}');

  Params := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File ' +
            AddQuotes(ScriptPath) +
            ' -InstallDir ' + AddQuotes(ExpandConstant('{app}')) +
            ' -Branch ' + AddQuotes('{#SourceBranch}');

  Log('Starting SWM bootstrap: ' + PowerShellExe() + ' ' + Params);
  Ok := Exec(PowerShellExe(), Params, '', SW_SHOW, ewWaitUntilTerminated,
             BootstrapExitCode);

  if not Ok then
    RaiseException('Windows could not start the SWM installation bootstrap.');

  if BootstrapExitCode <> 0 then
    RaiseException(
      'SWM installation failed (bootstrap exit code ' +
      IntToStr(BootstrapExitCode) +
      '). Review the PowerShell output and setup log.');
end;
