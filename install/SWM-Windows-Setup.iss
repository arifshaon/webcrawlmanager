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
#define BootstrapWrapper "run-bootstrap.ps1"
#define SourceBundle "SWM-source.zip"

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
; {autopf} maps to Program Files for an all-users/admin install and to the
; current user's Programs folder for a normal per-user install. The directory
; page is intentionally shown so the user can choose another writable folder.
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

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#BootstrapScript}"; Flags: dontcopy
Source: "{#BootstrapWrapper}"; Flags: dontcopy
Source: "{#SourceBundle}"; Flags: dontcopy

; The bootstrap creates Start SWM Server.cmd in {app}. These shortcuts are
; created after the bootstrap finishes and give the end user a normal
; double-click entry point without needing a terminal.
[Icons]
Name: "{autoprograms}\Simple Webcrawl Manager"; Filename: "{app}\Start SWM Server.cmd"; WorkingDir: "{app}"; Comment: "Start the Simple Webcrawl Manager dashboard server"
Name: "{autodesktop}\Simple Webcrawl Manager"; Filename: "{app}\Start SWM Server.cmd"; WorkingDir: "{app}"; Comment: "Start the Simple Webcrawl Manager dashboard server"; Tasks: desktopicon

[Run]
Filename: "{app}\Start SWM Server.cmd"; Description: "Start Simple Webcrawl Manager now"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

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
  WrapperPath: String;
  SourceZipPath: String;
  InstallLogPath: String;
  LogText: AnsiString;
  LogTail: String;
  Params: String;
  Ok: Boolean;
begin
  if CurStep <> ssInstall then
    Exit;

  ExtractTemporaryFile('{#BootstrapScript}');
  ExtractTemporaryFile('{#BootstrapWrapper}');
  ExtractTemporaryFile('{#SourceBundle}');
  ScriptPath := ExpandConstant('{tmp}\{#BootstrapScript}');
  WrapperPath := ExpandConstant('{tmp}\{#BootstrapWrapper}');
  InstallLogPath := ExpandConstant('{app}\install.log');

  Params := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File ' +
            AddQuotes(WrapperPath) +
            ' -BootstrapScript ' + AddQuotes(ScriptPath) +
            ' -InstallDir ' + AddQuotes(ExpandConstant('{app}')) +
            ' -Branch ' + AddQuotes('{#SourceBranch}') +
            ' -SourceZip ' + AddQuotes(SourceZipPath);

  Log('Starting SWM bootstrap wrapper: ' + PowerShellExe() + ' ' + Params);
  Ok := Exec(PowerShellExe(), Params, '', SW_SHOW, ewWaitUntilTerminated,
             BootstrapExitCode);

  if not Ok then
    RaiseException('Windows could not start the SWM installation bootstrap.');

  if BootstrapExitCode <> 0 then
  begin
    LogTail := '';
    if LoadStringFromFile(InstallLogPath, LogText) then
    begin
      if Length(LogText) > 1800 then
        LogTail := Copy(String(LogText), Length(LogText) - 1799, 1800)
      else
        LogTail := String(LogText);
    end;

    if LogTail <> '' then
      RaiseException(
        'SWM installation failed (bootstrap exit code ' +
        IntToStr(BootstrapExitCode) + ').' + #13#10 + #13#10 +
        'Detailed log: ' + InstallLogPath + #13#10 + #13#10 +
        'Last installer output:' + #13#10 + LogTail)
    else
      RaiseException(
        'SWM installation failed (bootstrap exit code ' +
        IntToStr(BootstrapExitCode) + ').' + #13#10 +
        'Detailed log: ' + InstallLogPath);
  end;
end;