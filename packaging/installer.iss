; ShoutAbout for Windows: the one-click installer (Inno Setup 6).
;
; Build it with packaging\build.ps1, which runs PyInstaller first and then calls ISCC with:
;   /DAppVersion=0.1.0  /DAppVersionNumeric=0.1.0.0
;   /DSourceDir=<packaging\dist\ShoutAbout>  /DAssetsDir=<packaging\build\assets>
;   /DOutputDir=<packaging\dist>
; and, only when a code-signing certificate is configured, /DSign plus
; /Sshoutabout=<signtool command> (see docs/installer.md).
;
; What it does on a PC (docs/installer.md has the full list):
;   * installs per machine into Program Files\ShoutAbout (one UAC prompt);
;   * adds a Start menu shortcut and one inbound Windows Firewall rule for ShoutAbout.exe,
;     Private networks only;
;   * on a first install, "Start ShoutAbout when I sign in" (ticked) writes the HKCU Run
;     value "ShoutAbout" for the user who ran Setup; after that the app's settings own it;
;   * stops a running ShoutAbout before replacing files (upgrades in place) and starts it
;     again afterwards;
;   * the uninstaller stops the app and removes the files, the firewall rule and the Run
;     value. Settings and words (%APPDATA%\shoutabout) stay unless the user
;     ticks "Also remove my settings and words" (or passes /REMOVEUSERDATA
;     to a /VERYSILENT uninstall).

#ifndef AppVersion
  #error Define AppVersion (build with packaging\build.ps1)
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric AppVersion + ".0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\ShoutAbout"
#endif
#ifndef AssetsDir
  #define AssetsDir "build\assets"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif

#define AppName "ShoutAbout"
#define AppExe "ShoutAbout.exe"
; Never change: Windows and upgrades identify the installed app by this id.
#define AppGuid "{B70711A6-7F23-457D-B910-31004776AB13}"
; Matches shoutabout.frozen.APP_USER_MODEL_ID.
#define AppUserModelId "ShoutAbout.App"
#define FirewallRule "ShoutAbout"
#define RunValue "ShoutAbout"
#define DataFolder "shoutabout"
; PyAV and its FFmpeg build must never ship (same list as packaging/bundle_rules.py FFMPEG).
#define FFmpegExcludes "\_internal\av,\_internal\av.libs,av-*.dist-info,avcodec*,avformat*,avutil*,avdevice*,avfilter*,swresample*,swscale*,postproc*,*x264*,*x265*"

#ifdef Sign
  #define SignFlag "signonce"
#else
  #define SignFlag ""
#endif

