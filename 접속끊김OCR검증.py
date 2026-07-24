# -*- coding: utf-8 -*-
# ============================================================
# 서버 접속 끊김 화면 OCR 좌표 검증 스크립트
# ------------------------------------------------------------
# 네트워크 이상으로 튕겼을 때 뜨는 대화상자(1920x1080 스크린샷)를 판독해
# 낚시.py에 하드코딩할 감지용 좌표/영역을 뽑아낸다.
#  - 타이틀: '서버와 접속이 끊어졌습니다.'
#  - 본문:   '해당 서버에 기술적인 문제가 있거나 네트워크의 문제일 수 있습니다.'
#  - 버튼:   '프로그램 종료'
# ============================================================
import io
import os
import re
import sys
import time

import cv2
import numpy as np
import easyocr
from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_PATH = r"C:\Users\windo\OneDrive - 한국교통대학교\20200620201719.jpg"

OUT_DIR = os.path.join(SCRIPT_DIR, "ocr_verify_result")
os.makedirs(OUT_DIR, exist_ok=True)

CROP = (720, 370, 1200, 640)   # 대화상자 주변만 판독

_log_lines = []
_orig_print = print


def print(*args, **kwargs):  # noqa: A001 - 검증 스크립트 한정 tee
    buf = io.StringIO()
    _orig_print(*args, **kwargs, file=buf)
    _log_lines.append(buf.getvalue())
    try:
        _orig_print(buf.getvalue(), end="")
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "cp949"
        _orig_print(buf.getvalue().encode(enc, "replace").decode(enc), end="")


print("[시스템] OCR 모델을 불러오는 중입니다. 잠시만 기다려주세요...")
reader = easyocr.Reader(['ko', 'en'], gpu=False,
                        model_storage_directory=os.path.join(SCRIPT_DIR, "ocr_model"),
                        download_enabled=False)

try:
    FONT = ImageFont.truetype(r"C:\Windows\Fonts\malgun.ttf", 16)
except Exception:
    FONT = ImageFont.load_default()


def suggest_region(bbox, margin_x=15, margin_y=8):
    x1, y1, x2, y2 = bbox
    return (x1 - margin_x, y1 - margin_y,
            (x2 - x1) + margin_x * 2, (y2 - y1) + margin_y * 2)


if __name__ == "__main__":
    data = np.fromfile(IMG_PATH, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"이미지를 읽을 수 없습니다: {IMG_PATH}")
    h, w = img.shape[:2]
    print(f"\n===== [접속 끊김 화면] {os.path.basename(IMG_PATH)} ({w}x{h}) =====")

    x1, y1, x2, y2 = CROP
    rgb = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
    t0 = time.time()
    results = reader.readtext(rgb)
    print(f"  (OCR {time.time() - t0:.1f}초, {len(results)}건)")

    items = []
    for box, text, conf in results:
        xs = [p[0] + x1 for p in box]
        ys = [p[1] + y1 for p in box]
        bb = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
        center = ((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
        ntext = re.sub(r"[\s'\"‘’“”`]", "", text)
        items.append((text, ntext, conf, bb, center))

    print(f"  {'인식 텍스트':<30} {'conf':>5}  {'중심(cx,cy)':<14} bbox")
    for text, ntext, conf, bb, center in sorted(items, key=lambda r: (r[3][1], r[3][0])):
        print(f"  {text:<30} {conf:>5.2f}  {str(center):<14} {bb}")

    title = next((it for it in items if "끊어" in it[1] or "접속이" in it[1]), None)
    body = next((it for it in items if "네트워크" in it[1] or "기술적" in it[1]), None)
    btn = next((it for it in items if "종료" in it[1]), None)

    print("\n  --- 하드코딩용 후보 좌표 (FHD 기준) ---")
    if title:
        print(f"  타이틀 '서버와 접속이 끊어졌습니다.': 중심 {title[4]}, bbox {title[3]}")
        print(f"    -> 감지용 OCR 영역 제안 REGION_DISCONNECT = {suggest_region(title[3])}")
    else:
        print("  [실패] 타이틀을 찾지 못했습니다.")
    if body:
        print(f"  본문(네트워크 문제 안내): 중심 {body[4]}, bbox {body[3]}")
    if btn:
        print(f"  '프로그램 종료' 버튼: 중심 {btn[4]}, bbox {btn[3]}")

    # 주석 이미지 저장
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    for text, ntext, conf, (bx1, by1, bx2, by2), (cx, cy) in items:
        color = (255, 0, 0) if any(k in ntext for k in ("끊어", "종료", "네트워크")) else (0, 90, 255)
        d.rectangle([bx1, by1, bx2, by2], outline=color, width=2)
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)
        d.text((bx1, max(0, by1 - 20)), f"{text} ({conf:.2f})", font=FONT, fill=color)
    out_png = os.path.join(OUT_DIR, "disconnect_20200620201719.png")
    pil.save(out_png)
    print(f"  -> 주석 이미지 저장: {out_png}")

    log_path = os.path.join(OUT_DIR, "접속끊김_결과.txt")
    with open(log_path, "w", encoding="utf-8") as fp:
        fp.writelines(_log_lines)
    _orig_print(f"\n[완료] 전체 결과 로그: {log_path}")
