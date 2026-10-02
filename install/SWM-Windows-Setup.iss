; Simple Webcrawl Manager (SWM) Windows bootstrap installer
; Built with Inno Setup 6. The resulting EXE should be Authenticode-signed
; before release distribution. Unsigned builds are supported for testing.

#ifndef AppVersion
  #define AppVersion "1.1"
#endif
#ifndef SourceBranch
  #define SourceBranch "main"
#endif

#define AppName "Simple Webcrawl Manager"
#define AppPublisher "Arif Shaon"
#define AppURL "https://github.com/arifshaon/webcrawlmanager"
#define BootstrapScript "install-windows.ps1"
#define BootstrapWrapper "run-bootstrap.ps1"

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
  SourcePage: TWizardPage;
  LatestReleaseRadio: TNewRadioButton;
  BranchRadio: TNewRadioButton;
  SourceHelpLabel: TNewStaticText;
  BranchLabel: TNewStaticText;
  BranchCombo: TNewComboBox;
  BranchStatusLabel: TNewStaticText;
  BranchesLoaded: Boolean;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

procedure LoadBranches;
var
  ScriptPath: String;
  OutputPath: String;
  ScriptText: String;
  Params: String;
  ExitCode: Integer;
  Ok: Boolean;
  Lines: TArrayOfString;
  I: Integer;
  Name: String;
  MainIndex: Integer;
begin
  if BranchesLoaded then
    Exit;

  BranchStatusLabel.Caption := 'Loading branches from GitHub...';
  BranchCombo.Items.Clear;
  BranchCombo.Items.Add('main');
  BranchCombo.ItemIndex := 0;

  ScriptPath := ExpandConstant('{tmp}\swm-list-branches.ps1');
  OutputPath := ExpandConstant('{tmp}\swm-branches.txt');

  ScriptText :=
    '$ErrorActionPreference = ''Stop''' + #13#10 +
    '$headers = @{ Accept = ''application/vnd.github+json''; ''User-Agent'' = ''SWM-Windows-Installer'' }' + #13#10 +
    '$all = @()' + #13#10 +
    '$page = 1' + #13#10 +
    'do {' + #13#10 +
    '  $uri = ''https://api.github.com/repos/arifshaon/webcrawlmanager/branches?per_page=100&page='' + $page' + #13#10 +
    '  $batch = @(Invoke-RestMethod -Uri $uri -Headers $headers -UseBasicParsing)' + #13#10 +
    '  $all += @($batch | ForEach-Object { $_.name })' + #13#10 +
    '  $page++' + #13#10 +
    '} while ($batch.Count -eq 100)' + #13#10 +
    '$all | Sort-Object -Unique | Set-Content -LiteralPath "' + OutputPath + '" -Encoding UTF8' + #13#10;

  if not SaveStringToFile(ScriptPath, ScriptText, False) then
  begin
    BranchStatusLabel.Caption := 'Could not prepare branch lookup. Using main.';
    BranchesLoaded := True;
    Exit;
  end;

  Params := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File ' +
            AddQuotes(ScriptPath);

  Ok := Exec(PowerShellExe(), Params, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);

  if Ok and (ExitCode = 0) and LoadStringsFromFile(OutputPath, Lines) and
     (GetArrayLength(Lines) > 0) then
  begin
    BranchCombo.Items.Clear;
    MainIndex := -1;

    for I := 0 to GetArrayLength(Lines) - 1 do
    begin
      Name := Trim(Lines[I]);
      if Name <> '' then
      begin
        BranchCombo.Items.Add(Name);
        if CompareText(Name, 'main') = 0 then
          MainIndex := BranchCombo.Items.Count - 1;
      end;
    end;

    if BranchCombo.Items.Count = 0 then
    begin
      BranchCombo.Items.Add('main');
      BranchCombo.ItemIndex := 0;
      BranchStatusLabel.Caption := 'No branch list was returned. Using main.';
    end
    else
    begin
      if MainIndex >= 0 then
        BranchCombo.ItemIndex := MainIndex
      else
        BranchCombo.ItemIndex := 0;

      BranchStatusLabel.Caption :=
        'Loaded ' + IntToStr(BranchCombo.Items.Count) + ' branch(es) from GitHub.';
    end;
  end
  else
  begin
    BranchStatusLabel.Caption :=
      'Could not load the branch list from GitHub. Using main.';
  end;

  BranchesLoaded := True;
end;

procedure UpdateSourceControls;
begin
  BranchLabel.Enabled := BranchRadio.Checked;
  BranchCombo.Enabled := BranchRadio.Checked;
  BranchStatusLabel.Enabled := BranchRadio.Checked;
end;

