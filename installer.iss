; 도미맨(domiman.py) 설치프로그램 (Inno Setup)
; 1) PyInstaller로 dist\domiman\ 를 먼저 빌드 (pyinstaller domiman.spec --noconfirm --clean)
; 2) Inno Setup Compiler로 이 스크립트를 컴파일 → Output\도미맨_설치.exe 생성
; 다운로드: https://jrsoftware.org/isdl.php
; ※ 설치프로그램 자체(마법사 UI)는 커스텀 아이콘이 필요 없음(요청사항).
;    바로가기 아이콘은 exe에 이미 내장된 앱 아이콘을 그대로 쓴다.

#define MyAppName "도미맨"
#define MyAppVersion "1.0"
#define MyAppExeName "domiman.exe"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\domiman
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=도미맨_설치
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 매크로가 좌표를 절대 픽셀로 다루므로 인스톨러도 DPI 인식
WizardStyle=modern

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 아이콘 만들기"; GroupDescription: "추가 아이콘:"

[Files]
; PyInstaller onedir 결과물 전체를 그대로 담는다.
Source: "dist\domiman\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; Filename만 지정하면 exe에 내장된 아이콘(app.ico)을 자동으로 사용한다.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "지금 실행"; Flags: nowait postinstall skipifsilent
