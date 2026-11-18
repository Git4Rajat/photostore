#!/usr/bin/env python3
"""Regenerate the Keepsake brand PNGs (icon-192, icon-512, apple-touch-icon, og-image)
from the mark in docs/design-mockups/keepsake-redesign.html (#i-keep, drawn on --accent).

    python3 frontend/scripts/generate-brand-icons.py

Needs Pillow and macOS's Futura (the redesign's display-font fallback) for og-image.
favicon.svg is hand-written next to these and is the source of the colours below.
"""
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

PUBLIC = Path(__file__).resolve().parent.parent / 'public'
ACCENT, ON_ACCENT = (0x2f, 0x5d, 0x82), (255, 255, 255)
BG, INK, MUTED = (0xf5, 0xf1, 0xea), (0x1d, 0x1a, 0x16), (0x66, 0x5f, 0x54)
SS = 4  # supersampling factor


def _arc(p0, p1, r):
    """Points along an SVG small arc with sweep-flag 1 (angle increasing, y down)."""
    mx, my, dx, dy = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2, p1[0] - p0[0], p1[1] - p0[1]
    d = math.hypot(dx, dy)
    h = math.sqrt(max(r * r - d * d / 4, 0))
    for sign in (1, -1):
        cx, cy = mx - sign * h * dy / d, my + sign * h * dx / d
        a0, a1 = math.atan2(p0[1] - cy, p0[0] - cx), math.atan2(p1[1] - cy, p1[0] - cx)
        span = (a1 - a0) % (2 * math.pi)
        if span < math.pi:
            return [(cx + r * math.cos(a0 + span * t / 24), cy + r * math.sin(a0 + span * t / 24)) for t in range(25)]
    raise ValueError('no arc')


def _cubic(p0, c1, c2, p1):
    return [tuple((1 - t) ** 3 * p0[i] + 3 * (1 - t) ** 2 * t * c1[i] + 3 * (1 - t) * t * t * c2[i] + t ** 3 * p1[i]
                  for i in (0, 1)) for t in (k / 24 for k in range(25))]


def heart():
    """#i-keep in a 24x24 box: M12 19.5s-7-4.3-7-9.4A3.9 3.9 0 0 1 12 7.8a3.9 3.9 0 0 1 7 2.3c0 5.1-7 9.4-7 9.4z"""
    return (_cubic((12, 19.5), (12, 19.5), (5, 15.2), (5, 10.1)) + _arc((5, 10.1), (12, 7.8), 3.9)
            + _arc((12, 7.8), (19, 10.1), 3.9) + _cubic((19, 10.1), (19, 15.2), (12, 19.5), (12, 19.5)))


def draw_mark(img, x, y, size, radius, glyph_scale):
    """Draw the mark (rounded square + heart) with its box at x, y on a supersampled image."""
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x, y, x + size, y + size], radius=radius, fill=ACCENT)
    s = size * glyph_scale / 24
    ox, oy = x + (size - 24 * s) / 2, y + (size - 24 * s) / 2
    d.polygon([(ox + px * s, oy + py * s) for px, py in heart()], fill=ON_ACCENT)


def square(px, name):
    img = Image.new('RGB', (px * SS, px * SS), ACCENT)  # full bleed: the OS masks it
    draw_mark(img, 0, 0, px * SS, 0, 0.6 * 1.0)
    img.resize((px, px), Image.LANCZOS).save(PUBLIC / name, optimize=True)


def og():
    w, h = 1200, 630
    img = Image.new('RGB', (w * SS, h * SS), BG)
    draw_mark(img, (w // 2 - 72) * SS, 96 * SS, 144 * SS, 45 * SS, 0.56)
    d = ImageDraw.Draw(img)
    fut = '/System/Library/Fonts/Supplemental/Futura.ttc'
    for text, y, size, idx, fill in (('Keepsake', 360, 112, 2, INK), ('Your own private photo library', 462, 36, 0, MUTED),
                                    ('Your memories stay yours.', 514, 30, 2, ACCENT)):
        font = ImageFont.truetype(fut, size * SS, index=idx)
        d.text((w * SS / 2, y * SS), text, font=font, fill=fill, anchor='ms')
    img.resize((w, h), Image.LANCZOS).save(PUBLIC / 'og-image.png', optimize=True)


if __name__ == '__main__':
    square(192, 'icon-192.png')
    square(512, 'icon-512.png')
    square(180, 'apple-touch-icon.png')
    og()
