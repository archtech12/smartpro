; Inno Setup 6 script for SmartSchool Manager.
; Build with:  iscc /DAppVersion=0.1.0 packaging\installer.iss
; (build_windows.ps1 does this for you after PyInstaller has produced dist\SmartSchoolManager)
;
; DATA SAFETY: the school database lives in %LOCALAPPDATA%\SmartSchoolManagerRebuild and is
; NEVER installed, overwritten or removed by this installer or the uninstaller. Upgrading only
; replaces program files; the app backs up the database before any schema migration.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#define AppName "SmartSchool Manager"
#define AppExe "SmartSchoolManager.exe"

[Setup]
; Never change AppId: it lets a new version upgrade the old one in place.
AppId={{E4932109-4FDE-4069-BA22-947BFEA9DCB5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=SmartSchool
DefaultDirName={autopf}\SmartSchool Manager
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=app.ico
OutputDir=..\installer
OutputBaseFilename=SmartSchoolManager-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Install for the current user without admin rights by default; the user may choose all users.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\SmartSchoolManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only program files are removed. School data in %LOCALAPPDATA%\SmartSchoolManagerRebuild is kept on purpose.
Type: filesandordirs; Name: "{app}"
