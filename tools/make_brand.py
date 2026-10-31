"""Generate the in-repo brand images.

Home Assistant 2026.3 and later serve custom_components/<domain>/brand/.

    icon.png       256x256
    icon@2x.png    512x512
    logo.png       512x256
    logo@2x.png    1024x512

The icons use brand/icon-source.png. The wide logos retain the lock and
routed connections.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

BG = (0, 0, 0, 0)
PAD_FRAC = 0.06
MARK_H_FRAC = 0.80
LOCK = (14, 165, 233, 255)
SHACKLE = (100, 116, 139, 255)
KEYHOLE = (240, 249, 255, 255)
ROUTE = (100, 116, 139, 255)
UPSTREAM = (245, 176, 66, 255)


def draw(size: tuple[int, int], pad_frac: float) -> Image.Image:
    w, h = size
    img = Image.new("RGBA", size, BG)
    d = ImageDraw.Draw(img)
    side = min(w, h)
    u = side - 2 * side * pad_frac
    x0 = (w - u) / 2
    y0 = (h - u * MARK_H_FRAC) / 2
    line = max(2, int(u * 0.05))

    def p(x: float, y: float) -> tuple[float, float]:
        return (x0 + x * u, y0 + y * u)

    # Shackle: the top half of an ellipse and two legs into the body.
    d.arc([*p(0.23, 0.0), *p(0.49, 0.30)], start=180, end=360, fill=SHACKLE, width=line)
    leg = line / 2 / u
    d.line([p(0.23 + leg, 0.15), p(0.23 + leg, 0.36)], fill=SHACKLE, width=line)
    d.line([p(0.49 - leg, 0.15), p(0.49 - leg, 0.36)], fill=SHACKLE, width=line)

    # Body and keyhole.
    d.rounded_rectangle([*p(0.14, 0.34), *p(0.58, 0.80)], radius=u * 0.06, fill=LOCK)
    d.ellipse([*p(0.32, 0.47), *p(0.40, 0.55)], fill=KEYHOLE)
    d.rectangle([*p(0.345, 0.53), *p(0.375, 0.67)], fill=KEYHOLE)

    # One route in, three out to upstreams.
    mid = 0.52
    d.line([p(0.0, mid), p(0.14, mid)], fill=ROUTE, width=line)
    r = line * 1.3 / u
    junction = p(0.66, mid)
    d.line([p(0.58, mid), junction], fill=ROUTE, width=line)
    for y in (0.30, mid, 0.74):
        end = p(1.0 - r, y)
        d.line([junction, end], fill=ROUTE, width=line)
        d.ellipse(
            [end[0] - r * u, end[1] - r * u, end[0] + r * u, end[1] + r * u],
            fill=UPSTREAM,
        )
    return img


def main() -> None:
    out = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
        "sslreverseproxy",
        "brand",
    )
    os.makedirs(out, exist_ok=True)
    specs = {
        "icon.png": (256, 256),
        "icon@2x.png": (512, 512),
        "logo.png": (512, 256),
        "logo@2x.png": (1024, 512),
    }
    source = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "brand", "icon-source.png"
    )
    with Image.open(source) as original:
        approved_icon = original.convert("RGBA")
    for name, size in specs.items():
        image = (
            approved_icon.resize(size, Image.LANCZOS)
            if name.startswith("icon")
            else draw(size, pad_frac=PAD_FRAC)
        )
        image.save(os.path.join(out, name), "PNG")

    # Checked against the rules: size, transparent corners, a mark that fills.
    ok = True
    for name, (want_w, want_h) in specs.items():
        with Image.open(os.path.join(out, name)) as im:
            w, h = im.size
            alpha = im.convert("RGBA").getchannel("A")
        good = (w, h) == (want_w, want_h)
        corners = [
            alpha.getpixel(c) for c in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
        ]
        transparent = all(a == 0 for a in corners)
        left, top, right, bottom = alpha.getbbox() or (0, 0, 0, 0)
        box = min(w, h) * (1 - 2 * PAD_FRAC)
        filled = (right - left) >= box - 2 and (bottom - top) >= box * MARK_H_FRAC - 2
        print(f"{name:12s} {w}x{h} transparent={transparent} filled={filled}")
        ok &= good and transparent and filled
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
