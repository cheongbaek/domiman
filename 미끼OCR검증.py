# -*- coding: utf-8 -*-
# ============================================================
# 미끼 부족 팝업 · 보유 미끼 창 OCR 좌표 검증 스크립트
# ------------------------------------------------------------
# 스크린샷(1920x1080)을 읽어 easyocr로 글자 위치를 판독하고,
# 나중에 낚시.py에 하드코딩할 FHD 기준 좌표/영역을 뽑아낸다.
#  - 팝업: '미끼가 부족합니다' 메시지 영역, '보유 미끼' 버튼 중심
#  - 보유 미끼 창: 페이지 표시, 각 카드(이름/수량/사용하기 버튼) 좌표
# 결과: 콘솔 표 출력 + ocr_verify_result/ 폴더에 주석 그린 이미지 저장
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
        _orig_print(buf.getvalue().encode(sys.stdout.encoding or "cp949",
                                          "replace").decode(sys.stdout.encoding or "cp949"), end="")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHOT_DIR = r"C:\Users\windo\OneDrive - 한국교통대학교\문서\테일즈런너\스크린샷"

POPUP_IMGS = ["20260719015400.jpg"]  # 미끼 부족 팝업
BAIT_IMGS = [                        # 보유 미끼 창 (페이지 1~5)
    "20260719015418.jpg",
    "20260719015423.jpg",
    "20260719015425.jpg",
    "20260719015427.jpg",
    "20260719015429.jpg",
]

OUT_DIR = os.path.join(SCRIPT_DIR, "ocr_verify_result")
os.makedirs(OUT_DIR, exist_ok=True)

# OCR 판독 범위(전체 화면을 다 읽으면 느리고 채팅창 등 잡음이 섞이므로
# UI가 나오는 중앙부만 넉넉히 자른다. 좌표는 전체 이미지 기준으로 환산해 출력)
CROP_POPUP = (740, 430, 1180, 650)   # x1, y1, x2, y2
CROP_BAIT = (560, 330, 1360, 760)


# === [1. OCR 모델 로드 — 낚시.py와 동일 설정] ===
print("[시스템] OCR 모델을 불러오는 중입니다. 잠시만 기다려주세요...")
OCR_MODEL_DIR = os.path.join(SCRIPT_DIR, "ocr_model")
reader = easyocr.Reader(['ko', 'en'], gpu=False,
                        model_storage_directory=OCR_MODEL_DIR,
                        download_enabled=False)

# 한글 라벨용 폰트 (cv2.putText는 한글 불가 → PIL 사용)
try:
    FONT = ImageFont.truetype(r"C:\Windows\Fonts\malgun.ttf", 16)
except Exception:
    FONT = ImageFont.load_default()


# === [2. 유틸] ===
def load_image(path):
    """한글 경로 대응 이미지 로드 (BGR)"""
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"이미지를 읽을 수 없습니다: {path}")
    return img


def norm_text(s):
    """공백/따옴표 제거 — 낚시.py OCR 부분매칭 관례와 동일한 전처리"""
    return re.sub(r"[\s'\"‘’“”`]", "", s)


def ocr_region(img_bgr, crop):
    """crop 영역만 OCR 후, 전체 이미지 기준 좌표로 환산해 반환.
    반환: [(텍스트, 정규화텍스트, conf, (x1,y1,x2,y2), (cx,cy)), ...]"""
    x1, y1, x2, y2 = crop
    roi = img_bgr[y1:y2, x1:x2]
    rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
    results = reader.readtext(rgb)  # detail=1: (box, text, conf)
    out = []
    for box, text, conf in results:
        xs = [p[0] + x1 for p in box]
        ys = [p[1] + y1 for p in box]
        bx1, by1, bx2, by2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
        cx, cy = (bx1 + bx2) // 2, (by1 + by2) // 2
        out.append((text, norm_text(text), conf, (bx1, by1, bx2, by2), (cx, cy)))
    return out


