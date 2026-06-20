"""
generate_icon.py
================
Creates assets/icon.ico with multiple embedded sizes (16, 24, 32, 48, 64,
128, 256 px) matching the InvoiceIQ brand — blue rounded square with invoice
lines and a green smart-check badge.

Run once before building:  python generate_icon.py
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

SIZES   = [16, 24, 32, 48, 64, 128, 256]
OUT_DIR = Path(__file__).parent / 'assets'
OUT_ICO = OUT_DIR / 'icon.ico'

# Brand colours
BLUE    = (59,  130, 246, 255)   # --accent  #3b82f6
BLUE_D  = (29,   78, 216, 255)   # darker variant
GREEN   = (16,  185, 129, 255)   # emerald   #10b981
WHITE   = (255, 255, 255, 255)
W75     = (255, 255, 255, 191)   # white 75 %
W50     = (255, 255, 255, 128)   # white 50 %
TRANSP  = (0,   0,   0,   0)


def _rounded_rect(draw: ImageDraw.Draw, xy, radius, fill):
    """Draw a filled rounded rectangle compatible with older Pillow builds."""
    try:
        draw.rounded_rectangle(xy, radius=radius, fill=fill)
    except AttributeError:
        # Pillow < 8.2 — fall back to a plain rect
        draw.rectangle(xy, fill=fill)


def _draw_frame(size: int) -> Image.Image:
    img  = Image.new('RGBA', (size, size), TRANSP)
    draw = ImageDraw.Draw(img)

    r = max(2, size // 8)                   # corner radius
    _rounded_rect(draw, [0, 0, size - 1, size - 1], r, BLUE)

    # ── Invoice row lines ──────────────────────────────────────────────────
    lh    = max(1, size // 12)              # line height
    lx0   = size // 5                       # left margin
    widths = [int(size * 0.6), int(size * 0.43), int(size * 0.3)]
    starts = [int(size * 0.27), int(size * 0.44), int(size * 0.61)]
    alphas = [WHITE, W75, W50]

    for y, w, col in zip(starts, widths, alphas):
        _rounded_rect(draw, [lx0, y, lx0 + w, y + lh], max(1, lh // 2), col)

    # ── Smart-check badge (bottom-right circle + tick) ────────────────────
    if size >= 16:
        # Badge circle — scale radius to ~30 % of icon
        br    = max(3, int(size * 0.215))
        bcx   = size - br - max(0, size // 12)
        bcy   = size - br - max(0, size // 12)
        draw.ellipse([bcx - br, bcy - br, bcx + br, bcy + br], fill=GREEN)

        if size >= 24:
            # Tick mark
            tk   = br * 0.55
            p1   = (bcx - tk * 0.55, bcy)
            p2   = (bcx - tk * 0.05, bcy + tk * 0.5)
            p3   = (bcx + tk * 0.65, bcy - tk * 0.6)
            tw   = max(1, size // 20)
            draw.line([p1, p2, p3], fill=WHITE, width=tw)

    return img


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    frames = [_draw_frame(s) for s in SIZES]

    frames[0].save(
        str(OUT_ICO),
        format='ICO',
        sizes=[(s, s) for s in SIZES],
        append_images=frames[1:],
    )
    print(f'Icon written: {OUT_ICO}')

    out_png = OUT_DIR / 'icon_256.png'
    frames[-1].save(str(out_png), format='PNG')
    print(f'PNG  written: {out_png}')


if __name__ == '__main__':
    main()
