; Inno Setup 6 script for the ApplyPilot Copilot friend installer.
; Built by packaging\build_windows.ps1 (which sets APC_VERSION); paths are
; relative to this file. Per-user install: no admin rights needed.

#define AppName "ApplyPilot Copilot"
#define AppVersion GetEnv("APC_VERSION")

[Setup]
AppId={{6E1C3B9A-4F2D-4C8E-9B7A-3D5F1A2C8E40}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ApplyPilot
DefaultDirName={localappdata}\Programs\ApplyPilotCopilot
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=ApplyPilotCopilot-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
; The service may be running from these files during an upgrade or uninstall.
CloseApplications=force
RestartApplications=no

[Files]
Source: "..\..\build\friend\windows\ApplyPilotCopilot\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\build\friend\windows\extension\*"; DestDir: "{app}\extension"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\build\friend\windows\SETUP.txt"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\ApplyPilotCopilot.exe"; Parameters: "install-host --extension-dir ""{app}\extension"""; Flags: runhidden waituntilterminated; StatusMsg: "Connecting ApplyPilot Copilot to Chrome..."
Filename: "{app}\SETUP.txt"; Description: "Show the next steps (Load the extension in Chrome)"; Flags: postinstall shellexec skipifsilent
Filename: "{app}\extension"; Description: "Open the extension folder"; Flags: postinstall shellexec skipifsilent

[UninstallRun]
Filename: "{app}\ApplyPilotCopilot.exe"; Parameters: "uninstall-host"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallHost"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ApplyPilotCopilot.exe"; Flags: runhidden waituntilterminated; RunOnceId: "StopService"
