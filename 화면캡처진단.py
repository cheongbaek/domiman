"""
wgc_align_check.py — WGC 캡처를 1920x1080으로 축소하고
FHD REGION 좌표가 제대로 떨어지는지 사각형을 그려 검증한다.
"""
import cv2
import numpy as np

SRC = "wgc_capture.png"
OUT = "wgc_1080_check.png"

# 낚시.py의 FHD REGION 값들 (x, y, w, h)
REGIONS = {
    "Q_LEFT":      (742, 406, 78, 69),
    "Q_RIGHT":     (830, 407, 78, 69),
    "ANSWERS":     (972, 433, 226, 296),
    "VERIFY_TEXT": (832, 503, 255, 38),
}

# FHD 버튼 좌표 (점으로 표시)
POINTS = {
    "FISHING": (1086, 988),
    "TANK":    (1007, 1006),
    "MYROOM":  (1138, 245),
    "CONFIRM": (958, 575),
    "QTY":     (890, 1007),   # 살림망 수량
    "MINTIME": (975, 933),    # 최소 획득 시간
}

img = cv2.imread(SRC)
if img is None:
    raise SystemExit(f"{SRC} 를 열 수 없습니다.")

h, w = img.shape[:2]
print(f"원본: {w} x {h}")

resized = cv2.resize(img, (1920, 1080), interpolation=cv2.INTER_AREA)
print(f"축소: 1920 x 1080  (x배율 {w/1920:.4f}, y배율 {h/1080:.4f})")

for name, (x, y, rw, rh) in REGIONS.items():
    cv2.rectangle(resized, (x, y), (x + rw, y + rh), (0, 255, 0), 2)
    cv2.putText(resized, name, (x, y - 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

for name, (x, y) in POINTS.items():
    cv2.circle(resized, (x, y), 6, (0, 0, 255), -1)
    cv2.putText(resized, name, (x + 8, y + 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

cv2.imwrite(OUT, resized)
print(f"저장 완료: {OUT}")
print("→ 열어서 초록 박스가 물고기/보기, 빨간 점이 버튼 위에 정확히 떨어지는지 확인하세요.")