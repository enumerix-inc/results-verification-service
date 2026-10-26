; ---------------------------------------------------------------------------
; Countable Regression Verification Service - Inno Setup script
;
; Build with installer\build_payload.ps1 (assembles payload\ then runs ISCC).
; Requires Inno Setup 6.3 or later.
;
; Layout on the target machine:
;   {app}                  C:\Program Files\Countable Labs\Regression Service
;     python\  service\  tools\          replaced on every upgrade
;   {commonappdata}\Countable Labs\Regression Service
;     config\local_config.yaml           per-machine, written once, never overwritten
;     data\verification.db               SQLite database (default location)
;     logs\                              service stdout/stderr, install logs
;
; Silent install:
;   Setup.exe /VERYSILENT /DBUSER=svc /DBPASSWORD=... [/DBHOST=localhost]
;     [/DBPORT=5432] [/DBNAME=countablelabs] [/MANIFESTSDIR=F:\RegressionTesting\manifests]
;     [/REPORTSDIR=...] [/RESULTSDIR=...] [/EREPORTDATA=...] [/DBFILE=...]
; ---------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define ServiceName "ResultsVerificationService"
#define DataRoot "{commonappdata}\Countable Labs\Regression Service"

[Setup]
AppId={{D5447480-CB16-413F-92CE-2C223FE8F5EF}
AppName=Countable Regression Verification Service
AppVersion={#AppVersion}
AppPublisher=Countable Labs
DefaultDirName={autopf}\Countable Labs\Regression Service
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=RegressionVerificationService-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=Countable Regression Verification Service
; The service holds python\ files open; this installer stops it first.
CloseApplications=no

[Dirs]
Name: "{#DataRoot}\config"
Name: "{#DataRoot}\data"
Name: "{#DataRoot}\logs"

[Files]
Source: "payload\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "payload\service\*"; DestDir: "{app}\service"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "payload\tools\nssm.exe"; DestDir: "{app}\tools"; Flags: ignoreversion

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\service\scripts\uninstall_verification_service.ps1"" -NssmPath ""{app}\tools\nssm.exe"""; \
  Flags: runhidden; RunOnceId: "RemoveService"

[UninstallDelete]
; Files created after install (bytecode caches, pip leftovers).
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\service"

[Code]
var
  DbPage: TInputQueryWizardPage;
  PathsPage: TInputQueryWizardPage;

function DataDir: String;
begin
  Result := ExpandConstant('{#DataRoot}');
end;

function ConfigDir: String;
begin
  Result := DataDir + '\config';
end;

function ConfigFile: String;
begin
  Result := ConfigDir + '\local_config.yaml';
end;

function PythonExe: String;
begin
  Result := ExpandConstant('{app}\python\python.exe');
end;

function ServiceDir: String;
begin
  Result := ExpandConstant('{app}\service');
end;

function JsonEscape(const S: String): String;
begin
  Result := S;
  StringChangeEx(Result, '\', '\\', True);
  StringChangeEx(Result, '"', '\"', True);
end;

function ServiceExists: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query {#ServiceName}', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure InitializeWizard;
begin
  DbPage := CreateInputQueryPage(wpSelectDir,
    'Pipeline database',
    'Connection used to receive report notifications',
    'Enter the Postgres connection details for this machine''s Countable pipeline database. ' +
    'The password is stored only in the per-machine config file, readable by Administrators and SYSTEM.');
  DbPage.Add('Host:', False);
  DbPage.Add('Port:', False);
  DbPage.Add('Database:', False);
  DbPage.Add('User:', False);
  DbPage.Add('Password:', True);
  DbPage.Values[0] := ExpandConstant('{param:DBHOST|localhost}');
  DbPage.Values[1] := ExpandConstant('{param:DBPORT|5432}');
  DbPage.Values[2] := ExpandConstant('{param:DBNAME|countablelabs}');
  DbPage.Values[3] := ExpandConstant('{param:DBUSER|}');
  DbPage.Values[4] := ExpandConstant('{param:DBPASSWORD|}');

  PathsPage := CreateInputQueryPage(DbPage.ID,
    'Folders',
    'Where the service reads and writes regression data',
    'These default to the standard regression-machine layout. The drives do not have to exist yet.');
  PathsPage.Add('Manifests folder:', False);
  PathsPage.Add('Reports folder:', False);
  PathsPage.Add('Results folder (F: archive):', False);
  PathsPage.Add('Report data folder (E:):', False);
  PathsPage.Add('Verification database file:', False);
  PathsPage.Values[0] := ExpandConstant('{param:MANIFESTSDIR|F:\RegressionTesting\manifests}');
  PathsPage.Values[1] := ExpandConstant('{param:REPORTSDIR|F:\RegressionTesting\reports}');
  PathsPage.Values[2] := ExpandConstant('{param:RESULTSDIR|F:\CountableLabs\data}');
  PathsPage.Values[3] := ExpandConstant('{param:EREPORTDATA|E:\CountableLabs\ReportData}');
  PathsPage.Values[4] := ExpandConstant('{param:DBFILE|}');
  if PathsPage.Values[4] = '' then
    PathsPage.Values[4] := DataDir + '\data\verification.db';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { An upgrade keeps the existing per-machine config, so do not ask again. }
  Result := ((PageID = DbPage.ID) or (PageID = PathsPage.ID)) and FileExists(ConfigFile);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = DbPage.ID then
  begin
    if (Trim(DbPage.Values[0]) = '') or (Trim(DbPage.Values[1]) = '') or
       (Trim(DbPage.Values[2]) = '') or (Trim(DbPage.Values[3]) = '') or
       (DbPage.Values[4] = '') then
    begin
      MsgBox('Please fill in every database field.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  { Stop the service so python\ files are not locked. net stop waits for the
    service's own stop timeout (NSSM allows up to 60 s for in-flight work). }
  if ServiceExists then
    Exec(ExpandConstant('{sys}\net.exe'), 'stop {#ServiceName}', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
end;

procedure WriteConfigIfMissing;
var
  Answers, Json: String;
  ResultCode: Integer;
begin
  if FileExists(ConfigFile) then
    Exit;

  Answers := ExpandConstant('{tmp}\rvs_answers.json');
  Json := '{' +
    '"db_host":"' + JsonEscape(Trim(DbPage.Values[0])) + '",' +
    '"db_port":"' + JsonEscape(Trim(DbPage.Values[1])) + '",' +
    '"db_name":"' + JsonEscape(Trim(DbPage.Values[2])) + '",' +
    '"db_user":"' + JsonEscape(Trim(DbPage.Values[3])) + '",' +
    '"db_password":"' + JsonEscape(DbPage.Values[4]) + '",' +
    '"manifests_dir":"' + JsonEscape(Trim(PathsPage.Values[0])) + '",' +
    '"reports_dir":"' + JsonEscape(Trim(PathsPage.Values[1])) + '",' +
    '"results_dir":"' + JsonEscape(Trim(PathsPage.Values[2])) + '",' +
    '"e_drive_report_data":"' + JsonEscape(Trim(PathsPage.Values[3])) + '",' +
    '"database":"' + JsonEscape(Trim(PathsPage.Values[4])) + '"' +
    '}';
  if not SaveStringToFile(Answers, Json, False) then
    RaiseException('Could not write the temporary answers file.');

  { write_local_config.py reads the answers, then deletes the file. }
  if not Exec(PythonExe,
      '"' + ServiceDir + '\scripts\write_local_config.py" ' +
      '--defaults "' + ServiceDir + '\config\config.yaml" ' +
      '--answers "' + Answers + '" --out "' + ConfigFile + '"',
      ServiceDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
  begin
    DeleteFile(Answers);
    RaiseException('Could not write ' + ConfigFile + ' (exit code ' + IntToStr(ResultCode) + ').');
  end;

  { Credentials live in this file: SYSTEM and Administrators only. }
  Exec(ExpandConstant('{sys}\icacls.exe'),
    '"' + ConfigFile + '" /inheritance:r /grant:r *S-1-5-18:F *S-1-5-32-544:F',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure RunInit;
var
  ResultCode: Integer;
begin
  if not Exec(PythonExe, '-m src.main --init --config "' + ConfigDir + '"',
      ServiceDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    RaiseException('Initialising the verification database failed (exit code ' +
      IntToStr(ResultCode) + '). Check the Folders page values and that the drive exists.');
end;

function RunPreflight: Boolean;
var
  ResultCode: Integer;
  LogFile: String;
  Output: AnsiString;
begin
  LogFile := DataDir + '\logs\install_preflight.log';
  { cmd /C with a doubled outer quote is the form that survives spaces. }
  Result := Exec(ExpandConstant('{sys}\cmd.exe'),
    '/C ""' + PythonExe + '" -m src.main --preflight --config "' + ConfigDir +
    '" > "' + LogFile + '" 2>&1"',
    ServiceDir, SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  if (not Result) and (not WizardSilent) then
    if LoadStringFromFile(LogFile, Output) then
      MsgBox('The service will be installed but not started, because a preflight check failed:' +
        #13#10#13#10 + String(Output) + #13#10 +
        'Fix the cause, then start the service with: Start-Service {#ServiceName}',
        mbInformation, MB_OK);
end;

procedure RegisterService(StartIt: Boolean);
var
  ResultCode: Integer;
  Params: String;
begin
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ServiceDir +
    '\scripts\install_verification_service.ps1"' +
    ' -NssmPath "' + ExpandConstant('{app}\tools\nssm.exe') + '"' +
    ' -AppDir "' + ServiceDir + '"' +
    ' -PythonExe "' + PythonExe + '"' +
    ' -ConfigDir "' + ConfigDir + '"' +
    ' -LogsDir "' + DataDir + '\logs"';
  if StartIt then
    Params := Params + ' -Start';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, '',
      SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    RaiseException('Registering the Windows service failed (exit code ' +
      IntToStr(ResultCode) + ').');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  PreflightOk: Boolean;
begin
  if CurStep = ssPostInstall then
  begin
    WriteConfigIfMissing;
    RunInit;
    PreflightOk := RunPreflight;
    { Never start against a failing config; register so it can be started later. }
    RegisterService(PreflightOk);
  end;
end;
