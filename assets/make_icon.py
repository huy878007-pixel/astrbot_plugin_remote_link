#!/usr/bin/env python3
"""云信互联：把品牌原图生成 EXE 图标（.ico）与插件 Logo（logo.png）。

用法:
    python assets/make_icon.py <原图路径>
输出:
    assets/yunxin.ico    多尺寸 Windows 图标（16~256px）
    logo.png             256x256 插件 Logo（AstrBot 插件市场显示）
"""

import sys
from pathlib import Path

from PIL import Image, ImageChops

ICON_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main():
    src_path = Path(sys.argv[1] if len(sys.argv) > 1 else "Codex 图像 2026年8月15日 18_55_35.png")
    here = Path(__file__).resolve().parent
    project_root = here.parent

    img = Image.open(src_path).convert("RGBA")

    # 1. 裁掉纯白边距（只裁四周，不动画面内部）
    diff = ImageChops.difference(img.convert("RGB"), Image.new("RGB", img.size, (255, 255, 255)))
    bbox = diff.getbbox()
    if bbox:
        pad = max(2, int(min(img.size) * 0.02))
        left = max(0, bbox[0] - pad)
        top = max(0, bbox[1] - pad)
        right = min(img.width, bbox[2] + pad)
        bottom = min(img.height, bbox[3] + pad)
        img = img.crop((left, top, right, bottom))

    # 2. 居中裁成正方形
    w, h = img.size
    side = min(w, h)
    cx, cy = (w - side) // 2, (h - side) // 2
    img = img.crop((cx, cy, cx + side, cy + side))

    # 3. 生成插件 Logo（256x256 PNG）
    logo = img.copy()
    logo.thumbnail((256, 256), Image.LANCZOS)
    logo_path = project_root / "logo.png"
    logo.save(logo_path)
    print(f"logo  -> {logo_path} ({logo.size[0]}x{logo.size[1]})")

    # 4. 生成多尺寸 ICO
    ico_path = here / "yunxin.ico"
    img.save(ico_path, format="ICO", sizes=ICON_SIZES)
    print(f"ico   -> {ico_path}")


if __name__ == "__main__":
    main()
