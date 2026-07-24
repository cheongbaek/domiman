# -*- coding: utf-8 -*-
"""
낚싯대 리스트(보유 낚싯대 창) 실시간 OCR 검증 스크립트.

사용법: 게임에서 '보유 낚싯대' 리스트 창을 띄워둔 상태로 이 스크립트를
실행한다. domiman.py의 실제 자동교체 로직과 동일한 캡처(WGC)/OCR 경로로
REGION_BAIT_NAMES(카드 이름 바 8칸)를 읽어, 슬롯(행,열)별로 인식된 모든
글자 조각과 신뢰도를 그대로 출력한다. '푸른 장미검 낚싯대'가 ROD_TARGET_
PATTERN(r"스타|장미검")에 왜 안 걸리는지(분절/오인식/낮은 conf/그리드
허용범위 밖 등) 데이터로 확인하기 위한 읽기 전용 진단 도구 — 실제 매크로
로직(domiman.py)은 건드리지 않는다.

domiman.py를 import하면 sys.stdout이 GUI 로그용 LogWriter로 즉시
치환되므로(모듈 최상단), 이 스크립트의 출력은 import 전에 챙겨둔 원본
stdout 버퍼(_raw)에 직접 써서 콘솔/파일 어디서 실행해도 보이게 한다.
"""
import os
import re
import sys
import time

_raw = sys.__stdout__.buffer


def w(s=""):
    _raw.write((str(s) + "\n").encode("utf-8", "replace"))
    _raw.flush()


sys.path.insert(0, r"c:\Users\windo\OneDrive - 한국교통대학교\문서\Python\macro")
import domiman  # noqa: E402
import easyocr  # noqa: E402

OUT_DIR = os.path.join(domiman.SCRIPT_DIR, "ocr_verify_result")
os.makedirs(OUT_DIR, exist_ok=True)

w("[시스템] OCR 모델 로드 중... (수 초 소요)")
domiman.reader = easyocr.Reader(
    ['ko', 'en'], gpu=False,
    model_storage_directory=os.path.join(domiman.SCRIPT_DIR, "ocr_model"),
    download_enabled=False)
w("[시스템] OCR 모델 로드 완료.")

if not domiman.bring_game_to_front(domiman.GAME_KEYWORD):
    w("[실패] 게임 창을 찾지 못했습니다. 게임을 켜고 '보유 낚싯대' 리스트를 띄운 뒤 다시 실행하세요.")
    sys.exit(1)

if not domiman._ensure_watch_capture():
    w("[경고] WGC 캡처 시작 실패 — pyautogui 폴백으로 진행합니다(FHD 전용, 다른 창에 가려지면 실패).")
time.sleep(1.0)   # 리스트 창 렌더링 안정화 대기

x0, y0, rw, rh = domiman.REGION_BAIT_NAMES
img = domiman._watch_grab_region(domiman.REGION_BAIT_NAMES)

try:
    if img is None:
        w("[실패] 화면을 읽지 못했습니다.")
        sys.exit(1)

    results = domiman.reader.readtext(img)
    w(f"\n총 {len(results)}개 글자조각 인식됨. (영역 REGION_BAIT_NAMES={domiman.REGION_BAIT_NAMES})")

    # === [1. 전체 인식 결과 원본 그대로, 신뢰도 낮은 순] ===
    w("\n=== [1. 전체 인식 결과 (conf 낮은 순 — 의심스러운 것이 위로)] ===")
    raw_rows = []
    for box, text, conf in results:
        cx = x0 + sum(p[0] for p in box) / 4.0
        cy = y0 + sum(p[1] for p in box) / 4.0
        raw_rows.append((conf, text, round(cx), round(cy)))
    for conf, text, cx, cy in sorted(raw_rows):
        w(f"  conf={conf:.3f}  중심=({cx},{cy})  텍스트='{text}'")

    # === [2. 슬롯(행,열)별 그룹핑 — _find_cards_by_pattern과 동일 판정 로직] ===
    w("\n=== [2. 슬롯(행,열)별 그룹핑 — 기존 매칭 로직(±30px 허용) 기준] ===")
    slots = {}
    rejected = []
    for box, text, conf in results:
        ntext = text.replace(" ", "")
        cx = x0 + sum(p[0] for p in box) / 4.0
        cy = y0 + sum(p[1] for p in box) / 4.0
        row = min(range(len(domiman.BAIT_NAME_ROW_Y)),
                  key=lambda r: abs(cy - domiman.BAIT_NAME_ROW_Y[r]))
        col = min(range(len(domiman.BAIT_COL_X)),
                  key=lambda c: abs(cx - domiman.BAIT_COL_X[c]))
        dy = abs(cy - domiman.BAIT_NAME_ROW_Y[row])
        if dy > 30:
            rejected.append((text, conf, round(cx), round(cy), dy))
            continue
        slots.setdefault((row, col), []).append((text, ntext, conf))

    for row in range(len(domiman.BAIT_NAME_ROW_Y)):
        for col in range(len(domiman.BAIT_COL_X)):
            frags = slots.get((row, col), [])
            joined = "".join(nt for _, nt, _ in frags)
            hit = re.search(domiman.ROD_TARGET_PATTERN, joined)
            frag_s = ", ".join(f"'{t}'(conf {c:.2f})" for t, _, c in frags) or "(없음)"
            w(f"  [{row + 1}행 {col + 1}열] 조각: {frag_s}")
            w(f"      -> 합친 텍스트='{joined}'  ROD_TARGET_PATTERN(r\"스타|장미검\") 매칭={'O' if hit else 'X'}")

    if rejected:
        w("\n=== [3. 허용범위(±30px) 밖이라 제외된 조각 — 그리드 행 오차 의심] ===")
        for text, conf, cx, cy, dy in sorted(rejected, key=lambda r: -r[4]):
            w(f"  텍스트='{text}'  conf={conf:.3f}  중심=({cx},{cy})  행기준오차={dy:.1f}px")
    else:
        w("\n=== [3. 허용범위 밖 제외 조각 없음] ===")

    # === [4. 시각 확인용 주석 이미지 저장] ===
    try:
        import cv2
        from PIL import Image, ImageDraw, ImageFont
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\malgun.ttf", 15)
        except Exception:
            font = ImageFont.load_default()
        pil = Image.fromarray(img)
        d = ImageDraw.Draw(pil)
        for box, text, conf in results:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            bx1, by1, bx2, by2 = min(xs), min(ys), max(xs), max(ys)
            color = (255, 0, 0) if conf < 0.5 else (0, 150, 0)
            d.rectangle([bx1, by1, bx2, by2], outline=color, width=2)
            d.text((bx1, max(0, by1 - 18)), f"{text}({conf:.2f})", font=font, fill=color)
        out_path = os.path.join(OUT_DIR, f"낚싯대리스트_{time.strftime('%Y%m%d_%H%M%S')}.png")
        pil.save(out_path)
        w(f"\n[완료] 주석 이미지 저장: {out_path}")
    except Exception as e:
        w(f"\n[경고] 주석 이미지 저장 실패(무시 가능): {e}")

finally:
    if domiman.game_capture is not None and domiman.game_capture.is_running:
        domiman.game_capture.stop()

w("\n[완료] 진단 종료.")