[Setup]
AppId={{#AppGuid}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
AppCopyright=Copyright (c) 2026 Spencer Seay. MIT License.
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
DefaultDirName={autopf}\{#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=no
PrivilegesRequired=admin
UsedUserAreasWarning=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809 or later (DNS-SD service registration needs it).
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile={#AssetsDir}\ShoutAbout.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardImageFile={#AssetsDir}\wizard-large-*.bmp
WizardSmallImageFile={#AssetsDir}\wizard-small-*.bmp
WizardImageAlphaFormat=defined
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
; ShoutAbout is stopped in PrepareToInstall; Restart Manager is only the fallback.
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no
SetupLogging=yes
#ifdef Sign
SignTool=shoutabout
SignedUninstaller=yes
#endif

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupWindowTitle=%1 Setup
WelcomeLabel1=Set up [name]
WelcomeLabel2=Private voice dictation. Nothing leaves your devices.
WizardSelectTasks=Starting ShoutAbout
SelectTasksDesc=
SelectTasksLabel2=
FinishedHeadingLabel=[name] is ready
FinishedLabelNoIcons=
FinishedLabel=
ConfirmUninstall=Remove %1 from this PC?
UninstalledAll=%1 was removed from this PC.

[Tasks]
Name: "autostart"; Description: "Start ShoutAbout when I sign in"

[InstallDelete]
; The bundle folder is entirely ours: clear it so an upgrade never mixes old and new files.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion {#SignFlag}
; Never shipped (packaging/bundle_rules.py already keeps them out of the build): *asio*
; (ASIO-enabled PortAudio builds) and PyAV with its FFmpeg DLLs (x264/x265 are GPL).
Source: "{#SourceDir}\*"; Excludes: "\{#AppExe},*asio*,{#FFmpegExcludes}"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#AssetsDir}\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
; ShoutAbout's own licence (MIT): its notice must come with every copy.
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Private voice dictation"; AppUserModelID: "{#AppUserModelId}"

[Run]
; Interactive installs: the "Start ShoutAbout now" box on the last page (runs as the user
; who started Setup, never elevated).
Filename: "{app}\{#AppExe}"; Description: "Start ShoutAbout now"; Flags: nowait postinstall skipifsilent
; Silent upgrades: start it again if it was running before.
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: RelaunchAfterSilentUpgrade

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppGuid}_is1';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  StartupApprovedKey = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run';

var
  IsUpgrade: Boolean;
  AppWasRunning: Boolean;
  RemoveUserData: Boolean;

{ ---------------------------------------------------------------- helpers }

function HasParam(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then begin
      Result := True;
      Exit;
    end;
end;

function AppIsRunning(): Boolean;
var
  Locator, Service, Found: Variant;
begin
  Result := False;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Found := Service.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name = ''{#AppExe}''');
    Result := Found.Count > 0;
  except
    Log('Could not list running processes: ' + GetExceptionMessage);
  end;
end;

function WaitForAppToExit(Tenths: Integer): Boolean;
var
  I: Integer;
begin
  for I := 1 to Tenths do begin
    if not AppIsRunning() then begin
      Result := True;
      Exit;
    end;
    Sleep(100);
  end;
  Result := not AppIsRunning();
end;

{ Stops every running ShoutAbout.exe (all signed-in users): first politely (taskkill without
  /F sends WM_CLOSE, and the tray quits cleanly), then forcefully. }
procedure StopApp();
var
  ResultCode: Integer;
begin
  if not AppIsRunning() then
    Exit;
  Log('ShoutAbout is running: asking it to quit');
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM {#AppExe}', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
  if WaitForAppToExit(50) then
    Exit;
  Log('ShoutAbout did not quit within 5 s: ending it');
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
  if not WaitForAppToExit(50) then
    Log('ShoutAbout is still running');
end;

procedure RunHidden(const FileName, Params, What: String);
var
  ResultCode: Integer;
begin
  if Exec(FileName, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log(Format('%s: exit code %d', [What, ResultCode]))
  else
    Log(Format('%s: could not run %s (%s)', [What, FileName, SysErrorMessage(ResultCode)]));
end;

procedure RemoveFirewallRule();
begin
  RunHidden(ExpandConstant('{sys}\netsh.exe'),
    'advfirewall firewall delete rule name="{#FirewallRule}"', 'Remove firewall rule');
end;

{ One inbound rule for the app, Private networks only: the phone can reach ShoutAbout at
  home, never on public Wi-Fi. Any TCP port, because the port is a setting. }
procedure AddFirewallRule();
begin
  RemoveFirewallRule();
  RunHidden(ExpandConstant('{sys}\netsh.exe'),
    'advfirewall firewall add rule name="{#FirewallRule}" dir=in action=allow protocol=TCP ' +
    'profile=private enable=yes program="' + ExpandConstant('{app}\{#AppExe}') + '" ' +
    'description="Lets your phone reach ShoutAbout on your home network (private networks only)."',
    'Add firewall rule');
end;

{ The Run value belongs to the user who started Setup, not to the administrator account
  that may have approved the UAC prompt, so reg.exe runs as that user. Its data is exactly
  what the app's settings write ("<exe>" run, see shoutabout.winstartup), so the settings
  window shows it as on. }
procedure EnableStartAtSignIn();
var
  ResultCode: Integer;
  Params: String;
begin
  Params := 'add "HKCU\' + RunKey + '" /v {#RunValue} /t REG_SZ /d "\"' +
    ExpandConstant('{app}\{#AppExe}') + '\" run" /f';
  if ExecAsOriginalUser(ExpandConstant('{sys}\reg.exe'), Params, '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) then
    Log(Format('Start at sign-in: exit code %d', [ResultCode]))
  else
    Log('Start at sign-in: could not run reg.exe');
end;

procedure DisableStartAtSignIn();
begin
  if RegValueExists(HKCU, RunKey, '{#RunValue}') then
    RegDeleteValue(HKCU, RunKey, '{#RunValue}');
  if RegValueExists(HKCU, StartupApprovedKey, '{#RunValue}') then
    RegDeleteValue(HKCU, StartupApprovedKey, '{#RunValue}');
end;

{ Windows Settings runs UninstallString. With /SILENT the uninstaller skips its generic
  "Are you sure?" box, so the user sees only our own dialog (InitializeUninstall), which
  asks the one question that matters. Scripted uninstalls use /VERYSILENT and see nothing. }
procedure UseOwnUninstallDialog();
begin
  RegWriteStringValue(HKLM, UninstallKey, 'UninstallString',
    '"' + ExpandConstant('{uninstallexe}') + '" /SILENT');
end;

{ ---------------------------------------------------------------- setup }

function InitializeSetup(): Boolean;
begin
  IsUpgrade := RegKeyExists(HKLM, UninstallKey);
  Result := True;
end;

procedure InitializeWizard();
begin
  if IsUpgrade then begin
    WizardForm.WelcomeLabel1.Caption := 'Update {#AppName}';
    WizardForm.WelcomeLabel2.Caption :=
      'Version {#AppVersion}. Your settings and words are kept.';
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { Start-at-sign-in is offered on the first install only; later the app's settings own it. }
  Result := IsUpgrade and (PageID = wpSelectTasks);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  { The Ready page is off, so the last page before installing says Install, not Next. }
  if (CurPageID = wpSelectTasks) or (IsUpgrade and (CurPageID = wpWelcome)) then
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  AppWasRunning := AppIsRunning();
  StopApp();
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    AddFirewallRule();
    if (not IsUpgrade) and WizardIsTaskSelected('autostart') then
      EnableStartAtSignIn();
    UseOwnUninstallDialog();
  end;
end;

function RelaunchAfterSilentUpgrade(): Boolean;
begin
  Result := WizardSilent and AppWasRunning;
end;

{ ---------------------------------------------------------------- uninstall }

function AskUninstallOptions(var RemoveData: Boolean): Boolean;
var
  Form: TSetupForm;
  Info: TNewStaticText;
  Box: TNewCheckBox;
  OkButton, CancelButton: TNewButton;
begin
#if Ver >= EncodeVer(6, 6, 0)
  Form := CreateCustomForm(ScaleX(420), ScaleY(130), False, False);
#else
  Form := CreateCustomForm;
  Form.ClientWidth := ScaleX(420);
  Form.ClientHeight := ScaleY(130);
#endif
  try
    Form.Caption := 'Uninstall {#AppName}';
    Form.Position := poScreenCenter;

    Info := TNewStaticText.Create(Form);
    Info.Parent := Form;
    Info.Left := ScaleX(20);
    Info.Top := ScaleY(18);
    Info.Caption := '{#AppName} will be removed from this PC.';

    Box := TNewCheckBox.Create(Form);
    Box.Parent := Form;
    Box.Left := ScaleX(20);
    Box.Top := ScaleY(48);
    Box.Width := Form.ClientWidth - ScaleX(40);
    Box.Height := ScaleY(20);
    Box.Caption := 'Also remove my settings and words';
    Box.Checked := False;

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Width := ScaleX(96);
    OkButton.Height := ScaleY(26);
    OkButton.Left := Form.ClientWidth - ScaleX(20 + 96 + 10 + 96);
    OkButton.Top := Form.ClientHeight - ScaleY(26 + 16);
    OkButton.Caption := 'Uninstall';
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Width := ScaleX(96);
    CancelButton.Height := ScaleY(26);
    CancelButton.Left := Form.ClientWidth - ScaleX(20 + 96);
    CancelButton.Top := OkButton.Top;
    CancelButton.Caption := 'Cancel';
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;

    Form.ActiveControl := OkButton;
    Result := Form.ShowModal() = mrOk;
    RemoveData := Box.Checked;
  finally
    Form.Free();
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  RemoveUserData := HasParam('/REMOVEUSERDATA');
  if not HasParam('/VERYSILENT') then
    Result := AskUninstallOptions(RemoveUserData);
  if Result then
    Log(Format('Uninstall: remove user data = %d', [Ord(RemoveUserData)]));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then begin
    StopApp();
    RemoveFirewallRule();
    DisableStartAtSignIn();
  end else if (CurUninstallStep = usPostUninstall) and RemoveUserData then begin
    Log('Removing settings and words');
    DelTree(ExpandConstant('{userappdata}\{#DataFolder}'), True, True, True);
    DelTree(ExpandConstant('{localappdata}\{#DataFolder}'), True, True, True);
  end;
end;
