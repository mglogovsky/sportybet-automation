; Inno Setup installer for FeedWire - Sporty Bet (Windows).
; Builds dist\FeedWire-SportyBet-Setup-<version>.exe from the PyInstaller onedir output.
;
; Why this exists: clients kept double-clicking the exe from inside the raw
; zip (Windows temp-extracts only the exe, then "Failed to load python312.dll").
; An installer means clients never see a zip at all.
;
; Dependencies handled automatically at install time (see [Code] below):
;   1. Microsoft Edge WebView2 Runtime - REQUIRED by the pywebview window;
;      missing runtime = "Failed to resolve Python.Runtime.Loader.Initialize"
;      crash. Downloaded (evergreen bootstrapper) and silently installed.
;   2. Visual C++ 2015-2022 x64 Redistributable - needed by python312.dll and
;      other native binaries. Downloaded and installed (UAC prompt if needed).
;   3. .NET Framework 4.8 - needed by pywebview's winforms fallback. Warns
;      with a download link if missing (built into Windows 10 1903+ / 11).
;   4. AdsPower - the app drives it over CDP. Non-blocking reminder if not
;      found; clients install it themselves from adspower.net.
;
; Downloads use Microsoft's evergreen URLs, so clients always get the current
; runtime. Install therefore needs an internet connection on first setup.
; Requires Inno Setup 6.1+ (for DownloadTemporaryFile).
;
; Build (on Windows):
;   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
; (the script finds ISCC.exe automatically and compiles this file.)

#define AppName      "FeedWire - Sporty Bet"
#define AppVersion   "0.1.3"
#define AppPublisher "FeedWire"
#define AppURL       "https://feed-wire.pro"
#define AppExeName   "FeedWire - Sporty Bet.exe"
; Keep this AppId fixed forever - it's how Windows tracks upgrades/uninstalls.
#define AppId        "{{D9BF4927-E9DD-4AB5-821D-E532FB545DBA}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
; Per-user install into %LOCALAPPDATA%\Programs - no admin prompt for the app
; itself. (Dependency installers may still raise UAC when they need it.)
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=FeedWire-SportyBet-Setup-{#AppVersion}
SetupIconFile=FeedWire.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Windows 10+ only: matches WebView2 support and the PyInstaller build.
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Use Restart Manager to close a running instance on upgrade/reinstall
; instead of failing with "file in use".
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
; The whole PyInstaller onedir output, including _internal\ with python312.dll.
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Nothing extra - license key and config live in %APPDATA%\SportyPilot and
; must survive uninstall/reinstall so clients don't have to re-activate.

[Code]
const
  { Microsoft evergreen download URLs - always serve the current runtime. }
  WebView2Url  = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';  { x64 bootstrapper }
  VCRedistUrl  = 'https://aka.ms/vs/17/release/vc_redist.x64.exe';
  NetFxUrl     = 'https://dotnet.microsoft.com/download/dotnet-framework/net48';
  AdsPowerUrl  = 'https://www.adspower.net/';
  { WebView2 client GUID used by EdgeUpdate registry entries. }
  WebView2ClientKey = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3E9A7E4C5}';
  WebView2ClientKeyWow64 = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3E9A7E4C5}';

function IsWebView2Installed: Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, WebView2ClientKeyWow64, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, WebView2ClientKey, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, WebView2ClientKey, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

function IsVCRedistInstalled: Boolean;
var
  Installed: Cardinal;
begin
  Result :=
    (RegQueryDWordValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1)) or
    (RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1));
end;

function IsNetFx48Installed: Boolean;
var
  Release: Cardinal;
begin
  { .NET Framework 4.8 = Release 528040 or higher. }
  Result := RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release) and (Release >= 528040);
end;

{ Best-effort detection only: AdsPower install locations vary, so a miss here
  just produces an informational reminder - never a blocked install. }
function IsAdsPowerInstalled: Boolean;
begin
  Result :=
    DirExists(ExpandConstant('{localappdata}\Programs\AdsPower')) or
    DirExists(GetEnv('ProgramFiles') + '\AdsPower') or
    DirExists(GetEnv('ProgramFiles(x86)') + '\AdsPower');
