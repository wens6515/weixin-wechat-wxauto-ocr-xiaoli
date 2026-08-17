; 小漓安装包脚本 — Inno Setup 6
; 编译：ISCC.exe 小漓.iss（输出到 dist\小漓安装包.exe）
; 在 dist 目录下执行：ISCC.exe 小漓.iss

#define MyAppName "小漓"
#define MyAppVersion "2.0.0"
#define MyAppExeName "小漓.exe"
#define MyAppId "{{cee1eae6-2485-43b3-8649-fbf772492265}}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
; 强制显示"选择安装位置"向导页（默认 auto 会在用标准目录常量时隐藏目录页，用户无法自选路径）
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=小漓安装包
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional tasks:"

[Files]
Source: "小漓\小漓.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "小漓\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "小漓\壁纸\*"; DestDir: "{app}\壁纸"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "小漓\fonts\*"; DestDir: "{app}\fonts"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tools\install-node.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装 Node.js（缺失时自动下载官方 LTS 并 per-user 静默安装，已装则跳过）
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\install-node.ps1"""; StatusMsg: "Checking Node.js (auto-install if missing)..."; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
