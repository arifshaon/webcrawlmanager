; Simple Webcrawl Manager (SWM) Windows bootstrap installer
; Built with Inno Setup 6. The resulting EXE should be Authenticode-signed
; before release distribution. Unsigned builds are supported for testing.

#ifndef AppVersion
  #define AppVersion "1.1.1"
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
Filename: "{app}\Start SWM Server.cmd"; Description: "Start the SWM dashboard now (uses Start SWM Server.cmd)"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

[Code]
var
  BootstrapExitCode: Integer;

  InstallModePage: TWizardPage;
  UpdateRadio: TNewRadioButton;
  FreshRadio: TNewRadioButton;
  InstallModeInfoLabel: TNewStaticText;

  SourcePage: TWizardPage;
  LatestReleaseRadio: TNewRadioButton;
  BranchRadio: TNewRadioButton;
  SourceHelpLabel: TNewStaticText;
  BranchLabel: TNewStaticText;
  BranchCombo: TNewComboBox;
  BranchStatusLabel: TNewStaticText;
  BranchesLoaded: Boolean;

  PortPage: TWizardPage;
  PortLabel: TNewStaticText;
  PortEdit: TNewEdit;
  PortCheckButton: TNewButton;
  PortStatusLabel: TNewStaticText;
  PortHelpLabel: TNewStaticText;
  PortInitialized: Boolean;

function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function IsExistingInstall: Boolean;
begin
  Result :=
    FileExists(AddBackslash(WizardDirValue) + 'Start SWM Server.cmd') or
    FileExists(AddBackslash(WizardDirValue) + '.swm-install.json') or
    FileExists(AddBackslash(WizardDirValue) + '.runtime\python\python.exe');
end;

procedure RefreshInstallModePage;
begin
  if IsExistingInstall then
  begin
    UpdateRadio.Enabled := True;
    UpdateRadio.Checked := True;
    FreshRadio.Checked := False;
    InstallModeInfoLabel.Caption :=
      'An existing SWM installation was found in this folder.' + #13#10 +
      'Update keeps the existing configuration/state and private runtime where possible, ' +
      'then refreshes the source and all dependencies.';
  end
  else
  begin
    UpdateRadio.Enabled := False;
    UpdateRadio.Checked := False;
    FreshRadio.Checked := True;
    InstallModeInfoLabel.Caption :=
      'No existing SWM installation was found in this folder. ' +
      'A fresh installation will be created.';
  end;
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

function ReadExistingPort: Integer;
var
  PortText: AnsiString;
  P: Integer;
begin
  Result := 8080;

  if LoadStringFromFile(AddBackslash(WizardDirValue) + 'server-port.txt', PortText) then
  begin
    P := StrToIntDef(Trim(String(PortText)), 8080);
    if (P >= 1) and (P <= 65535) then
      Result := P;
  end;
end;

function FindFreePort(StartPort: Integer): Integer;
var
  ScriptPath: String;
  OutputPath: String;
  ScriptText: String;
  Params: String;
  ExitCode: Integer;
  Ok: Boolean;
  PortText: AnsiString;
begin
  Result := 0;
  ScriptPath := ExpandConstant('{tmp}\swm-find-port.ps1');
  OutputPath := ExpandConstant('{tmp}\swm-free-port.txt');

  DeleteFile(OutputPath);

  ScriptText :=
    '$ErrorActionPreference = ''Stop''' + #13#10 +
    '$start = ' + IntToStr(StartPort) + #13#10 +
    '$last = [Math]::Min(65535, $start + 99)' + #13#10 +
    'for ($p = $start; $p -le $last; $p++) {' + #13#10 +
    '  $listener = $null' + #13#10 +
    '  try {' + #13#10 +
    '    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p)' + #13#10 +
    '    $listener.Start()' + #13#10 +
    '    $listener.Stop()' + #13#10 +
    '    Set-Content -LiteralPath "' + OutputPath + '" -Value $p -Encoding ASCII' + #13#10 +
    '    exit 0' + #13#10 +
    '  } catch {' + #13#10 +
    '    if ($listener) { try { $listener.Stop() } catch {} }' + #13#10 +
    '  }' + #13#10 +
    '}' + #13#10 +
    'exit 1' + #13#10;

  if not SaveStringToFile(ScriptPath, ScriptText, False) then
    Exit;

  Params := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File ' + AddQuotes(ScriptPath);
  Ok := Exec(PowerShellExe(), Params, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);

  if Ok and (ExitCode = 0) and LoadStringFromFile(OutputPath, PortText) then
    Result := StrToIntDef(Trim(String(PortText)), 0);
end;

procedure CheckOrFindPort(SetSuggested: Boolean);
var
  RequestedPort: Integer;
  FreePort: Integer;
