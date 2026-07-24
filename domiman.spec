# -*- mode: python ; coding: utf-8 -*-
# domiman.py PyInstaller 빌드 스펙 (onedir = 분할 파일 구조, AV 오탐 완화용)
# 빌드:  pyinstaller domiman.spec --noconfirm --clean
# 결과:  dist/domiman/  (이 폴더 전체를 installer.iss로 설치프로그램화)
#
# ※ 반드시 python.org 정식 Python(가상환경)에서 빌드할 것. Microsoft Store
#    Python으로는 PyInstaller가 정상 동작하지 않는다.
# ※ GUI 전용(콘솔 없음). 로그는 domiman.py 내부 로그 창으로 확인.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# 무겁고 데이터/DLL이 흩어져 있는 패키지들은 통째로 수집(가장 안전).
# torch/torchvision은 easyocr의 인식 모델 백엔드라 실제로 필요(CNN 제거는
# 별개의 물고기 분류 모델 얘기 — CLAUDE.md 참고).
for pkg in [
    'easyocr',        # OCR 아키텍처/문자셋/유틸 데이터
    'torch',          # torch/lib/*.dll, oneDNN 등
    'torchvision',
    'cv2',            # OpenCV 5 바이너리
    'skimage',        # easyocr 의존(scikit-image)
    'scipy',          # easyocr 의존
    'shapely',        # easyocr 의존
    'pyclipper',      # easyocr 의존
    'windows_capture' # 컴파일된 .pyd (QHD/감시 캡처)
]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pywin32
hiddenimports += ['win32gui', 'win32con', 'win32api', 'win32process']

# OCR 모델 동봉 (다운로드 불가 시스템 대응). 런타임엔 sys._MEIPASS/ocr_model 로 접근.
datas += [('ocr_model', 'ocr_model')]

a = Analysis(
    ['domiman.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 불필요한 대형 모듈 제외 → 용량·AV 표면 축소 (tkinter는 GUI라 제외 금지)
    excludes=['matplotlib', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
              'IPython', 'notebook', 'pandas', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # onedir: 바이너리는 COLLECT로 분리
    name='domiman',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # ★ UPX 금지: 압축이 AV 오탐의 주범
    console=False,               # ★ GUI 전용 — 콘솔 창 없음
    uac_admin=True,              # ★ 관리자 권한 요청(requireAdministrator).
                                 #   게임이 관리자로 돌 때 UIPI 때문에 입력/포커스
                                 #   전송이 막히므로, 매크로도 관리자로 떠야 한다.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,                   # ★ 여기도 UPX 금지
    upx_exclude=[],
    name='domiman',
)
