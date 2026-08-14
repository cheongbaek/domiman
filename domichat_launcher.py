# -*- coding: utf-8 -*-
"""
domichat_launcher.py — domichat.exe의 실제 진입점(런처).

PyInstaller는 이 파일만 컴파일해 실행 파일 본체(domichat.exe)로 삼는다.
채팅 클라이언트 로직(domichat.py)은 매 실행마다 옆에서 그대로 읽어서 돌린다.
실행 중인 exe 자체는 Windows에서 덮어쓸 수 없으므로, '업데이트'는 domichat.py
파일만 통째로 교체하는 것으로 끝나게 하려는 구조다
(domichat.py의 [1-1. 버전 확인 + 수동 업데이트] 섹션 참고).

domichat.py는 runpy로 동적 로드되어 PyInstaller의 정적 분석 대상이 아니므로,
그 파일이 쓰는 의존성은 domichat.spec의 hiddenimports에 명시해 묻어들여야 한다.

domiman의 launcher.py와 같은 구조지만 **관리자 권한을 요구하지 않는다** —
채팅 클라이언트는 다른 프로그램에 입력을 보내지 않으므로 권한 상승이 필요 없고,
설치도 사용자 폴더(LOCALAPPDATA)에 하기 때문에 업데이트로 domichat.py를
덮어쓰는 것도 권한 없이 된다.
"""
import os
import runpy
import sys

if getattr(sys, "frozen", False):
    _base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    _base_dir = os.path.dirname(os.path.abspath(__file__))

_core_path = os.path.join(_base_dir, "domichat.py")

def _alert(msg):
    # DOMICHAT_NO_UI=1 이면 대화상자 대신 출력만 한다(자동 복구 동작을 시험할 때 쓴다).
    if os.environ.get("DOMICHAT_NO_UI") == "1":
        print(msg)
        sys.stdout.flush()   # 아래에서 os._exit 로 끝내면 버퍼가 비워지지 않는다
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("domichat", msg)
        root.destroy()
    except Exception:
        print(msg)


if not os.path.isfile(_core_path):
    _alert(f"실행 파일을 찾을 수 없습니다:\n{_core_path}\n\n"
           "설치가 손상됐을 수 있습니다. 재설치해 주세요.")
    os._exit(1)

# 업데이트가 앱을 못 쓰게 만드는 것을 막는다(중요).
# domichat.py 는 exe에 데이터로만 들어가 runpy로 실행되므로 PyInstaller 정적 분석
# 대상이 아니다. 그래서 새 버전이 **번들에 없는 모듈을 import** 하면 시작조차 못 하고,
# 그러면 ⟳ 버튼을 눌러 되돌릴 수도 없다(실제로 `import uuid` 하나로 그렇게 됐다).
# 업데이트가 남겨둔 .bak 이 있으면 그것으로 자동 복구하고 사유를 파일로 남긴다.
try:
    runpy.run_path(_core_path, run_name="__main__")
except SystemExit:
    raise
except BaseException:
    import traceback
    _err = traceback.format_exc()
    _bak = _core_path + ".bak"
    _log = os.path.join(os.path.dirname(_core_path), "domichat_error.log")
    try:
        with open(_log, "w", encoding="utf-8") as fp:
            fp.write(_err)
    except Exception:
        pass
    if os.path.isfile(_bak):
        os.replace(_bak, _core_path)
        _alert("업데이트한 버전이 실행되지 않아 이전 버전으로 되돌렸습니다.\n\n"
               f"오류 기록: {_log}\n\n확인을 누르면 이전 버전으로 시작합니다.")
        runpy.run_path(_core_path, run_name="__main__")
    else:
        _alert(f"실행 중 오류가 났습니다.\n\n{_err.strip().splitlines()[-1]}\n\n"
               f"자세한 내용: {_log}")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
