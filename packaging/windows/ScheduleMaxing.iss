; packaging/windows/ScheduleMaxing.iss -- the Windows installer of Schedule Maxing (Inno Setup 6).
;
; Built by packaging\windows\build_windows.ps1, which passes the version read from app\version.py:
;
;     ISCC /DAppVersion=1.2.3 packaging\windows\ScheduleMaxing.iss
;     -> dist\installer\ScheduleMaxing-Setup-1.2.3.exe
;
; USER DATA IS NEVER TOUCHED. The application keeps everything a person creates (the SQLite database,
; settings, the trained model, logs and backups) in %LOCALAPPDATA%\ScheduleMaxing, and its saved sign-in
; in Windows Credential Manager. This installer writes only to its own install folder: installing,
; upgrading and uninstalling leave that data exactly as it was. Nothing below may reference the user's
; application-data folders. Removing the data is a separate, manual step (docs/deployment/windows-distribution.md).
;
; Silent use (also what the in-app updater runs):
;     ScheduleMaxing-Setup-1.2.3.exe /SILENT /NORESTART /RELAUNCH=1
; installs over the existing version (waiting for the running application to close) and, with
; /RELAUNCH=1, starts the new version afterwards. /VERYSILENT hides the progress window as well.

#ifndef AppVersion
  #error AppVersion is required: pass /DAppVersion to ISCC (packaging\windows\build_windows.ps1 does)
#endif
#ifndef AppPublisher
  #define AppPublisher "Schedule Maxing"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\ScheduleMaxing"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\installer"
#endif

#define AppName "Schedule Maxing"
#define AppExeName "ScheduleMaxing.exe"
; The mutex the running application holds (INSTANCE_MUTEX_NAME in app\desktop.py).
#define AppMutexName "ScheduleMaxing_SingleInstance"

[Setup]
; Identifies the application to Windows across versions: upgrades and the Add/Remove Programs entry
; depend on it. NEVER change it.
AppId={{14235D14-9AA2-440C-A227-D8AC414886C7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; 64-bit Windows 10 and 11 only.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; For everyone on the computer (Program Files, one UAC prompt) by default; the first page also offers
; "only for me", which needs no administrator rights (/CURRENTUSER on the command line).
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
DefaultDirName={autopf}\{#AppName}
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
DisableDirPage=auto
; Files in use by a running copy are released by closing it (see PrepareToInstall below); never restart Windows.
CloseApplications=yes
RestartApplications=no
SetupIconFile=..\..\assets\ScheduleMaxing.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=ScheduleMaxing-Setup-{#AppVersion}
#ifdef SignedBuild
; Signs the installer and the uninstaller it contains (the "smsign" tool is defined on ISCC's command line).
SignTool=smsign
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the program's own files completely, so nothing a newer build no longer ships lingers.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; The "Launch Schedule Maxing" checkbox on the last page of an interactive install.
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; After a silent update started by the application itself (/RELAUNCH=1): start the new version, as the user.
Filename: "{app}\{#AppExeName}"; Flags: nowait runasoriginaluser; Check: RelaunchRequested

[Code]
function RelaunchRequested: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

var
  SawProgram: Boolean;

function AppIsRunning: Boolean;
begin
  Result := CheckForMutexes('{#AppMutexName}');
end;

{ Every process running from the install folder: the application's window, and also its schedule-engine
  worker (app\planning\engine_process.py), which uses the same program files but holds no mutex. With
  EndThem, they are ended. Returns how many were found; 0 when Windows cannot list them. }
function ProgramProcesses(EndThem: Boolean): Integer;
var
  Locator, Service, Processes, Process: Variant;
  Folder, Path: String;
  I: Integer;
begin
  Result := 0;
  Folder := Lowercase(AddBackslash(ExpandConstant('{app}')));
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Processes := Service.ExecQuery('SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE Name = ''{#AppExeName}''');
    for I := 0 to Processes.Count - 1 do
    begin
      Process := Processes.ItemIndex(I);
      if not VarIsNull(Process.ExecutablePath) then
      begin
        Path := Process.ExecutablePath;
        Path := Lowercase(Path);
        if Pos(Folder, Path) = 1 then
        begin
          Result := Result + 1;
          SawProgram := True;
          if EndThem then
          begin
            Log('Ending ' + Path + ', which is still running from the install folder.');
            Process.Terminate(0);
          end;
        end;
      end;
    end;
  except
    Log('The running processes could not be listed: ' + GetExceptionMessage);
  end;
end;

function ProgramIsRunning: Boolean;
begin
  Result := AppIsRunning or (ProgramProcesses(False) > 0);
end;

{ Waits up to Steps half-seconds for every program process to end. True if none is left. }
function WaitForProgramToEnd(Steps: Integer): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  while ProgramIsRunning and (Waited < Steps) do
  begin
    Sleep(500);
    Waited := Waited + 1;
  end;
  Result := not ProgramIsRunning;
end;

{ Asks the user to close a running window. False if they cancel. }
function AskUntilAppIsClosed: Boolean;
begin
  Result := True;
  while AppIsRunning do
  begin
    if SuppressibleMsgBox('{#AppName} is still running.' + #13#10 + #13#10 +
                          'Close it, then click OK to continue.', mbError, MB_OKCANCEL, IDCANCEL) <> IDOK then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

{ What is left after the window has closed has no window to close: an engine worker still ending, or one a
  copy that crashed left behind. Give it a moment, then end it. True if nothing from the folder still runs. }
function EndLeftoverProcesses: Boolean;
begin
  if not WaitForProgramToEnd(6) then
  begin
    ProgramProcesses(True);
    WaitForProgramToEnd(20);
  end;
  Result := not ProgramIsRunning;
  if Result and SawProgram then
    Sleep(1000);  { Windows releases an ended program's files a moment after it ends }
end;

{ The application's files cannot be replaced while any of its processes runs. An update started from inside
  the application arrives here while that copy is closing (it closes within ~15 seconds, CLOSE_DEADLINE_S in
  app\app.py), so wait for it, and end it if it does not close: the person chose to install. Otherwise ask the
  user to close the window. Either way, nothing of the old version is left running when files are replaced. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  SawProgram := False;
  if RelaunchRequested and not WaitForProgramToEnd(120) then
  begin
    Log('{#AppName} did not close within 60 seconds after starting the update; ending it.');
    ProgramProcesses(True);
    WaitForProgramToEnd(20);
  end;
  if not AskUntilAppIsClosed then
  begin
    Result := '{#AppName} is still running, so it was not updated. Nothing was changed.';
    Exit;
  end;
  if not EndLeftoverProcesses then
    Result := 'Part of {#AppName} is still running and could not be ended, so it was not updated. ' +
              'Nothing was changed. Restart Windows, then run this installer again.';
end;

function InitializeUninstall: Boolean;
begin
  SawProgram := False;
  Result := AskUntilAppIsClosed;
  if Result and not EndLeftoverProcesses then
  begin
    SuppressibleMsgBox('Part of {#AppName} is still running and could not be ended. ' +
                       'Restart Windows, then uninstall again.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;
