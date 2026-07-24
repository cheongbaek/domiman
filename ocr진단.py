"""
ocr_probe.py — easyocr이 화면 글씨를 어떻게 인식하는지 확인하는 진단 도구
"""
import os
import sys
import time
import numpy as np
import pyautogui
import easyocr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_MODEL_DIR = os.path.join(SCRIPT_DIR, "ocr_model")

print("[OCR 로딩 중...]")
reader = easyocr.Reader(['ko', 'en'], gpu=False,
                        model_storage_directory=OCR_MODEL_DIR,
                        download_enabled=True)
print("[준비 완료]\n")


def probe(region=None, save_crop=True):
    """
    region=(x, y, w, h) 또는 None(전체화면).
    인식된 텍스트/신뢰도/좌표를 출력하고, 캡처 이미지를 저장.
    """
    shot = pyautogui.screenshot(region=region)
    img = np.array(shot)

    if save_crop:
        fname = f"ocr_probe_{time.strftime('%H%M%S')}.png"
        shot.save(fname)
        print(f"[캡처 저장] {fname}  (region={region})")

    results = reader.readtext(img)  # detail=1 (기본): bbox, text, conf

    if not results:
        print("  (인식된 텍스트 없음)\n")
        return

    ox, oy = (region[0], region[1]) if region else (0, 0)
    print(f"  {'신뢰도':>6}  {'중앙좌표(절대)':>16}  텍스트")
    print("  " + "-" * 50)
    for bbox, text, conf in results:
        pts = np.array(bbox)
        cx = ox + int(pts[:, 0].mean())
        cy = oy + int(pts[:, 1].mean())
        # 공백/특수문자 눈에 보이게
        shown = repr(text)
        print(f"  {conf:6.2f}  ({cx:5d},{cy:5d})    {shown}")
    print()


if __name__ == "__main__":
    print("=== OCR 진단 도구 ===")
    print("사용법:")
    print("  Enter        → 3초 뒤 전체화면 인식")
    print("  x,y,w,h 입력 → 3초 뒤 해당 영역만 인식 (예: 832,503,255,38)")
    print("  q            → 종료\n")

    while True:
        cmd = input("region 입력 (Enter=전체 / q=종료): ").strip()
        if cmd.lower() == 'q':
            break

        region = None
        if cmd:
            try:
                parts = [int(v) for v in cmd.replace(" ", "").split(",")]
                if len(parts) == 4:
                    region = tuple(parts)
                else:
                    print("  x,y,w,h 형식으로 4개 값을 입력하세요.\n")
                    continue
            except ValueError:
                print("  숫자 4개를 콤마로 구분해 입력하세요.\n")
                continue

        print("  3초 뒤 캡처합니다... 게임 화면을 준비하세요.")
        time.sleep(3)
        probe(region)