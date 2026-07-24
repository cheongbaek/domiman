# -*- coding: utf-8 -*-
# ============================================================
# 낚싯대 기간 만료 팝업 · 보유 낚싯대 창 OCR 좌표 검증 스크립트
# ------------------------------------------------------------
# 스크린샷(1920x1080)을 읽어 easyocr로 글자 위치를 판독하고,
# 낚시.py에 하드코딩할 FHD 기준 좌표/영역을 뽑아낸다.
#  - 만료 팝업: '! 아이템 기간 만료' 타이틀, 만료 안내문, 아이템 이름 위치
#  - 보유 낚싯대 창: 카드 그리드(이름/남은기간/사용하기·사용중 버튼) 좌표
# 결과: 콘솔 표 출력 + ocr_verify_result/ 폴더에 주석 이미지 + UTF-8 로그
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
SHOT_DIR = r"C:\Users\windo\OneDrive - 한국교통대학교\문서\테일즈런너\스크린샷"

EXPIRE_IMGS = ["20260719025713.jpg"]   # 아이템 기간 만료 팝업
ROD_IMGS = ["20260719030010.jpg"]      # 보유 낚싯대 창

OUT_DIR = os.path.join(SCRIPT_DIR, "ocr_verify_result")
os.makedirs(OUT_DIR, exist_ok=True)

# OCR 판독 범위 (좌표는 전체 이미지 기준으로 환산해 출력)
CROP_EXPIRE = (820, 440, 1090, 660)   # 만료 팝업 주변
CROP_ROD = (560, 330, 1360, 760)      # 보유 낚싯대 창 주변 (보유 미끼와 동일)

# 콘솔 인코딩이 뭐든 결과를 온전히 남기도록 UTF-8 로그 파일에도 동시 기록
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


# === [1. OCR 모델 로드 — 낚시.py와 동일 설정] ===
print("[시스템] OCR 모델을 불러오는 중입니다. 잠시만 기다려주세요...")
OCR_MODEL_DIR = os.path.join(SCRIPT_DIR, "ocr_model")
reader = easyocr.Reader(['ko', 'en'], gpu=False,
                        model_storage_directory=OCR_MODEL_DIR,
                        download_enabled=False)

try:
    FONT = ImageFont.truetype(r"C:\Windows\Fonts\malgun.ttf", 16)
except Exception:
    FONT = ImageFont.load_default()


# === [2. 유틸 (미끼OCR검증.py와 동일)] ===
def load_image(path):
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"이미지를 읽을 수 없습니다: {path}")
    return img


def norm_text(s):
    return re.sub(r"[\s'\"‘’“”`]", "", s)


def ocr_region(img_bgr, crop):
    x1, y1, x2, y2 = crop
    roi = img_bgr[y1:y2, x1:x2]
    rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
    results = reader.readtext(rgb)
    out = []
    for box, text, conf in results:
        xs = [p[0] + x1 for p in box]
        ys = [p[1] + y1 for p in box]
        bx1, by1, bx2, by2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
        cx, cy = (bx1 + bx2) // 2, (by1 + by2) // 2
        out.append((text, norm_text(text), conf, (bx1, by1, bx2, by2), (cx, cy)))
    return out


def draw_results(img_bgr, items, out_path):
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    for text, ntext, conf, (x1, y1, x2, y2), (cx, cy) in items:
        if "하기" in ntext or "사용중" in ntext:
            color = (0, 200, 0)      # 초록: 사용하기/사용중 버튼
        elif "남음" in ntext or re.search(r"\d일|\d시간", ntext):
            color = (255, 140, 0)    # 주황: 남은 기간
        elif any(k in ntext for k in ("만료", "기간", "낚싯대", "페이지")):
            color = (255, 0, 0)      # 빨강: 핵심 키워드
        else:
            color = (0, 90, 255)     # 파랑: 일반 텍스트
        d.rectangle([x1, y1, x2, y2], outline=color, width=2)
        d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)
        d.text((x1, max(0, y1 - 20)), f"{text} ({conf:.2f})", font=FONT, fill=color)
    pil.save(out_path)
    print(f"  -> 주석 이미지 저장: {out_path}")


def print_table(items):
    print(f"  {'인식 텍스트':<24} {'conf':>5}  {'중심(cx,cy)':<14} bbox(x1,y1,x2,y2)")
    for text, ntext, conf, bbox, center in sorted(items, key=lambda r: (r[3][1], r[3][0])):
        print(f"  {text:<24} {conf:>5.2f}  {str(center):<14} {bbox}")


def suggest_region(bbox, margin_x=15, margin_y=8):
    x1, y1, x2, y2 = bbox
    return (x1 - margin_x, y1 - margin_y,
            (x2 - x1) + margin_x * 2, (y2 - y1) + margin_y * 2)


