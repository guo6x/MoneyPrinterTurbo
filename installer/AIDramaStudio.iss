#define MyAppName "AIDrama Studio"
#define MyAppVersion "1.0.0"
#define MyAppExeName "AIDramaStudio.exe"

[Setup]
AppId={{E4E28FF4-937C-4E88-A6C4-E175C0A7F232}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\AIDrama Studio
DefaultGroupName=AIDrama Studio
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=AIDramaStudio-{#MyAppVersion}-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern

[Files]
Source: "..\dist\AIDramaStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AIDrama Studio"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\AIDrama Studio"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 AIDrama Studio"; Flags: nowait postinstall skipifsilent

; User projects, credentials and logs live under %LOCALAPPDATA%\AIDramaStudio.
; Deliberately define no user-data deletion section, so uninstall and upgrade
; preserve that directory by default.