def draw_results(img_bgr, items, out_path):
    """OCR 결과를 이미지에 그려 저장. 박스+중심점+텍스트 라벨."""
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    for text, ntext, conf, (x1, y1, x2, y2), (cx, cy) in items:
        if "사용하기" in ntext:
            color = (0, 200, 0)      # 초록: 사용하기 버튼
        elif re.fullmatch(r"\d+개?", ntext):
            color = (255, 140, 0)    # 주황: 수량
        elif any(k in ntext for k in ("부족", "보유", "구매", "페이지")):
            color = (255, 0, 0)      # 빨강: 핵심 키워드
        else:
            color = (0, 90, 255)     # 파랑: 이름 등 일반 텍스트
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
    """OCR용 인식 영역 제안: bbox에 여유를 더해 (x,y,w,h)로"""
    x1, y1, x2, y2 = bbox
    x, y = x1 - margin_x, y1 - margin_y
    w, h = (x2 - x1) + margin_x * 2, (y2 - y1) + margin_y * 2
    return (x, y, w, h)


# === [3. 팝업 검증: '미끼가 부족합니다' + '보유 미끼' 버튼] ===
def verify_popup(fname):
    path = os.path.join(SHOT_DIR, fname)
    img = load_image(path)
    h, w = img.shape[:2]
    print(f"\n===== [팝업] {fname} ({w}x{h}) =====")
    t0 = time.time()
    items = ocr_region(img, CROP_POPUP)
    print(f"  (OCR {time.time() - t0:.1f}초, {len(items)}건)")
    print_table(items)

    # 핵심 요소 추출 — 부분매칭 (OCR 오인식 대비, '부족'/'보유' 정도만 요구)
    msg = next((it for it in items if "부족" in it[1]), None)
    btn_own = next((it for it in items if "보유" in it[1]), None)
    btn_buy = next((it for it in items if "구매" in it[1]), None)

    print("\n  --- 하드코딩용 후보 좌표 (FHD 기준) ---")
    if msg:
        print(f"  '미끼가 부족합니다' 메시지: 중심 {msg[4]}, bbox {msg[3]}")
        print(f"    -> 감지용 OCR 영역 제안 REGION_NO_BAIT = {suggest_region(msg[3])}")
    else:
        print("  [실패] '부족' 텍스트를 찾지 못했습니다.")
    if btn_own:
        print(f"  '보유 미끼' 버튼: 클릭 좌표 후보 {btn_own[4]}  (bbox {btn_own[3]})")
    else:
        print("  [실패] '보유 미끼' 버튼을 찾지 못했습니다.")
    if btn_buy:
        print(f"  '미끼 구매' 버튼(참고): 중심 {btn_buy[4]}")

    draw_results(img, items, os.path.join(OUT_DIR, f"popup_{fname}.png"))


