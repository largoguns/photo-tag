"""Generate the PWA icons in static/img/icons/ (run once: python scripts/make_icons.py)."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "img" / "icons"
ACCENT = (31, 95, 191)
WHITE = (255, 255, 255)
SCALE = 4  # draw large and downsample for smooth edges


def draw_symbol(draw, box):
    """A stylised photo (mountains + sun) with a small tag, inside `box`."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    stroke = round(w * 0.06)
    radius = round(w * 0.08)
    # Photo frame.
    frame = (x0 + w * 0.06, y0 + h * 0.16, x1 - w * 0.14, y1 - h * 0.12)
    draw.rounded_rectangle(frame, radius=radius, outline=WHITE, width=stroke)
    fx0, fy0, fx1, fy1 = frame
    fw, fh = fx1 - fx0, fy1 - fy0
    # Sun and mountains.
    sun_r = fw * 0.09
    sun_c = (fx0 + fw * 0.30, fy0 + fh * 0.32)
    draw.ellipse(
        (sun_c[0] - sun_r, sun_c[1] - sun_r, sun_c[0] + sun_r, sun_c[1] + sun_r), fill=WHITE
    )
    base = fy1 - stroke * 1.4
    draw.polygon(
        [(fx0 + stroke * 1.4, base), (fx0 + fw * 0.40, fy0 + fh * 0.48), (fx0 + fw * 0.62, base)],
        fill=WHITE,
    )
    draw.polygon(
        [(fx0 + fw * 0.42, base), (fx0 + fw * 0.66, fy0 + fh * 0.40), (fx1 - stroke * 1.4, base)],
        fill=WHITE,
    )
    # Tag in the bottom-right corner, overlapping the frame.
    tx, ty, ts = x1 - w * 0.30, y1 - h * 0.36, w * 0.30
    tag = [
        (tx + ts * 0.30, ty),
        (tx + ts, ty),
        (tx + ts, ty + ts * 0.70),
        (tx + ts * 0.30, ty + ts * 0.70),
        (tx, ty + ts * 0.35),
    ]
    draw.polygon(tag, fill=WHITE, outline=ACCENT, width=stroke // 2)
    hole_r = ts * 0.08
    hole_c = (tx + ts * 0.30, ty + ts * 0.35)
    draw.ellipse(
        (hole_c[0] - hole_r, hole_c[1] - hole_r, hole_c[0] + hole_r, hole_c[1] + hole_r),
        fill=ACCENT,
    )


def icon(size, *, maskable=False, rounded=True):
    big = size * SCALE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if maskable or not rounded:
        draw.rectangle((0, 0, big, big), fill=ACCENT)
    else:
        draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=round(big * 0.22), fill=ACCENT)
    # Maskable icons keep the symbol inside the central 80% safe zone.
    margin = big * (0.22 if maskable else 0.14)
    draw_symbol(draw, (margin, margin, big - margin, big - margin))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def badge(size):
    """Monochrome notification badge (Android shows only the alpha channel)."""
    big = size * SCALE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = big * 0.08
    draw_symbol(draw, (margin, margin, big - margin, big - margin))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    icon(192).save(OUT / "icon-192.png")
    icon(512).save(OUT / "icon-512.png")
    icon(512, maskable=True).save(OUT / "icon-maskable-512.png")
    # iOS ignores transparency and adds its own rounding.
    icon(180, rounded=False).convert("RGB").save(OUT / "apple-touch-icon.png")
    icon(32).save(OUT / "favicon-32.png")
    badge(96).save(OUT / "badge-96.png")
    print(f"Icons written to {OUT}")


if __name__ == "__main__":
    main()
