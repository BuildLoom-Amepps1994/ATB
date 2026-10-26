; Editable per-user Windows installation. Trading and state are never started by Setup.
#ifndef StageDir
  #define StageDir "..\dist\editable"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif
#ifndef AppVersion
  #define AppVersion "1.5.0"
#endif

[Setup]
AppId=CryptoPilot.Editable.Windows.v1
AppName=CryptoPilot
AppVersion={#AppVersion}
AppPublisher=CryptoPilot
DefaultDirName={localappdata}\Programs\CryptoPilot
DefaultGroupName=CryptoPilot
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=CryptoPilot-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
AllowNoIcons=yes
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
AlwaysRestart=no
UninstallDisplayIcon={app}\runtime\CryptoPilotRuntime.exe
UninstallDisplayName=CryptoPilot
CreateUninstallRegKey=not TestInstall
UsePreviousAppDir=not TestInstall
UsePreviousGroup=not TestInstall
UsePreviousTasks=not TestInstall
SetupLogging=yes
DisableWelcomePage=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked; Check: not TestInstall

[Files]
Source: "{#StageDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\maintenance\*"; DestDir: "{app}\maintenance"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.pyc,__pycache__"
; Source is moved as a complete directory after validation. Preserve it and backups on uninstall.
Source: "{#StageDir}\app\*"; DestDir: "{code:GetStageRoot}\app"; Flags: ignoreversion recursesubdirs createallsubdirs uninsneveruninstall; Excludes: "*.pyc,__pycache__"
Source: "{#StageDir}\installation.json"; DestDir: "{code:GetStageRoot}"; Flags: ignoreversion uninsneveruninstall
Source: "{#StageDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\CryptoPilot"; Filename: "{app}\runtime\CryptoPilotRuntime.exe"; Parameters: "--app-dir ""{app}\app"""; WorkingDir: "{app}"; Check: not TestInstall
Name: "{group}\CryptoPilot editable files"; Filename: "{app}\app"; Check: not TestInstall
Name: "{group}\Uninstall CryptoPilot"; Filename: "{uninstallexe}"; Check: not TestInstall
Name: "{autodesktop}\CryptoPilot"; Filename: "{app}\runtime\CryptoPilotRuntime.exe"; Parameters: "--app-dir ""{app}\app"""; WorkingDir: "{app}"; Tasks: desktopicon; Check: not TestInstall

[Code]
const
  GenericReadWrite = $C0000000;
  ShareReadWrite = 3;
  OpenAlways = 4;
  AttributeNormal = $80;
  AttributeReparsePoint = $400;
  ReplaceExistingWriteThrough = 9;

var
  InstallLock: THandle;
  HasInstallLock: Boolean;
  StageToken: String;
  BackupRoot: String;

function CreateFileW(FileName: String; Access, ShareMode: Cardinal;
  Security: Integer; Disposition, Attributes: Cardinal; Template: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function LockFile(Handle: THandle; OffsetLow, OffsetHigh, LengthLow, LengthHigh: Cardinal): Boolean;
  external 'LockFile@kernel32.dll stdcall';
function UnlockFile(Handle: THandle; OffsetLow, OffsetHigh, LengthLow, LengthHigh: Cardinal): Boolean;
  external 'UnlockFile@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function GetFileAttributesW(FileName: String): Cardinal;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function MoveFileExW(ExistingName, NewName: String; Flags: Cardinal): Boolean;
  external 'MoveFileExW@kernel32.dll stdcall';

function TestInstall: Boolean;
begin
  { This switch tests the final installer without modifying real shortcuts or registration. }
  Result := ExpandConstant('{param:TESTINSTALL|0}') = '1';
end;

function WaitLockSeconds: Integer;
begin
  { The in-app updater starts Setup, then exits; Setup waits for it to release the lock. }
  Result := StrToIntDef(ExpandConstant('{param:WAITLOCK|0}'), 0);
  if Result < 0 then Result := 0;
  if Result > 600 then Result := 600;
end;

function RelaunchAfterSetup: Boolean;
begin
  Result := (ExpandConstant('{param:RELAUNCH|0}') = '1') and not TestInstall;
end;

function GetStageRoot(Param: String): String;
begin
  Result := AddBackslash(ExpandConstant('{app}')) + '.setup-' + StageToken;
end;

function SafeTree(const Folder: String): Boolean;
var
  Entry: TFindRec;
  Path: String;
begin
  Result := True;
  if not DirExists(Folder) then exit;
  if (GetFileAttributesW(Folder) and AttributeReparsePoint) <> 0 then begin
    Result := False;
    exit;
  end;
  if FindFirst(AddBackslash(Folder) + '*', Entry) then begin
    try
      repeat
        if (Entry.Name <> '.') and (Entry.Name <> '..') then begin
          Path := AddBackslash(Folder) + Entry.Name;
          if (Entry.Attributes and AttributeReparsePoint) <> 0 then begin
            Result := False;
            exit;
          end;
          if (Entry.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
            if not SafeTree(Path) then begin
              Result := False;
              exit;
            end;
        end;
      until not FindNext(Entry);
    finally
      FindClose(Entry);
    end;
  end;
end;

function AcquireInstallLock: Boolean;
var
  Folder: String;
begin
  if HasInstallLock then begin Result := True; exit; end;
  Folder := ExpandConstant('{app}');
  Result := ForceDirectories(Folder);
  if not Result then exit;
  InstallLock := CreateFileW(AddBackslash(Folder) + 'install.lock', GenericReadWrite,
    ShareReadWrite, 0, OpenAlways, AttributeNormal, 0);
  Result := InstallLock <> THandle(-1);
  if not Result then exit;
  Result := LockFile(InstallLock, 0, 0, 1, 0);
  if Result then HasInstallLock := True
  else CloseHandle(InstallLock);
end;

procedure ReleaseInstallLock;
begin
  if HasInstallLock then begin
    UnlockFile(InstallLock, 0, 0, 1, 0);
    CloseHandle(InstallLock);
    HasInstallLock := False;
  end;
end;

function InitializeSetup: Boolean;
begin
  HasInstallLock := False;
  StageToken := GetDateTimeString('yyyymmddhhnnss', '-', ':');
  Result := True;
end;

function OverlappingPaths(const First, Second: String): Boolean;
var A, B: String;
begin
  A := Lowercase(AddBackslash(ExpandFileName(First)));
  B := Lowercase(AddBackslash(ExpandFileName(Second)));
  Result := (Pos(A, B) = 1) or (Pos(B, A) = 1);
end;

function EmptyDestination(const Folder: String): Boolean;
var Entry: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Folder) + '*', Entry) then begin
    try
      repeat
        if (Entry.Name <> '.') and (Entry.Name <> '..') and (Entry.Name <> 'install.lock') then begin
          Result := False;
          exit;
        end;
      until not FindNext(Entry);
    finally
      FindClose(Entry);
    end;
  end;
end;

function DestinationError: String;
var Folder, Parent, DataPath: String; Attr: Cardinal;
begin
  Result := '';
  Folder := ExpandFileName(ExpandConstant('{app}'));
  if Length(RemoveBackslash(Folder)) <= 3 then begin
    Result := 'Choose a dedicated CryptoPilot application folder.';
    exit;
  end;
  DataPath := ExpandConstant('{localappdata}\CryptoPilot');
  if OverlappingPaths(Folder, DataPath) then begin
    Result := 'The application folder must be separate from CryptoPilot portfolio data.';
    exit;
  end;
  DataPath := GetEnv('CRYPTOPILOT_DATA_DIR');
  if (DataPath <> '') and OverlappingPaths(Folder, DataPath) then begin
    Result := 'The application folder overlaps your configured portfolio data folder.';
    exit;
  end;
  Parent := Folder;
  repeat
    Attr := GetFileAttributesW(Parent);
    if (Attr <> $FFFFFFFF) and ((Attr and AttributeReparsePoint) <> 0) then begin
      Result := 'Choose a regular folder without symbolic links or junctions in its path.';
      exit;
    end;
    Parent := ExtractFileDir(Parent);
    if (Parent = Folder) or (Parent = '') then break;
    Folder := Parent;
  until False;
  Folder := ExpandFileName(ExpandConstant('{app}'));
  if DirExists(Folder) and (not FileExists(Folder + '\installation.json')) and (not EmptyDestination(Folder)) then
    Result := 'Choose an empty folder or an existing editable CryptoPilot installation. Other files will not be overwritten.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Suffix, Waited: Integer;
begin
  Result := DestinationError;
  if Result <> '' then exit;
  if not SafeTree(ExpandConstant('{app}')) then begin
    Result := 'The installation folder contains a symbolic link or junction. Choose a regular local folder.';
    exit;
  end;
  Waited := 0;
  while (not AcquireInstallLock) and (Waited < WaitLockSeconds * 2) do begin
    Sleep(500);
    Waited := Waited + 1;
  end;
  { Give the exiting runtime a moment to unmap its executable before files are replaced. }
  if HasInstallLock and (Waited > 0) then Sleep(3000);
  if not HasInstallLock then begin
    Result := 'CryptoPilot or an update is running. Stop trading, close its console, and try again. Setup will not stop it for you.';
    exit;
  end;
  if FileExists(ExpandConstant('{app}\update-transaction.json')) then begin
    Result := 'A previous update needs recovery. Start CryptoPilot once to recover it, close it, then run Setup again.';
    ReleaseInstallLock;
    exit;
  end;
  Suffix := 0;
  while DirExists(GetStageRoot('')) or DirExists(ExpandConstant('{app}\backups\setup-') + StageToken) do begin
    Suffix := Suffix + 1;
    StageToken := GetDateTimeString('yyyymmddhhnnss', '-', ':') + '-' + IntToStr(Suffix);
  end;
  BackupRoot := ExpandConstant('{app}\backups\setup-') + StageToken;
end;

procedure RequireSuccess(Success: Boolean; const Message: String);
begin
  if not Success then RaiseException(Message);
end;

procedure RollBackApp(const HadApp: Boolean);
var
  Root: String;
begin
  Root := ExpandConstant('{app}');
  if HadApp and DirExists(BackupRoot + '\app') then begin
    if DirExists(Root + '\app') then
      RequireSuccess(RenameFile(Root + '\app', GetStageRoot('') + '\app'),
        'Could not preserve the interrupted source. Recovery will run at next startup.');
    RequireSuccess(RenameFile(BackupRoot + '\app', Root + '\app'),
      'Could not restore the previous source. Recovery will run at next startup.');
  end else if (not HadApp) and DirExists(Root + '\app') then
    RequireSuccess(RenameFile(Root + '\app', GetStageRoot('') + '\app'),
      'Could not restore the staged source.');
  if HadApp and FileExists(BackupRoot + '\installation.json') then
    RequireSuccess(FileCopy(BackupRoot + '\installation.json', Root + '\installation.json', False),
      'Could not restore the previous installation record.');
  if HadApp then DeleteFile(Root + '\update-transaction.json');
end;

procedure ActivateSource;
var
  Root, Journal: String;
  ExitCode: Integer;
  HadApp: Boolean;
begin
  Root := ExpandConstant('{app}');
  RequireSuccess(SafeTree(GetStageRoot('')), 'The staged application contains a symbolic link or junction.');
  RequireSuccess(Exec(Root + '\runtime\CryptoPilotRuntime.exe',
    '--check-app "' + GetStageRoot('') + '\app"', Root, SW_HIDE, ewWaitUntilTerminated, ExitCode),
    'Could not validate the installed runtime.');
  RequireSuccess(ExitCode = 0, 'The new application failed offline validation. Existing source was preserved.');
  HadApp := DirExists(Root + '\app');
  if HadApp then begin
    RequireSuccess(FileExists(Root + '\installation.json'),
      'This folder contains app files without a valid installation record. Choose an empty folder.');
    RequireSuccess(ForceDirectories(BackupRoot), 'Could not create the source backup folder.');
    RequireSuccess(FileCopy(Root + '\installation.json', BackupRoot + '\installation.json', True),
      'Could not back up the installation record.');
    Journal := '{"format":1,"product":"CryptoPilot","backup":"backups/setup-' + StageToken +
      '","staging":".setup-' + StageToken + '"}';
    RequireSuccess(SaveStringToFile(Root + '\update-transaction.json', Journal, False),
      'Could not create the recovery record.');
  end;
  try
    if HadApp then
      RequireSuccess(RenameFile(Root + '\app', BackupRoot + '\app'), 'Could not back up the previous source.');
    RequireSuccess(RenameFile(GetStageRoot('') + '\app', Root + '\app'), 'Could not activate the new source.');
    RequireSuccess(MoveFileExW(GetStageRoot('') + '\installation.json', Root + '\installation.json',
      ReplaceExistingWriteThrough), 'Could not activate the installation record.');
    if HadApp then
      RequireSuccess(DeleteFile(Root + '\update-transaction.json'), 'Could not finish the update transaction.');
  except
    RollBackApp(HadApp);
    RaiseException(GetExceptionMessage);
  end;
  RemoveDir(GetStageRoot(''));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then ActivateSource;
end;

procedure DeinitializeSetup;
var
  Runtime: String;
  ExitCode: Integer;
begin
  ReleaseInstallLock;
  { Reopen CryptoPilot after an in-app update, whether or not Setup succeeded. It never starts trading. }
  if RelaunchAfterSetup then
    try
      Runtime := ExpandConstant('{app}\runtime\CryptoPilotRuntime.exe');
      if FileExists(Runtime) then
        Exec(Runtime, '--app-dir "' + ExpandConstant('{app}\app') + '"', ExpandConstant('{app}'),
          SW_SHOWNORMAL, ewNoWait, ExitCode);
    except
    end;
end;

function InitializeUninstall: Boolean;
begin
  HasInstallLock := False;
  Result := SafeTree(ExpandConstant('{app}')) and AcquireInstallLock;
  if not Result then
    SuppressibleMsgBox('Close CryptoPilot and its updater before uninstalling. Its files and user data were preserved.',
      mbError, MB_OK, IDOK);
end;

procedure DeinitializeUninstall;
begin
  ReleaseInstallLock;
end;
