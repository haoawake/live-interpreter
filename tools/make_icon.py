"""Draws assets/icon.ico (+ icon.png preview). Run: .venv\\Scripts\\python.exe tools\\make_icon.py

    --mac   only write assets/icon-mac.png, the 1024 px master build.py turns into the
            macOS .icns (drawn here because the fonts are Windows ones)
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")
FONTS = r"C:\Windows\Fonts"
TOP_LEFT, BOTTOM_RIGHT = (37, 99, 235), (124, 58, 237)  # blue -> violet
INK = (79, 70, 229)


def _gradient(size):
    t = np.add.outer(np.arange(size), np.arange(size)) / (2 * size - 2)  # 0 at top-left, 1 at bottom-right
    rgb = np.array(TOP_LEFT) * (1 - t[..., None]) + np.array(BOTTOM_RIGHT) * t[..., None]
    return Image.fromarray(np.dstack([rgb, np.full((size, size), 255)]).astype(np.uint8), "RGBA")


def _tile(size, inset, radius):
    """Gradient rounded square on a transparent canvas."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((inset, inset, size - inset, size - inset), radius, fill=255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(_gradient(size), (0, 0), mask)
    return img


def _center_text(draw, box, text, font, fill):
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    x = (box[0] + box[2]) / 2 - (l + r) / 2
    y = (box[1] + box[3]) / 2 - (t + b) / 2
    draw.text((x, y), text, font=font, fill=fill)


def detailed(size=1024):
    """Two chat bubbles: a translucent "A" behind a white "译"."""
    img = _tile(size, 56, 210)
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    back = (150, 170, 640, 560)
    d.rounded_rectangle(back, 120, fill=(255, 255, 255, 90))
    d.polygon([(220, 520), (200, 660), (340, 540)], fill=(255, 255, 255, 90))
    _center_text(d, (back[0], back[1] - 20, back[2] - 110, back[3] - 100), "A",
                 ImageFont.truetype(os.path.join(FONTS, "segoeuib.ttf"), 250), (255, 255, 255, 255))
    img = Image.alpha_composite(img, over)
    d = ImageDraw.Draw(img)
    front = (390, 400, 874, 830)
    d.rounded_rectangle(front, 130, fill=(255, 255, 255, 255))
    d.polygon([(800, 780), (850, 920), (700, 815)], fill=(255, 255, 255, 255))
    _center_text(d, front, "译", ImageFont.truetype(os.path.join(FONTS, "msyhbd.ttc"), 300), INK)
    return img


def simple(size=512):
    """Just a bold 译: the bubbles turn to mush at 16-32 px."""
    img = _tile(size, 16, 110)
    _center_text(ImageDraw.Draw(img), (0, 0, size, size), "译",
                 ImageFont.truetype(os.path.join(FONTS, "msyhbd.ttc"), 360), (255, 255, 255, 255))
    return img


def mac(size=1024):
    """Apple's icon grid: the tile is 824 of 1024 px with a soft shadow below it, so the
    icon sits the same size as the system's own in the Dock and Finder."""
    from PIL import ImageFilter

    body = round(size * 824 / 1024)
    tile = detailed(1024).resize((round(body * 1024 / 912),) * 2, Image.LANCZOS)  # detailed() is 912 px of 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    off = (size - tile.width) // 2
    alpha = tile.getchannel("A").point(lambda a: a * 0.32)
    shadow.paste((0, 0, 0, 255), (off, off + round(size * 0.012)), alpha)
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(size * 0.012)))
    img.alpha_composite(tile, (off, off))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    if "--mac" in sys.argv:
        mac().save(os.path.join(OUT, "icon-mac.png"))
        print("wrote", os.path.join(OUT, "icon-mac.png"))
        return
    big, small = detailed(), simple()
    # 20/40/56/72/96 are what Windows asks for at 125-300% display scaling
    sizes = (256, 128, 96, 72, 64, 56, 48, 40, 32, 24, 20, 16)
    frames = {s: (big if s >= 48 else small).resize((s, s), Image.LANCZOS) for s in sizes}
    frames[256].save(os.path.join(OUT, "icon.ico"), sizes=[(s, s) for s in frames],
                     append_images=[frames[s] for s in frames if s != 256])
    frames[256].save(os.path.join(OUT, "icon.png"))
    print("wrote", os.path.join(OUT, "icon.ico"))


if __name__ == "__main__":
    main()