begin
  RequestedPort := StrToIntDef(Trim(PortEdit.Text), 0);

  if (RequestedPort < 1) or (RequestedPort > 65535) then
  begin
    PortStatusLabel.Caption := 'Enter a port number between 1 and 65535.';
    Exit;
  end;

  PortStatusLabel.Caption := 'Checking port availability...';
  FreePort := FindFreePort(RequestedPort);

  if FreePort = 0 then
  begin
    PortStatusLabel.Caption :=
      'No available port was found in the next 100 ports.';
    Exit;
  end;

  if FreePort = RequestedPort then
  begin
    PortStatusLabel.Caption :=
      'Port ' + IntToStr(RequestedPort) + ' is available.';
  end
  else
  begin
    if SetSuggested then
    begin
      PortEdit.Text := IntToStr(FreePort);
      PortStatusLabel.Caption :=
        'Port ' + IntToStr(FreePort) + ' is available and has been selected.';
    end
    else
      PortStatusLabel.Caption :=
        'Port ' + IntToStr(RequestedPort) + ' is in use. Next available: ' +
        IntToStr(FreePort) + '.';
  end;
end;

procedure PortCheckClick(Sender: TObject);
begin
  CheckOrFindPort(True);
end;

procedure PortEditChange(Sender: TObject);
begin
  PortStatusLabel.Caption := 'Click "Check / find available" to verify this port.';
end;

