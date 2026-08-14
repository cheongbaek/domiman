# -*- mode: python ; coding: utf-8 -*-
# domichat 빌드 스펙 (onedir = 분할 파일 구조, AV 오탐 완화용)
# 빌드:  pyinstaller domichat.spec --noconfirm --clean
# 결과:  dist/domichat/  (이 폴더 전체를 domichat_installer.iss로 설치프로그램화)
#
# ※ domiman.spec과 **완전히 별개**다. 이름(domichat)·아이콘(domichat.ico)·
#   출력 폴더(dist/domichat)가 모두 다르므로 두 앱이 섞이지 않는다.
# ※ 컴파일 대상은 domichat_launcher.py(실행 파일 본체)이고, domichat.py는
#   datas로만 동봉해 매 실행마다 runpy로 읽게 한다(업데이트 시 domichat.py만
#   교체하면 되는 구조 — domichat_launcher.py 상단 주석 참고).
# ※ **관리자 권한을 요구하지 않는다**(uac_admin 미지정 = asInvoker).
#   채팅 클라이언트는 권한 상승이 필요 없고, 설치도 사용자 폴더에 한다.

from PyInstaller.utils.hooks import collect_all

# Pillow는 이미지 기능(PNG 변환·클립보드·표시)에 쓰인다. 컴파일된 _imaging.pyd 와
# 플러그인들이 흩어져 있어 통째로 수집하는 게 안전하다.
pil_datas, pil_binaries, pil_hidden = collect_all('PIL')

datas = [
    # 클라이언트 로직 본체. 런타임엔 sys._MEIPASS/domichat.py 로 접근하며,
    # 업데이트는 이 파일만 통째로 교체하면 된다.
    ('domichat.py', '.'),
    # 창/작업표시줄 아이콘용 실물 파일(tkinter iconbitmap). exe 자체 아이콘은
    # 아래 EXE(icon=...)로 별도 임베드된다.
    ('domichat.ico', '.'),
]

# domichat.py는 runpy로 동적 로드되어 정적 분석에 안 잡히므로 여기에 명시한다.
# domichat.py는 표준 라이브러리만 쓰고(socket/sqlite3 없음, urllib으로 업데이트),
# 알림만 winotify가 있으면 쓰고 없으면 자체 토스트로 대체한다.
# domichat.py 의 **최상위 import 를 하나도 빠짐없이** 적는다.
# 빠뜨리면 스크립트로는 되는데 exe에서만 시작조차 못 한다(실측: `import uuid` 하나로
# 업데이트한 exe가 ModuleNotFoundError 로 죽었다). scripts/check_spec_imports.py 가
# domichat.py 를 파싱해 이 목록과 대조하므로, import 를 추가하면 여기도 같이 고칠 것.
hiddenimports = [
    'base64', 'ctypes', 'hashlib', 'io', 'json', 'os', 'queue', 're', 'socket',
    'ssl', 'struct', 'subprocess', 'sys', 'threading', 'time',
    'urllib.request',
    'tkinter', 'tkinter.font', 'tkinter.messagebox', 'tkinter.filedialog',
    # 선택적 의존(없으면 해당 기능만 꺼진다) — 번들에는 넣어둔다
    'winotify', 'PIL', 'PIL.Image', 'PIL.ImageGrab',
] + pil_hidden

a = Analysis(
    ['domichat_launcher.py'],
    pathex=[],
    binaries=pil_binaries,
    datas=datas + pil_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 채팅 클라이언트는 무거운 과학 계산 스택이 전혀 필요 없다 → 통째로 제외해
    # 용량과 AV 표면을 줄인다(tkinter는 GUI라 제외 금지).
    excludes=['matplotlib', 'numpy', 'scipy', 'pandas', 'cv2',
              'torch', 'torchvision', 'easyocr', 'skimage', 'shapely',
              'pyautogui', 'requests', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
              'IPython', 'notebook', 'pytest', 'setuptools'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # onedir: 바이너리는 COLLECT로 분리
    name='domichat',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # ★ UPX 금지: 압축이 AV 오탐의 주범
    console=False,               # ★ GUI 전용 — 콘솔 창 없음
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='domichat.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,                   # ★ 여기도 UPX 금지
    upx_exclude=[],
    name='domichat',
)
