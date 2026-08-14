; 도미챗(domichat) 설치프로그램 (Inno Setup)
; 1) PyInstaller로 dist\domichat\ 를 먼저 빌드
;      pyinstaller domichat.spec --noconfirm --clean
; 2) Inno Setup Compiler로 이 스크립트를 컴파일 → Output\도미챗_설치.exe
;
; ※ domiman(installer.iss)과 **완전히 별개**다. AppId·설치 폴더·시작 메뉴 이름·
;    출력 파일명이 모두 달라 서로 덮어쓰거나 지우지 않는다.
; ※ **관리자 권한이 필요 없다.** PrivilegesRequired=lowest + 사용자 폴더
;    (LOCALAPPDATA) 설치. 이 점이 업데이트에도 중요하다 — domichat.exe는
;    옆의 domichat.py를 매 실행 읽어 돌리고 ⟳ 버튼이 그 파일을 덮어쓰는데,
;    Program Files에 설치하면 그 덮어쓰기에 관리자 권한이 필요해진다.

#define MyAppName "도미챗"
#define MyAppVersion "1.0"
#define MyAppExeName "domichat.exe"

[Setup]
; domiman과 절대 겹치지 않도록 전용 AppId를 명시한다(제거·업그레이드 식별자).
AppId={{7C1D2A64-9B3E-4E27-8F51-0C1A7E4D9B21}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\domichat
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; lowest 로 못박는다. PrivilegesRequiredOverridesAllowed=dialog 를 두면 관리자로
; 실행됐을 때 Setup이 **관리자 모드로 전환**돼(HKLM 등록, 공용 설치) ⟳ 업데이트가
; domichat.py 를 덮어쓸 때 권한을 요구하게 된다 — 실측으로 확인해 제거했다.
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=도미챗_설치
SetupIconFile=domichat.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 아이콘 만들기"; GroupDescription: "추가 아이콘:"

[Files]
; PyInstaller onedir 결과물 전체를 그대로 담는다.
Source: "dist\domichat\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Filename만 지정하면 exe에 내장된 아이콘(domichat.ico)을 자동으로 사용한다.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "지금 실행"; Flags: nowait postinstall skipifsilent
