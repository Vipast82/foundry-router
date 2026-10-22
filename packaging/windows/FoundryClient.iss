; Inno Setup script: per-user install (no admin), Start menu entry, optional start at
; sign-in, uninstaller. Build after PyInstaller:
;   iscc /DAppVersion=0.133.0 packaging\windows\FoundryClient.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7C1E2F64-6E0B-4F2B-9C44-5D3A9F0C1A11}
AppName=Foundry Client
AppVersion={#AppVersion}
AppPublisher=Foundry
DefaultDirName={localappdata}\Programs\FoundryClient
DefaultGroupName=Foundry Client
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=FoundryClientSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\foundry-client.ico
UninstallDisplayIcon={app}\FoundryClient.exe
CloseApplications=yes

[Tasks]
Name: "autostart"; Description: "Start Foundry Client when I sign in"; GroupDescription: "Startup:"

[Files]
Source: "..\..\dist\FoundryClient\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Foundry Client"; Filename: "{app}\FoundryClient.exe"; Parameters: "run"
Name: "{group}\Uninstall Foundry Client"; Filename: "{uninstallexe}"
Name: "{userstartup}\Foundry Client"; Filename: "{app}\FoundryClient.exe"; Parameters: "run"; Tasks: autostart

[Run]
Filename: "{app}\FoundryClient.exe"; Parameters: "run"; Description: "Start Foundry Client now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM FoundryClient.exe /T /F"; Flags: runhidden; RunOnceId: "StopClient"