procedure InitializeWizard;
begin
  BranchesLoaded := False;
  PortInitialized := False;

  InstallModePage := CreateCustomPage(
    wpSelectDir,
    'Installation mode',
    'Choose whether to update an existing SWM installation or install fresh.'
  );

  UpdateRadio := TNewRadioButton.Create(InstallModePage);
  UpdateRadio.Parent := InstallModePage.Surface;
  UpdateRadio.Left := 0;
  UpdateRadio.Top := ScaleY(10);
  UpdateRadio.Width := InstallModePage.SurfaceWidth;
  UpdateRadio.Height := ScaleY(24);
  UpdateRadio.Caption := 'Update existing installation (recommended when detected)';

  FreshRadio := TNewRadioButton.Create(InstallModePage);
  FreshRadio.Parent := InstallModePage.Surface;
  FreshRadio.Left := 0;
  FreshRadio.Top := UpdateRadio.Top + UpdateRadio.Height + ScaleY(10);
  FreshRadio.Width := InstallModePage.SurfaceWidth;
  FreshRadio.Height := ScaleY(24);
  FreshRadio.Caption := 'Fresh installation / reinstall';

  InstallModeInfoLabel := TNewStaticText.Create(InstallModePage);
  InstallModeInfoLabel.Parent := InstallModePage.Surface;
  InstallModeInfoLabel.Left := ScaleX(28);
  InstallModeInfoLabel.Top := FreshRadio.Top + FreshRadio.Height + ScaleY(10);
  InstallModeInfoLabel.Width := InstallModePage.SurfaceWidth - ScaleX(28);
  InstallModeInfoLabel.Height := ScaleY(70);
  InstallModeInfoLabel.AutoSize := False;
  InstallModeInfoLabel.WordWrap := True;

  SourcePage := CreateCustomPage(
    InstallModePage.ID,
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

  PortPage := CreateCustomPage(
    SourcePage.ID,
    'Dashboard port',
    'Choose the local port used by the SWM dashboard.'
  );

  PortLabel := TNewStaticText.Create(PortPage);
  PortLabel.Parent := PortPage.Surface;
  PortLabel.Left := 0;
  PortLabel.Top := ScaleY(12);
  PortLabel.Caption := 'Dashboard port:';

  PortEdit := TNewEdit.Create(PortPage);
  PortEdit.Parent := PortPage.Surface;
  PortEdit.Left := 0;
  PortEdit.Top := PortLabel.Top + PortLabel.Height + ScaleY(6);
  PortEdit.Width := ScaleX(130);
  PortEdit.Height := ScaleY(24);
  PortEdit.Text := '8080';
  PortEdit.OnChange := @PortEditChange;

  PortCheckButton := TNewButton.Create(PortPage);
  PortCheckButton.Parent := PortPage.Surface;
  PortCheckButton.Left := PortEdit.Left + PortEdit.Width + ScaleX(12);
  PortCheckButton.Top := PortEdit.Top - ScaleY(1);
  PortCheckButton.Width := ScaleX(160);
  PortCheckButton.Height := ScaleY(27);
  PortCheckButton.Caption := 'Check / find available';
  PortCheckButton.OnClick := @PortCheckClick;

  PortStatusLabel := TNewStaticText.Create(PortPage);
  PortStatusLabel.Parent := PortPage.Surface;
  PortStatusLabel.Left := 0;
  PortStatusLabel.Top := PortEdit.Top + PortEdit.Height + ScaleY(12);
  PortStatusLabel.Width := PortPage.SurfaceWidth;
  PortStatusLabel.Height := ScaleY(28);
  PortStatusLabel.Caption := 'Port availability has not been checked yet.';

  PortHelpLabel := TNewStaticText.Create(PortPage);
  PortHelpLabel.Parent := PortPage.Surface;
  PortHelpLabel.Left := 0;
  PortHelpLabel.Top := PortStatusLabel.Top + PortStatusLabel.Height + ScaleY(12);
  PortHelpLabel.Width := PortPage.SurfaceWidth;
  PortHelpLabel.Height := ScaleY(70);
  PortHelpLabel.AutoSize := False;
  PortHelpLabel.WordWrap := True;
  PortHelpLabel.Caption :=
    'The dashboard will use http://127.0.0.1:<port>. After installation, ' +
    'Start SWM Server.cmd is the normal file to start the dashboard. ' +
    'The selected port is stored in server-port.txt and can be changed later.';

  RefreshInstallModePage;
  UpdateSourceControls;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  RequestedPort: Integer;
  FreePort: Integer;
begin
  Result := True;

  if CurPageID = SourcePage.ID then
  begin
    if BranchRadio.Checked then
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
  end
  else if CurPageID = PortPage.ID then
  begin
    RequestedPort := StrToIntDef(Trim(PortEdit.Text), 0);

    if (RequestedPort < 1) or (RequestedPort > 65535) then
    begin
      MsgBox('Enter a dashboard port between 1 and 65535.', mbError, MB_OK);
      Result := False;
      Exit;
    end;

    FreePort := FindFreePort(RequestedPort);

    if FreePort = 0 then
    begin
      MsgBox(
        'No available port was found between ' + IntToStr(RequestedPort) +
        ' and the next 99 ports.',
        mbError,
        MB_OK
      );
      Result := False;
    end
    else if FreePort <> RequestedPort then
    begin
      PortEdit.Text := IntToStr(FreePort);
      PortStatusLabel.Caption :=
        'Port ' + IntToStr(RequestedPort) + ' is in use. ' +
        'Port ' + IntToStr(FreePort) + ' has been selected instead.';
      MsgBox(
        'Port ' + IntToStr(RequestedPort) + ' is currently in use.' + #13#10 + #13#10 +
        'The next available port, ' + IntToStr(FreePort) + ', has been selected.' + #13#10 +
        'Review it and click Next again.',
        mbInformation,
        MB_OK
      );
      Result := False;
    end
    else
    begin
      PortStatusLabel.Caption :=
        'Port ' + IntToStr(RequestedPort) + ' is available.';
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  PortText: AnsiString;
  FinalPort: String;
begin
  if CurPageID = InstallModePage.ID then
    RefreshInstallModePage
  else if CurPageID = PortPage.ID then
  begin
    if not PortInitialized then
    begin
      PortEdit.Text := IntToStr(ReadExistingPort);
      PortInitialized := True;
      CheckOrFindPort(False);
    end;
  end
  else if CurPageID = wpFinished then
  begin
    FinalPort := PortEdit.Text;
    if LoadStringFromFile(ExpandConstant('{app}\server-port.txt'), PortText) then
      FinalPort := Trim(String(PortText));

    WizardForm.FinishedLabel.Caption :=
      'Simple Webcrawl Manager has been installed.' + #13#10 + #13#10 +
      'Dashboard: http://127.0.0.1:' + FinalPort + #13#10 + #13#10 +
      'To start SWM later, double-click:' + #13#10 +
      ExpandConstant('{app}\Start SWM Server.cmd') + #13#10 + #13#10 +
      'START-HERE.txt in the installation folder contains the same instructions.';
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
  InstallMode: String;
  DashboardPort: Integer;
  Ok: Boolean;
begin
  if CurStep <> ssInstall then
    Exit;

  ExtractTemporaryFile('{#BootstrapScript}');
  ExtractTemporaryFile('{#BootstrapWrapper}');
  ScriptPath := ExpandConstant('{tmp}\{#BootstrapScript}');
  WrapperPath := ExpandConstant('{tmp}\{#BootstrapWrapper}');
  InstallLogPath := ExpandConstant('{app}\install.log');

  if UpdateRadio.Checked and UpdateRadio.Enabled then
    InstallMode := 'Update'
  else
    InstallMode := 'Fresh';

  DashboardPort := StrToIntDef(Trim(PortEdit.Text), 8080);

  Params := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File ' +
            AddQuotes(WrapperPath) +
            ' -BootstrapScript ' + AddQuotes(ScriptPath) +
            ' -InstallDir ' + AddQuotes(ExpandConstant('{app}')) +
            ' -InstallMode ' + InstallMode +
            ' -InstallerVersion ' + AddQuotes('{#AppVersion}') +
            ' -DashboardPort ' + IntToStr(DashboardPort);

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
