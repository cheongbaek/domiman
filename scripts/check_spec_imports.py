# -*- coding: utf-8 -*-
"""domichat.py 의 최상위 import 가 domichat.spec 의 hiddenimports 에 다 있는지 검사.

**왜 필요한가:** domichat.py 는 exe에 데이터로만 들어가 runpy 로 실행되므로
PyInstaller 의 정적 분석 대상이 아니다. 새로 추가한 import 를 스펙에 안 적으면
스크립트로는 멀쩡한데 **exe에서만 시작조차 못 한다**(ModuleNotFoundError).
실제로 `import uuid` 하나 때문에 배포된 exe가 업데이트 후 죽었다.

사용: python scripts/check_spec_imports.py      (문제가 있으면 종료코드 1)
"""
import ast
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "domichat.py")
SPEC = os.path.join(BASE, "domichat.spec")


def top_level_imports(path):
    """모듈 최상위 import (try/except 로 감싼 선택적 import 포함)."""
    mods = set()

    def scan(body):
        for node in body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module)
            elif isinstance(node, ast.Try):
                scan(node.body)

    with open(path, encoding="utf-8") as fp:
        scan(ast.parse(fp.read()).body)
    return mods


def spec_hiddenimports(path):
    with open(path, encoding="utf-8") as fp:
        text = fp.read()
    m = re.search(r"hiddenimports\s*=\s*\[(.*?)\]", text, re.S)
    return set(re.findall(r"['\"]([\w.]+)['\"]", m.group(1))) if m else set()


def main():
    used = top_level_imports(SRC)
    listed = spec_hiddenimports(SPEC)
    # from tkinter import filedialog/messagebox 처럼 서브모듈로 쓰는 것도 확인
    with open(SRC, encoding="utf-8") as fp:
        text = fp.read()
    for sub in re.findall(r"from tkinter import ([\w, ]+)", text):
        for name in sub.split(","):
            used.add(f"tkinter.{name.strip()}")

    missing = sorted(m for m in used if m not in listed)
    print(f"domichat.py 최상위 import {len(used)}개 / 스펙 hiddenimports {len(listed)}개")
    if missing:
        print("\n스펙에 빠진 모듈 (exe에서 ModuleNotFoundError 가 납니다):")
        for m in missing:
            print(f"  - {m}")
        print("\n→ domichat.spec 의 hiddenimports 에 추가하세요.")
        return 1
    print("모두 스펙에 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
