; Inno Setup 6+ 脚本：把 dist/Saymore/ 打成 Windows 安装包。
; 编译：  iscc installer\setup.iss   （由 build.ps1 一键调）
; 输出：  installer\output\Saymore-Setup-<版本>.exe
;
; 目录布局(装完后 {app}\ 下)：
;   Saymore.exe           ← PyInstaller 产物
;   _internal\            ← Python 运行时(不动)
;   config.json           ← 首启由程序自己写(用 DEFAULT_CONFIG),之后用户改就保留、卸载不删
;   terms.txt             ← 术语表(用户可编辑,卸载不删)
;   silero_vad.onnx       ← VAD
;   kws-model\            ← 唤醒模型
;   polish_lora\          ← 整理 LoRA
;   llama-cpp\            ← 推理引擎
;   models\               ← 首启后台下载的 ASR gguf(约 1.5GB)——卸载删
;   logs\                 ← 运行日志——卸载删
; 见 saymore/paths.py: 打包后 PROJECT_ROOT = Saymore.exe 所在目录。

#define AppName        "Saymore"
; 版本号：build.ps1 -v 1.0.1 会通过 /DAppVersion=1.0.1 覆盖，不传就用这里的默认值
#ifndef AppVersion
  #define AppVersion   "1.0.0"
#endif
#define AppPublisher   "frankzch"
#define AppExeName     "Saymore.exe"
#define AppSource      "..\dist\Saymore"
#define RepoRoot       ".."

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
; --- PyInstaller 产物：Saymore.exe + _internal\ ---
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; --- 用户可见资源:直接从仓库拷,不进 _internal ---
; config.json 不 ship:首启由 saymore.config.load_config() 用 DEFAULT_CONFIG 写一份干净的
; (device=auto 自动探测 vulkan/cpu),同时让 main.py 的 first_run 判定(CONFIG_PATH.exists())
; 生效,首启自动弹主窗。仓库根那份是 dev 配置(device=cuda 等),不适合直接下发给用户。
; terms.txt 是用户术语表种子,首装拷贝、之后保留改动、卸载不删
Source: "{#RepoRoot}\terms.txt";       DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#RepoRoot}\silero_vad.onnx"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\saymore.ico";     DestDir: "{app}"; Flags: ignoreversion

; 大件目录:整目录搬,排掉 dev-only 副产物
; keywords.txt 必须保留(唤醒兜底,click 未装/写盘失败时靠它);keywords_raw.txt 一起留,无害
Source: "{#RepoRoot}\kws-model\*";   DestDir: "{app}\kws-model";   Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "test_wavs\*,*.bak"
Source: "{#RepoRoot}\polish_lora\*"; DestDir: "{app}\polish_lora"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.bak"
Source: "{#RepoRoot}\llama-cpp\*";   DestDir: "{app}\llama-cpp";   Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";               Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}";           Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}";         Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; 开机自启：HKCU Run 项，任务管理器"启动"页可见并可开关（与应用内 saymore/win/autostart.py 一致）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "Saymore"; ValueData: """{app}\{#AppExeName}"""; \
    Tasks: autostart; Flags: uninsdeletevalue

[Run]
; 装完可选立即启动
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 方案 A: 卸载删下载的模型 + 日志 + 运行时生成的 keywords 缓存；保留 config.json / terms.txt。
; 之后 {app}\ 若空,Inno 会自动清掉(需 [Setup] 里没设 UsePreviousAppDir=no 之类;默认行为即可)。
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\logs"
; 运行时刮痕文件(.launch_count / .ready / .*trigger / .download_*.json 等)
; 一并清掉,重装才能重置"首启弹窗"计数
Type: files;          Name: "{app}\.*"
Type: files;          Name: "{app}\kws-model\keywords.txt"
Type: files;          Name: "{app}\kws-model\keywords_raw.txt"
; 强删整个 _internal:里头会有运行时新增的东西(comtypes.gen SAPI 存根、
; __pycache__、rapidocr/sherpa 首启生成的 cache),Inno 没登记就不会自动清,
; 目录卡着不空、无法整体删掉。这里一并端掉。
Type: filesandordirs; Name: "{app}\_internal"
; 兜底:老版本 onefile 残留(现走 onedir 用不上,留着无害)
Type: filesandordirs; Name: "{app}\_MEI*"
