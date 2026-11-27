; Inno Setup 6.3+ script. Build with:  ISCC /DAppVersion=1.0.0 packaging\filebot.iss
; Expects dist\FileBot to be the finished folder from packaging\build.ps1 (PyInstaller output
; with bin\ and models\ already staged under _internal).
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F0B4C52-7A1E-4E0B-9D6A-3A5C1D2E8F41}
AppName=FileBot
AppVersion={#AppVersion}
AppPublisher=Sarv
DefaultDirName={autopf}\FileBot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=FileBot-Setup-{#AppVersion}
; The GGUF model is already compressed, so a fast non-solid pass keeps build time sane.
Compression=lzma2/fast
SolidCompression=no
UninstallDisplayIcon={app}\FileBot.exe
WizardStyle=modern
; NOTE: if the compiled installer exceeds the single-file size limit of your Inno version,
; set DiskSpanning=yes (verify on a full build; this has not been run here).

[Tasks]
Name: "startup"; Description: "Start FileBot when I sign in to Windows"; Flags: unchecked

[Files]
Source: "..\dist\FileBot\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\FileBot"; Filename: "{app}\FileBot.exe"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "FileBot"; ValueData: """{app}\FileBot.exe"""; Tasks: startup; Flags: uninsdeletevalue

[Run]
Filename: "{app}\FileBot.exe"; Description: "Launch FileBot"; Flags: nowait postinstall skipifsilent

; Your index, logs and settings (%USERPROFILE%\.filebot and %APPDATA%\FileBot) are kept on uninstall.
