# -*- coding: utf-8 -*-
"""
launcher.py — domiman.exe의 실제 진입점(런처).

PyInstaller는 이 파일만 컴파일해 실행 파일 본체(domiman.exe)로 삼는다.
실제 낚시 매크로 로직(domiman.py)은 매 실행마다 옆에서 그대로 읽어서
돌린다. 실행 중인 exe 자체는 Windows에서 덮어쓸 수 없으므로, '업데이트'는
domiman.py 파일만 통째로 교체하는 것으로 끝나게 하려는 구조다
(domiman.py의 [2-1. 버전 확인 + 수동 업데이트] 섹션 참고).

domiman.py는 runpy로 동적 로드되어 PyInstaller의 정적 분석 대상이
아니므로, 그 파일이 쓰는 의존성은 domiman.spec의 hiddenimports에
명시해 묻어들여야 한다.
"""
import os
import runpy
import sys

if getattr(sys, "frozen", False):
    _base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))

_core_path = os.path.join(_base_dir, "domiman.py")

if not os.path.isfile(_core_path):
    import tkinter as tk
    from tkinter import messagebox
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "domiman",
        f"실행 파일을 찾을 수 없습니다:\n{_core_path}\n\n"
        "설치가 손상됐을 수 있습니다. 재설치해 주세요.")
    os._exit(1)

runpy.run_path(_core_path, run_name="__main__")
