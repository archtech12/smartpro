"""Generate packaging/app.ico (multi-size). Run once; the .ico is committed.

    python packaging/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = size // 5
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=(16, 42, 67, 255))
    pad = size // 7
    draw.rounded_rectangle((pad, pad, size - 1 - pad, size - 1 - pad), radius=radius // 2, outline=(21, 94, 239, 255), width=max(2, size // 32))
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(size * 0.55))
    except OSError:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), "S", font=font)
    draw.text(((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]), "S", fill=(255, 255, 255, 255), font=font)
    return img


if __name__ == "__main__":
    out = Path(__file__).with_name("app.ico")
    render(256).save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", out)