# === [3. 만료 팝업 검증: '! 아이템 기간 만료' + 아이템 이름] ===
def verify_expire_popup(fname):
    path = os.path.join(SHOT_DIR, fname)
    img = load_image(path)
    h, w = img.shape[:2]
    print(f"\n===== [만료 팝업] {fname} ({w}x{h}) =====")
    t0 = time.time()
    items = ocr_region(img, CROP_EXPIRE)
    print(f"  (OCR {time.time() - t0:.1f}초, {len(items)}건)")
    print_table(items)

    title = next((it for it in items if "만료" in it[1] and "기간" in it[1]), None)
    msg = next((it for it in items if "되었습니다" in it[1] or "만료되" in it[1]), None)
    name = next((it for it in items if "낚싯대" in it[1] or "낚시대" in it[1]), None)

    print("\n  --- 하드코딩용 후보 좌표 (FHD 기준) ---")
    if title:
        print(f"  '! 아이템 기간 만료' 타이틀: 중심 {title[4]}, bbox {title[3]}")
        print(f"    -> 감지용 OCR 영역 제안 REGION_ROD_EXPIRE = {suggest_region(title[3])}")
    else:
        print("  [실패] '기간 만료' 타이틀을 찾지 못했습니다.")
    if msg:
        print(f"  만료 안내문: 중심 {msg[4]}, bbox {msg[3]}")
    if name:
        print(f"  아이템 이름('테런 낚싯대'): 중심 {name[4]}, bbox {name[3]}")

    draw_results(img, items, os.path.join(OUT_DIR, f"expire_{fname}.png"))


# === [4. 보유 낚싯대 창 검증: 카드 그리드] ===
def verify_rod_window(fname):
    path = os.path.join(SHOT_DIR, fname)
    img = load_image(path)
    h, w = img.shape[:2]
    print(f"\n===== [보유 낚싯대] {fname} ({w}x{h}) =====")
    t0 = time.time()
    items = ocr_region(img, CROP_ROD)
    print(f"  (OCR {time.time() - t0:.1f}초, {len(items)}건)")
    print_table(items)

    page = next((it for it in items if re.search(r"\d+/\d+", it[1])), None)
    if page:
        m = re.search(r"(\d+)/(\d+)", page[1])
        print(f"\n  페이지 표시: {m.group(0)}  중심 {page[4]}, bbox {page[3]}")

    # 버튼: '사용하기'(오인식 '사럭하기' 포함 -> '하기' 매칭) 또는 '사용중'
    buttons = [it for it in items if "하기" in it[1] or "사용중" in it[1]]
    buttons.sort(key=lambda it: (it[4][1], it[4][0]))
    print(f"\n  --- 카드 요약 ({len(buttons)}칸 감지) ---")
    for i, btn in enumerate(buttons):
        bx, by = btn[4]
        state = "사용중" if "사용중" in btn[1] else "사용하기"
        same_col = [it for it in items
                    if it is not btn and abs(it[4][0] - bx) <= 70
                    and 10 <= by - it[4][1] <= 55]
        info = next((it for it in same_col if by - it[4][1] < 30), None)   # 남은기간/게이지행
        name = next((it for it in same_col if by - it[4][1] >= 30), None)  # 이름 바
        name_s = f"{name[0]} (conf {name[2]:.2f}, 중심 {name[4]})" if name else "?(게이지형)"
        info_s = info[1] if info else "(게이지 — 글자 없음)"
        print(f"  [{i}] 이름: {name_s}")
        print(f"      상태: {state} | 부가정보: {info_s} | 버튼 중심(클릭 좌표): ({bx}, {by})")

    draw_results(img, items, os.path.join(OUT_DIR, f"rod_{fname}.png"))
    return buttons


# === [5. 메인] ===
if __name__ == "__main__":
    for f in EXPIRE_IMGS:
        verify_expire_popup(f)

    all_buttons = []
    for f in ROD_IMGS:
        all_buttons.append(verify_rod_window(f))

    print("\n===== [미끼 창 좌표와 교차 검증] =====")
    print("  보유 미끼 창 실측 사용하기 버튼(참고):")
    print("    1행 (708,544) (874,543) (1038,540) (1209,541)")
    print("    2행 (714,702) (877,699) (1042,702) (1201,701)")
    for f, btns in zip(ROD_IMGS, all_buttons):
        coords = [(it[4], "사용중" if "사용중" in it[1] else "사용하기") for it in btns]
        print(f"  {f}: {coords}")
    print("  -> 좌표가 몇 px 이내로 일치하면 낚싯대 창도 동일 그리드 좌표 사용 가능")

    log_path = os.path.join(OUT_DIR, "낚싯대_결과.txt")
    with open(log_path, "w", encoding="utf-8") as fp:
        fp.writelines(_log_lines)
    _orig_print(f"\n[완료] 전체 결과 로그: {log_path}")