# === [4. 보유 미끼 창 검증: 카드 그리드 (이름/수량/사용하기)] ===
def verify_bait_window(fname):
    path = os.path.join(SHOT_DIR, fname)
    img = load_image(path)
    h, w = img.shape[:2]
    print(f"\n===== [보유 미끼] {fname} ({w}x{h}) =====")
    t0 = time.time()
    items = ocr_region(img, CROP_BAIT)
    print(f"  (OCR {time.time() - t0:.1f}초, {len(items)}건)")
    print_table(items)

    # 페이지 표시
    page = next((it for it in items if re.search(r"\d+/\d+", it[1])), None)
    if page:
        m = re.search(r"(\d+)/(\d+)", page[1])
        print(f"\n  페이지 표시: {m.group(0)}  중심 {page[4]}, bbox {page[3]}")
        print(f"    -> 페이지 OCR 영역 제안 REGION_BAIT_PAGE = {suggest_region(page[3])}")

    # '사용하기' 버튼 기준으로 카드 구성. OCR이 '사럭하기' 등으로 오인식하므로
    # '하기' 부분매칭(프로젝트 관례: 완전일치 금지). 카드 구조가 촘촘해서
    # 버튼 위 이름 dy≈38, 수량 dy≈21 → 열 ±70px, 위쪽 10~55px만 본다.
    buttons = [it for it in items if "하기" in it[1]]
    buttons.sort(key=lambda it: (it[4][1], it[4][0]))
    print(f"\n  --- 카드 요약 ({len(buttons)}칸 감지) ---")
    cards = []
    for i, btn in enumerate(buttons):
        bx, by = btn[4]
        same_col = [it for it in items
                    if it is not btn and abs(it[4][0] - bx) <= 70
                    and 10 <= by - it[4][1] <= 55]
        # 수량은 버튼 바로 위(dy<30), 이름은 그 위(dy>=30)
        count = next((it for it in same_col if by - it[4][1] < 30), None)
        name = next((it for it in same_col if by - it[4][1] >= 30), None)
        name_s = f"{name[0]} (conf {name[2]:.2f}, 중심 {name[4]}, bbox {name[3]})" if name else "?"
        count_s = count[1] if count else "?"
        print(f"  [{i}] 이름: {name_s}")
        print(f"      수량: {count_s} | 사용하기 버튼 중심(클릭 좌표): ({bx}, {by})")
        cards.append((btn, name, count))

    draw_results(img, items, os.path.join(OUT_DIR, f"bait_{fname}.png"))
    return buttons


# === [5. 메인] ===
if __name__ == "__main__":
    for f in POPUP_IMGS:
        verify_popup(f)

    all_buttons = []
    for f in BAIT_IMGS:
        all_buttons.append(verify_bait_window(f))

    # 페이지가 달라도 그리드 좌표가 동일한지 교차 검증
    print("\n===== [그리드 좌표 교차 검증] =====")
    print("  페이지별 '사용하기' 버튼 중심 좌표 (행 순서):")
    for f, btns in zip(BAIT_IMGS, all_buttons):
        coords = [it[4] for it in btns]
        print(f"  {f}: {coords}")
    print("  -> 페이지 간 좌표가 몇 px 이내로 일치하면 그리드 좌표로 하드코딩 가능")

    # 전 페이지 버튼 좌표를 모아 열/행 중심을 클러스터링 → 하드코딩 상수 제안
    xs = sorted(it[4][0] for btns in all_buttons for it in btns)
    ys = sorted(it[4][1] for btns in all_buttons for it in btns)

    def cluster(vals, tol=30):
        groups = []
        for v in vals:
            if groups and v - groups[-1][-1] <= tol:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [round(sum(g) / len(g)) for g in groups]

    col_x = cluster(xs)
    btn_y = cluster(ys)
    print("\n===== [하드코딩용 상수 제안 (FHD 기준)] =====")
    print(f"  BAIT_COL_X   = {col_x}   # 카드 열 중심 x (4열)")
    print(f"  BAIT_BTN_Y   = {btn_y}   # '사용하기' 버튼 중심 y (2행)")
    print(f"  BAIT_NAME_Y  = {[y - 38 for y in btn_y]}   # 이름 파란 바 중심 y (버튼 위 38px)")
    print("  # 카드(r행 c열)의 이름 OCR 영역: (BAIT_COL_X[c]-70, BAIT_NAME_Y[r]-12, 140, 24)")
    print("  # 카드(r행 c열)의 사용하기 클릭: (BAIT_COL_X[c], BAIT_BTN_Y[r])")

    # 결과 전체를 UTF-8 로그로 저장 (콘솔 인코딩 무관하게 원문 보존)
    log_path = os.path.join(OUT_DIR, "결과.txt")
    with open(log_path, "w", encoding="utf-8") as fp:
        fp.writelines(_log_lines)
    _orig_print(f"\n[완료] 전체 결과 로그: {log_path}")