end;

{ Downloads Url into the tmp folder as BaseName (Inno 6.1+ built-in) and runs it.
  Returns True when the installer exits with a success/reboot code.
  NeedsAdmin runs it via the "runas" verb so the user gets a UAC prompt even
  though our own installer is per-user. }
function DownloadAndRun(const Url, BaseName, Params, DisplayName: String; const NeedsAdmin: Boolean): Boolean;
var
  TempFile: String;
  ResultCode: Integer;
begin
  Result := False;
  WizardForm.StatusLabel.Caption := 'Downloading ' + DisplayName + '...';
  try
    DownloadTemporaryFile(Url, BaseName, '', nil);
  except
    MsgBox('Could not download ' + DisplayName + '.'#13#10#13#10 +
           'The app may not start without it. You can install it manually from:'#13#10 +
           Url, mbError, MB_OK);
    Exit;
  end;

  TempFile := ExpandConstant('{tmp}\' + BaseName);
  WizardForm.StatusLabel.Caption := 'Installing ' + DisplayName + '...';
  if NeedsAdmin then
  begin
    if not ShellExec('runas', TempFile, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    begin
      MsgBox('Could not start the ' + DisplayName + ' installer (was the admin prompt declined?).'#13#10 +
             'You can install it manually from:'#13#10 + Url, mbError, MB_OK);
      Exit;
    end;
  end
  else if not Exec(TempFile, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    MsgBox('Could not run the ' + DisplayName + ' installer.'#13#10 +
           'You can install it manually from:'#13#10 + Url, mbError, MB_OK);
    Exit;
  end;

  { 0 = success, 3010 = success + reboot required, 1638/1602 variants treated as failures }
  Result := (ResultCode = 0) or (ResultCode = 3010);
  if not Result then
    Log(DisplayName + ' installer exited with code ' + IntToStr(ResultCode));
end;

procedure InstallDependencies;
begin
  { 1. WebView2 Runtime - hard requirement for the app window.
       The bootstrapper installs per-user when not elevated, so no UAC here. }
  if IsWebView2Installed then
    Log('WebView2 Runtime already present')
  else if DownloadAndRun(WebView2Url, 'MicrosoftEdgeWebview2Setup.exe', '/silent /install', 'Microsoft Edge WebView2 Runtime', False) then
    Log('WebView2 Runtime installed')
  else
    MsgBox('WebView2 Runtime installation did not complete.'#13#10 +
           'The app window will not open without it. Install it manually from:'#13#10 +
           'https://developer.microsoft.com/microsoft-edge/webview2/', mbError, MB_OK);

  { 2. VC++ 2015-2022 x64 runtime - needed by python312.dll & native deps.
       vc_redist requires elevation -> runas verb (single UAC prompt). }
  if IsVCRedistInstalled then
    Log('Visual C++ Runtime already present')
  else if DownloadAndRun(VCRedistUrl, 'vc_redist.x64.exe', '/install /quiet /norestart', 'Microsoft Visual C++ Redistributable', True) then
    Log('Visual C++ Runtime installed');

  { 3. .NET Framework 4.8 - only used by pywebview's winforms fallback.
       Cannot be installed silently from a per-user installer (needs admin +
       often a reboot), and it ships with Windows 10 1903+/11 - so just warn. }
  if not IsNetFx48Installed then
    MsgBox('.NET Framework 4.8 was not detected. The app usually does not need it' #13#10 +
           '(it is only a fallback display engine), but if the app fails to open' #13#10 +
           'after installation, install it from:'#13#10 + NetFxUrl, mbInformation, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    InstallDependencies;

  if CurStep = ssPostInstall then
  begin
    { 4. AdsPower - third-party app the app drives over CDP. Non-blocking. }
    if not IsAdsPowerInstalled then
      MsgBox('Reminder: this app controls the AdsPower browser, which was not' #13#10 +
             'detected on this PC. Install AdsPower and keep it running before' #13#10 +
             'starting the app:'#13#10 + AdsPowerUrl, mbInformation, MB_OK);
  end;
end;
