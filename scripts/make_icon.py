"""Generate assets/icon.ico - a flat printer glyph in the app's accent color.

Run from the repo root:  python scripts/make_icon.py
Produces a multi-resolution .ico (16/32/48/64/128/256) used by the window and
the PyInstaller build. Re-runnable; overwrites the existing icon.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (46, 139, 87, 255)        # #2e8b57 sea green (theme.ACCENT)
ACCENT_DK = (33, 104, 65, 255)     # darker green for the slot / depth
PAPER = (250, 250, 250, 255)
PAPER_EDGE = (205, 210, 208, 255)
INK = (150, 160, 156, 255)         # faux printed text lines
LIGHT = (235, 245, 239, 255)       # status light

SIZE = 256


def _round(d, box, radius, **kw):
    d.rounded_rectangle(box, radius=radius, **kw)


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- paper feeding in at the top (behind the body) ---
    _round(d, (78, 18, 178, 104), 8, fill=PAPER, outline=PAPER_EDGE, width=3)
    for y in (40, 58, 76):
        d.line((96, y, 160, y), fill=INK, width=4)

    # --- printer body ---
    _round(d, (34, 96, 222, 182), 18, fill=ACCENT)
    # paper exit slot across the front
    _round(d, (70, 150, 186, 164), 6, fill=ACCENT_DK)
    # status light
    d.ellipse((190, 110, 206, 126), fill=LIGHT)

    # --- printed page coming out of the front ---
    _round(d, (66, 158, 190, 238), 8, fill=PAPER, outline=PAPER_EDGE, width=3)
    for y in (182, 200, 218):
        d.line((84, y, 172, y), fill=INK, width=4)

    return img


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    out = root / "assets" / "icon.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    base = build()
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(out, format="ICO", sizes=sizes)
    # Also emit a PNG preview for quick visual review.
    base.save(root / "assets" / "icon_preview.png", format="PNG")
    print(f"Wrote {out} ({', '.join(f'{w}x{h}' for w, h in sizes)})")


if __name__ == "__main__":
    main()
