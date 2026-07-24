import os
import tkinter as tk
from tkinter import filedialog

from PIL import Image

SIZE_OPTIONS = {
    "1": (16, 16),
    "2": (32, 32),
    "3": (48, 48),
    "4": (64, 64),
    "5": (128, 128),
    "6": (256, 256),
}


def select_image_file():
    root = tk.Tk()
    root.withdraw()
    path = filedialog.askopenfilename(
        title="변환할 이미지 파일 선택",
        filetypes=[
            ("이미지 파일", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
            ("모든 파일", "*.*"),
        ],
    )
    root.destroy()
    return path


def select_sizes():
    print("사용 가능한 크기:")
    for key, (w, h) in SIZE_OPTIONS.items():
        print(f"  {key}) {w}x{h}")
    print("  a) 전체 생성")

    choice = input("원하는 크기를 선택하세요 (쉼표로 여러 개 선택 가능, a=전체): ").strip().lower()

    if choice == "a":
        return list(SIZE_OPTIONS.values())

    sizes = []
    for part in choice.split(","):
        part = part.strip()
        if part in SIZE_OPTIONS:
            sizes.append(SIZE_OPTIONS[part])

    return sizes


def main():
    input_path = select_image_file()
    if not input_path:
        print("파일을 선택하지 않았습니다. 종료합니다.")
        return

    sizes = select_sizes()
    if not sizes:
        print("선택된 크기가 없습니다. 종료합니다.")
        return

    img = Image.open(input_path).convert("RGBA")

    base, _ = os.path.splitext(input_path)
    output_path = base + ".ico"

    img.save(output_path, format="ICO", sizes=sizes)

    print(f"변환 완료: {output_path}")
    print(f"생성된 크기: {sizes}")


if __name__ == "__main__":
    main()
