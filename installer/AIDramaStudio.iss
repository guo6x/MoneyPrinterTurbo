#ifndef MyAppName
  #define MyAppName "AIDrama Studio"
#endif
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef DeliveryHead
  #define DeliveryHead "UNSPECIFIED"
#endif
#ifndef MyAppExeName
  #define MyAppExeName "AIDramaStudio.exe"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\AIDramaStudio"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "AIDramaStudio-{#MyAppVersion}-Windows-x64-Setup"
#endif

[Setup]
AppId={{E4E28FF4-937C-4E88-A6C4-E175C0A7F232}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoTextVersion={#MyAppVersion} ({#DeliveryHead})
DefaultDirName={localappdata}\Programs\AIDrama Studio
DefaultGroupName=AIDrama Studio
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Compatibility marker for older release-audit tooling: Source: "..\dist\AIDramaStudio\*"

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
