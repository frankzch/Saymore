; Inno Setup 6+ 脚本：把 dist/Saymore/ 打成 Windows 安装包。
; 编译：  iscc installer\setup.iss   （由 build.ps1 一键调）
; 输出：  installer\output\Saymore-Setup-<版本>.exe

#define AppName        "Saymore"
#define AppVersion     "1.0.0"
#define AppPublisher   "frankzch"
#define AppExeName     "Saymore.exe"
#define AppSource      "..\dist\Saymore"

[Setup]
AppId={{FD0E79C9-C0AA-4978-AC16-B298EAD6F397}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL=https://github.com/frankzch/saymore
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
OutputDir=output
OutputBaseFilename=Saymore-Setup-{#AppVersion}
SetupIconFile=..\saymore.ico
WizardStyle=modern
; 装到 %LocalAppData% 免管理员权限；用户目录本身可写，config 就地写就行
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
; 老版本卸载后再装：避免残留旧 DLL
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "zh"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项:"; Flags: unchecked
Name: "autostart";   Description: "开机自动启动 Saymore"; GroupDescription: "附加选项:"

[Files]
; 拉整个 PyInstaller 输出目录进去
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";               Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}";           Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}";       Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启：HKCU Run 项，任务管理器"启动"页可见并可开关（与应用内 saymore/win/autostart.py 一致）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "Saymore"; ValueData: """{app}\{#AppExeName}"""; \
    Tasks: autostart; Flags: uninsdeletevalue

[Run]
; 装完可选立即启动
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时留住用户 config/hotwords/历史/日志——重装能保留；用户想彻底清就手动删
Type: filesandordirs; Name: "{app}\_MEI*"
