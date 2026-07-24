# -*- coding: utf-8 -*-
"""
'낚시 취소'/'낚시 시작' 토글 버튼 OCR 위치 검증 스크립트.

COORD_FISHING_BTN=(1086,988) 버튼은 낚시 진행 중엔 '낚시 취소', 대기 중엔
'낚시 시작'으로 글자만 바뀐다(버튼 위치는 동일). 이 스크립트는:
  1) 현재 실행 중인 게임 화면(라이브, WGC)에서 그 주변을 넉넉히 잘라 OCR —
     지금은 낚시 중이라 '낚시 취소'가 보일 것으로 예상.
  2) 정적 스크린샷 파일(20260719025713.jpg, 낚시 대기 중 캡처분)의 같은
     좌표를 잘라 OCR — '낚시 시작'이 보일 것으로 예상.
두 결과를 비교해 두 상태 모두 안정적으로 잡히는 REGION_FISHING_BTN(x,y,w,h)
후보를 제안한다. 읽기 전용 진단 — domiman.py 로직은 건드리지 않는다.
"""
import os
import sys
import time

_raw = sys.__stdout__.buffer


def w(s=""):
    _raw.write((str(s) + "\n").encode("utf-8", "replace"))
    _raw.flush()


sys.path.insert(0, r"c:\Users\windo\OneDrive - 한국교통대학교\문서\Python\macro")
import domiman  # noqa: E402
import easyocr  # noqa: E402
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

OUT_DIR = os.path.join(domiman.SCRIPT_DIR, "ocr_verify_result")
os.makedirs(OUT_DIR, exist_ok=True)

STATIC_IMG = (r"C:\Users\windo\OneDrive - 한국교통대학교\문서\테일즈런너"
              r"\스크린샷\20260719025713.jpg")

# COORD_FISHING_BTN=(1086,988) 중심으로 넉넉하게 (실측 후 좁힐 것)
CROP = (976, 963, 220, 50)   # (x, y, w, h)

try:
    FONT = ImageFont.truetype(r"C:\Windows\Fonts\malgun.ttf", 16)
except Exception:
    FONT = ImageFont.load_default()

w("[시스템] OCR 모델 로드 중... (수 초 소요)")
domiman.reader = easyocr.Reader(
    ['ko', 'en'], gpu=False,
    model_storage_directory=os.path.join(domiman.SCRIPT_DIR, "ocr_model"),
    download_enabled=False)
w("[시스템] OCR 모델 로드 완료.")


def annotate_and_save(rgb_img, results, out_path):
    pil = Image.fromarray(rgb_img)
    d = ImageDraw.Draw(pil)
    for box, text, conf in results:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        bx1, by1, bx2, by2 = min(xs), min(ys), max(xs), max(ys)
        color = (255, 0, 0) if conf < 0.5 else (0, 150, 0)
        d.rectangle([bx1, by1, bx2, by2], outline=color, width=2)
        d.text((bx1, max(0, by1 - 18)), f"{text}({conf:.2f})", font=FONT, fill=color)
    pil.save(out_path)
    w(f"  -> 주석 이미지 저장: {out_path}")


def report(label, x0, y0, results):
    w(f"\n=== [{label}] {len(results)}개 글자조각 인식됨 ===")
    for box, text, conf in results:
        cx = x0 + sum(p[0] for p in box) / 4.0
        cy = y0 + sum(p[1] for p in box) / 4.0
        xs = [p[0] + x0 for p in box]
        ys = [p[1] + y0 for p in box]
        bbox = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
        w(f"  conf={conf:.3f}  중심=({cx:.0f},{cy:.0f})  bbox={bbox}  텍스트='{text}'")

    hit = next((it for it in results if "낚시" in it[1].replace(" ", "")
                or "취소" in it[1].replace(" ", "") or "시작" in it[1].replace(" ", "")), None)
    if hit:
        box, text, conf = hit
        xs = [p[0] + x0 for p in box]
        ys = [p[1] + y0 for p in box]
        bx1, by1, bx2, by2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
        margin_x, margin_y = 12, 8
        suggest = (bx1 - margin_x, by1 - margin_y,
                   (bx2 - bx1) + margin_x * 2, (by2 - by1) + margin_y * 2)
        w(f"  -> 버튼 텍스트 후보: '{text}' bbox=({bx1},{by1},{bx2},{by2})")
        w(f"  -> REGION_FISHING_BTN 제안 = {suggest}")
        return suggest
    w("  [경고] '낚시'/'취소'/'시작' 관련 글자를 찾지 못함")
    return None


# === [1. 라이브 캡처 — 지금 낚시 중이면 '낚시 취소' 기대] ===
if not domiman.bring_game_to_front(domiman.GAME_KEYWORD):
    w("[실패] 게임 창을 찾지 못했습니다.")
    sys.exit(1)
if not domiman._ensure_watch_capture():
    w("[경고] WGC 캡처 시작 실패 — pyautogui 폴백으로 진행합니다.")
time.sleep(1.0)

x0, y0, cw, ch = CROP
live_rgb = domiman._watch_grab_region(CROP)
suggest_live = None
try:
    if live_rgb is None:
        w("[실패] 라이브 화면을 읽지 못했습니다.")
    else:
        live_results = domiman.reader.readtext(live_rgb)
        suggest_live = report("라이브 화면 (낚시 중 예상 -> '낚시 취소')", x0, y0, live_results)
        annotate_and_save(live_rgb, live_results,
                           os.path.join(OUT_DIR, f"낚시상태_라이브_{time.strftime('%Y%m%d_%H%M%S')}.png"))
finally:
    if domiman.game_capture is not None and domiman.game_capture.is_running:
        domiman.game_capture.stop()

# === [2. 정적 스크린샷 — 낚시 대기 중 캡처분, '낚시 시작' 기대] ===
suggest_static = None
if not os.path.isfile(STATIC_IMG):
    w(f"\n[경고] 정적 스크린샷을 찾을 수 없습니다: {STATIC_IMG}")
else:
    data = np.fromfile(STATIC_IMG, dtype=np.uint8)
    img_bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img_bgr is None:
        w(f"\n[실패] 이미지를 읽을 수 없습니다: {STATIC_IMG}")
    else:
        roi_bgr = img_bgr[y0:y0 + ch, x0:x0 + cw]
        roi_rgb = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2RGB)
        static_results = domiman.reader.readtext(roi_rgb)
        suggest_static = report("정적 스크린샷 (대기 중 예상 -> '낚시 시작')", x0, y0, static_results)
        annotate_and_save(roi_rgb, static_results,
                           os.path.join(OUT_DIR, f"낚시상태_스크린샷_{time.strftime('%Y%m%d_%H%M%S')}.png"))

# === [3. 결론] ===
w("\n=== [결론] ===")
w(f"  라이브 제안 영역   : {suggest_live}")
w(f"  스크린샷 제안 영역 : {suggest_static}")
if suggest_live and suggest_static:
    w("  -> 두 좌표가 비슷하면 '두 버튼의 위치가 동일하다'는 전제가 맞는 것으로 확인됨.")

w("\n[완료] 진단 종료.")