procedure SourceChoiceClick(Sender: TObject);
begin
  if BranchRadio.Checked then
    LoadBranches;

  UpdateSourceControls;
end;

procedure InitializeWizard;
begin
  BranchesLoaded := False;

  SourcePage := CreateCustomPage(
    wpSelectDir,
    'Source version',
    'Choose which Simple Webcrawl Manager source should be installed.'
  );

  LatestReleaseRadio := TNewRadioButton.Create(SourcePage);
  LatestReleaseRadio.Parent := SourcePage.Surface;
  LatestReleaseRadio.Left := 0;
  LatestReleaseRadio.Top := ScaleY(10);
  LatestReleaseRadio.Width := SourcePage.SurfaceWidth;
  LatestReleaseRadio.Height := ScaleY(24);
  LatestReleaseRadio.Caption := 'Latest published release (recommended)';
  LatestReleaseRadio.Checked := True;
  LatestReleaseRadio.OnClick := @SourceChoiceClick;

  SourceHelpLabel := TNewStaticText.Create(SourcePage);
  SourceHelpLabel.Parent := SourcePage.Surface;
  SourceHelpLabel.Left := ScaleX(28);
  SourceHelpLabel.Top := LatestReleaseRadio.Top + LatestReleaseRadio.Height + ScaleY(6);
  SourceHelpLabel.Width := SourcePage.SurfaceWidth - ScaleX(28);
  SourceHelpLabel.Height := ScaleY(42);
  SourceHelpLabel.AutoSize := False;
  SourceHelpLabel.WordWrap := True;
  SourceHelpLabel.Caption :=
    'The installer will resolve the latest published GitHub release and download source from that release tag.';

  BranchRadio := TNewRadioButton.Create(SourcePage);
  BranchRadio.Parent := SourcePage.Surface;
  BranchRadio.Left := 0;
  BranchRadio.Top := SourceHelpLabel.Top + SourceHelpLabel.Height + ScaleY(14);
  BranchRadio.Width := SourcePage.SurfaceWidth;
  BranchRadio.Height := ScaleY(24);
  BranchRadio.Caption := 'Advanced: install from a GitHub branch';
  BranchRadio.OnClick := @SourceChoiceClick;

  BranchLabel := TNewStaticText.Create(SourcePage);
  BranchLabel.Parent := SourcePage.Surface;
  BranchLabel.Left := ScaleX(28);
  BranchLabel.Top := BranchRadio.Top + BranchRadio.Height + ScaleY(8);
  BranchLabel.Caption := 'Branch:';

  BranchCombo := TNewComboBox.Create(SourcePage);
  BranchCombo.Parent := SourcePage.Surface;
  BranchCombo.Left := ScaleX(28);
  BranchCombo.Top := BranchLabel.Top + BranchLabel.Height + ScaleY(6);
  BranchCombo.Width := SourcePage.SurfaceWidth - ScaleX(28);
  BranchCombo.Height := ScaleY(24);
  BranchCombo.Style := csDropDownList;
  BranchCombo.Items.Add('main');
  BranchCombo.ItemIndex := 0;

  BranchStatusLabel := TNewStaticText.Create(SourcePage);
  BranchStatusLabel.Parent := SourcePage.Surface;
  BranchStatusLabel.Left := ScaleX(28);
  BranchStatusLabel.Top := BranchCombo.Top + BranchCombo.Height + ScaleY(6);
  BranchStatusLabel.Width := SourcePage.SurfaceWidth - ScaleX(28);
  BranchStatusLabel.Height := ScaleY(24);
  BranchStatusLabel.Caption := 'Branches will be loaded from GitHub when Advanced is selected.';

  UpdateSourceControls;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;

  if (CurPageID = SourcePage.ID) and BranchRadio.Checked then
  begin
    LoadBranches;

    if (BranchCombo.ItemIndex < 0) or (Trim(BranchCombo.Text) = '') then
    begin
      MsgBox(
        'Select a GitHub branch, or select Latest published release.',
        mbError,
        MB_OK
      );
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ScriptPath: String;
  WrapperPath: String;
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
  ScriptPath := ExpandConstant('{tmp}\{#BootstrapScript}');
  WrapperPath := ExpandConstant('{tmp}\{#BootstrapWrapper}');
  InstallLogPath := ExpandConstant('{app}\install.log');

  Params := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File ' +
            AddQuotes(WrapperPath) +
            ' -BootstrapScript ' + AddQuotes(ScriptPath) +
            ' -InstallDir ' + AddQuotes(ExpandConstant('{app}'));

  if BranchRadio.Checked then
    Params := Params +
              ' -SourceMode Branch' +
              ' -Branch ' + AddQuotes(Trim(BranchCombo.Text))
  else
    Params := Params + ' -SourceMode LatestRelease';

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
