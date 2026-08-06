; Inno Setup script for Notula on Windows.
;
;   1. .venv\Scripts\pyinstaller tools\notula_win.spec     -> dist\Notula\
;   2. iscc tools\installer.iss                            -> dist\Notula-Setup-2.0.0-beta1.exe
;
; Or just run tools\build_windows.ps1, which does both.
;
; Design notes:
;
;  * Per-user install (PrivilegesRequired=lowest). Notula needs no machine-wide
;    state, and a per-user install means no UAC prompt - which also means the
;    models it later downloads into %LOCALAPPDATA% belong to the same user that
;    installed it, rather than being invisible to them.
;
;  * The installer deliberately does NOT bundle ffmpeg, whisper.cpp, or the
;    models. They are 3+ GB, they update independently, and which whisper build
;    you want depends on whether the machine has an NVIDIA GPU. The app fetches
;    them itself on first run - see deps.py - so setup ends inside Notula rather
;    than in a console window.
;
;  * File types are registered as an "Open with" candidate, not as the default
;    handler. Silently becoming the default application for every .mp3 on the
;    machine is not a thing a meeting recorder should do.

#define AppName        "Notula"
; build_windows.ps1 passes the real version with /DAppVersion=..., read from
; version.py so the DMG and the installer can never disagree. The fallback keeps
; a bare `iscc tools\installer.iss` working.
#ifndef AppVersion
  #define AppVersion   "2.0.0-beta1"
#endif
#define AppPublisher   "Virtue Digital Indonesia"
#define AppExeName     "Notula.exe"
#define SourceDir      "..\dist\Notula"

[Setup]
AppId={{8F3A1C42-7B6E-4D19-9C25-6E1A0B7D4F83}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=..\assets\Notula.ico
; Windows 10 1903 - the WebView2 and WASAPI-loopback baseline
MinVersion=10.0.18362

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "openwith";   Description: "Offer {#AppName} in the ""Open with"" menu for audio and video files"; GroupDescription: "File types:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; shipped alongside so the finish-page checkbox (and later re-runs) can find it
Source: "setup_windows.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\docs\windows.md"; DestDir: "{app}\docs"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"
Name: "{group}\Windows setup guide";  Filename: "{app}\docs\windows.md"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; "Open with" candidate only - no default-handler hijacking. Notula takes a file
; path on its command line, which is exactly what this invokes.
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".wav";  ValueData: ""; Flags: uninsdeletekey; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mp3";  ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".m4a";  ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mp4";  ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mov";  ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mkv";  ValueData: ""; Tasks: openwith
Root: HKCU; Subkey: "Software\Classes\Applications\{#AppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".flac"; ValueData: ""; Tasks: openwith

[Run]
; Just launch the app. It notices what's missing and offers to download it from
; its own window, with a progress bar and a Stop button - so there is no reason
; to throw a PowerShell console at someone who has just finished an installer.
; (setup_windows.ps1 still ships, for setting a machine up without the app.)
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; \
    Flags: postinstall nowait skipifsilent

[UninstallDelete]
; the app's own cache; models and settings are left alone deliberately, so a
; reinstall doesn't mean re-downloading 3 GB
Type: filesandordirs; Name: "{localappdata}\Notula\cache"

[Code]
const
  WEBVIEW2_CLIENT = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_BOOTSTRAP = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

var
  DownloadPage: TDownloadWizardPage;

function WebView2Installed: Boolean;
var
  V: String;
begin
  { The Evergreen runtime records its version under EdgeUpdate\Clients. Check
    both hives: a machine-wide install lands in HKLM, a per-user one in HKCU. }
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT, 'pv', V) or
     RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT, 'pv', V) or
     RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2_CLIENT, 'pv', V))
    and (V <> '') and (V <> '0.0.0.0');
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(
    SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and not WebView2Installed then
  begin
    { Notula's whole UI is a WebView2 control - without the runtime the window
      never opens, so fetch the ~2 MB evergreen bootstrapper now rather than
      letting the app fail on first launch. }
    DownloadPage.Clear;
    DownloadPage.Add(WEBVIEW2_BOOTSTRAP, 'MicrosoftEdgeWebview2Setup.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        SuppressibleMsgBox(
          'The Microsoft Edge WebView2 Runtime could not be downloaded.'#13#10#13#10 +
          'Notula will install, but will not open a window until you install it from:'#13#10 +
          'https://developer.microsoft.com/microsoft-edge/webview2/',
          mbInformation, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Installer: String;
begin
  if CurStep = ssPostInstall then
  begin
    Installer := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
    if FileExists(Installer) and not WebView2Installed then
      Exec(Installer, '/silent /install', '', SW_SHOW, ewWaitUntilTerminated, Code);
  end;
end;
