# -*- coding: utf-8 -*-
"""domiman.py 의 최상위 import 가 domiman.spec 의 hiddenimports 에 다 있는지 검사.

domiman.py 도 exe에서 launcher 가 runpy 로 읽어 돌리므로 **PyInstaller 정적 분석
대상이 아니다.** 새로 추가한 import 를 스펙에 안 적으면 스크립트로는 멀쩡한데
exe에서만 시작조차 못 한다(domichat 에서 `import uuid` 하나로 실제로 그렇게 죽었다).

사용: python scripts/check_domiman_spec.py     (빠진 게 있으면 종료코드 1)
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def top_level_imports(path):
    mods = set()

    def scan(body):
        for n in body:
            if isinstance(n, ast.Import):
                for a in n.names:
                    mods.add(a.name)
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods.add(n.module)
            elif isinstance(n, ast.Try):
                scan(n.body)          # try/except 로 감싼 선택적 import 포함

    with open(path, encoding="utf-8") as fp:
        scan(ast.parse(fp.read()).body)
    return mods


def main():
    used = top_level_imports(os.path.join(BASE, "domiman.py"))
    with open(os.path.join(BASE, "domiman.spec"), encoding="utf-8") as fp:
        spec = fp.read()
    missing = sorted(m for m in used if f"'{m}'" not in spec)
    print(f"domiman.py 최상위 import {len(used)}개")
    if missing:
        print("\n스펙에 빠진 모듈 (exe에서 ModuleNotFoundError 가 납니다):")
        for m in missing:
            print(f"  - {m}")
        print("\n→ domiman.spec 의 hiddenimports 에 추가하세요.")
        return 1
    print("모두 스펙에 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
