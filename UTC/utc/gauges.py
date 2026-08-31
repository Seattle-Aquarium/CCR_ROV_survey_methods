"""
Compass rose and tilt (attitude) indicator.

Drawn with Pillow at a supersampled scale and reduced with LANCZOS -- Pillow has
no anti-aliased primitives, and at the sizes these render, aliasing on the dial
ring and needle is very visible.

Conventions, stated because sign errors here are invisible until someone
notices the horizon is backwards:

* **Heading** -- the card rotates so the current heading sits under a fixed
  index at the top, the way a real compass card reads.
* **Roll** is positive right-side-down (ArduPilot/aerospace). An attitude
  indicator shows what the pilot sees, so rolling right makes the world appear
  rotated counter-clockwise and the sky moves to the LEFT. Check the limit case:
  at roll +90 the right side points at the seabed, so the sky must be on the
  left.
* **Pitch** is positive nose-up, which slides the horizon DOWN and shows more
  sky.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from PIL import Image, ImageDraw, ImageFont

from . import brand
from .config import Layout

SS = 3          # supersampling factor


def _font(size: int, weight: str = "medium") -> ImageFont.FreeTypeFont:
    p = brand.font_path(weight)
    if p:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _rgba(color: str, alpha: float | None = None):
    """'#RRGGBB' or '#RRGGBBAA' -> tuple, with optional alpha override."""
    c = color.lstrip("#")
    if len(c) == 8:
        r, g, b, a = (int(c[i:i + 2], 16) for i in (0, 2, 4, 6))
    else:
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        a = 255
    if alpha is not None:
        a = max(0, min(255, int(round(alpha * 255))))
    return (r, g, b, a)


def _bearing_xy(cx: float, cy: float, r: float, bearing_deg: float) -> tuple[float, float]:
    """Screen point at a bearing measured clockwise from straight up.

    Pillow's y axis grows downward, so 'up' is -y.
    """
    a = math.radians(bearing_deg)
    return (cx + r * math.sin(a), cy - r * math.cos(a))


def _circle_points(cx: float, cy: float, r: float, n: int = 360):
    return [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


# --------------------------------------------------------------------------
#  Compass
# --------------------------------------------------------------------------


def draw_compass(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
                 heading: float | None, cfg: Layout) -> None:
    hd = 0.0 if heading is None else float(heading)

    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              fill=_rgba(cfg.gauge_face), outline=_rgba(cfg.gauge_fg), width=int(2 * SS))

    for b in range(0, 360, 15):
        scr = b - hd
        major = (b % 90) == 0
        inner = r * (0.78 if major else (0.84 if b % 45 == 0 else 0.88))
        p1 = _bearing_xy(cx, cy, r * 0.96, scr)
        p2 = _bearing_xy(cx, cy, inner, scr)
        d.line([p1, p2],
               fill=_rgba(cfg.gauge_fg if major else cfg.gauge_dim),
               width=int((2.5 if major else 1.2) * SS))

    f = _font(int(r * 0.30), "bold")
    for i, lab in enumerate(("N", "E", "S", "W")):
        p = _bearing_xy(cx, cy, r * 0.58, i * 90 - hd)
        col = _rgba(cfg.gauge_north if i == 0 else cfg.gauge_fg)
        d.text(p, lab, font=f, fill=col, anchor="mm")

    # north needle, so the card's orientation reads at a glance
    np_ = _bearing_xy(cx, cy, r * 0.42, -hd)
    d.line([(cx, cy), np_], fill=_rgba(cfg.gauge_north), width=int(3 * SS))
    rr = 0.05 * r
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=_rgba(cfg.gauge_fg))

    # fixed index at the top
    w = r * 0.10
    d.polygon([(cx - w, cy - r * 1.02), (cx + w, cy - r * 1.02), (cx, cy - r * 0.86)],
              fill=_rgba(cfg.gauge_index))


# --------------------------------------------------------------------------
#  Tilt / attitude
# --------------------------------------------------------------------------


def draw_tilt(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
              pitch: float | None, roll: float | None, cfg: Layout) -> None:
    p = 0.0 if pitch is None else float(pitch)
    rl = 0.0 if roll is None else float(roll)

    a = math.radians(rl)
    # Screen y grows downward, so both direction vectors have their y negated
    # relative to the textbook (y-up) form.
    #   roll 0   -> n = (0, -1), sky straight up.
    #   roll +90 -> n = (-1, 0), sky to the LEFT, which is the check case.
    u = (math.cos(a), -math.sin(a))          # along the horizon
    n = (-math.sin(a), -math.cos(a))         # normal, pointing skyward
    d_off = p * cfg.gauge_pitch_px * SS      # nose up -> horizon slides down
    p0 = (cx - n[0] * d_off, cy - n[1] * d_off)

    pts = _circle_points(cx, cy, r, 360)
    side = [((x - p0[0]) * n[0] + (y - p0[1]) * n[1]) for x, y in pts]

    def half(keep: Sequence[bool]):
        idx = [i for i, k in enumerate(keep) if k]
        if not idx:
            return None
        gaps = [j for j in range(1, len(idx)) if idx[j] - idx[j - 1] > 1]
        if gaps:
            g = gaps[0]
            idx = idx[g:] + idx[:g]
        return [pts[i] for i in idx]

    sky = half([s >= 0 for s in side])
    gnd = half([s < 0 for s in side])
    if sky:
        d.polygon(sky, fill=_rgba(cfg.gauge_sky, cfg.gauge_sky_alpha))
    if gnd:
        d.polygon(gnd, fill=_rgba(cfg.gauge_ground, cfg.gauge_ground_alpha))

    if abs(d_off) < r:
        h = math.sqrt(max(0.0, r * r - d_off * d_off))
        i1 = (p0[0] + u[0] * h, p0[1] + u[1] * h)
        i2 = (p0[0] - u[0] * h, p0[1] - u[1] * h)
        d.line([i1, i2], fill=_rgba(cfg.gauge_fg), width=int(2 * SS))

    for k in (-20, -10, 10, 20):
        dd = (p - k) * cfg.gauge_pitch_px * SS
        if abs(dd) > r * 0.8:
            continue
        q = (cx - n[0] * dd, cy - n[1] * dd)
        ln = r * (0.30 if k % 20 == 0 else 0.18)
        d.line([(q[0] - u[0] * ln, q[1] - u[1] * ln),
                (q[0] + u[0] * ln, q[1] + u[1] * ln)],
               fill=_rgba(cfg.gauge_dim), width=int(1.2 * SS))

    # bank scale rides on the card, so the tick under the fixed index reads bank
    for b in (-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60):
        major = b in (-60, -30, 0, 30, 60)
        p1 = _bearing_xy(cx, cy, r, b - rl)
        p2 = _bearing_xy(cx, cy, r * (0.84 if major else 0.90), b - rl)
        d.line([p1, p2], fill=_rgba(cfg.gauge_fg if major else cfg.gauge_dim),
               width=int((2.2 if major else 1.1) * SS))

    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              outline=_rgba(cfg.gauge_fg), width=int(2 * SS))

    # fixed reference symbol, with a dark underlay so it reads over sky or ground
    w, inner = r * 0.46, r * 0.15
    for col, wid in ((_rgba("#000000", 0.6), 7), (_rgba(cfg.gauge_index), 3.5)):
        d.line([(cx - w, cy), (cx - inner, cy)], fill=col, width=int(wid * SS))
        d.line([(cx + inner, cy), (cx + w, cy)], fill=col, width=int(wid * SS))
        d.line([(cx - inner, cy), (cx - inner, cy + r * 0.09)], fill=col, width=int(wid * SS))
        d.line([(cx + inner, cy), (cx + inner, cy + r * 0.09)], fill=col, width=int(wid * SS))
    rr = 0.04 * r
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=_rgba(cfg.gauge_index))

    tw = r * 0.075
    d.polygon([(cx - tw, cy - r), (cx + tw, cy - r), (cx, cy - r * 0.86)],
              fill=_rgba(cfg.gauge_index))


# --------------------------------------------------------------------------
#  The gauge box
# --------------------------------------------------------------------------


def _fmt(v, digits: int = 0, suffix: str = "") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "--"
    return f"{v:.{digits}f}{suffix}"


def captions(vals: dict) -> tuple[str, str]:
    return (
        f"HDG {_fmt(vals.get('heading'), 0, chr(176))}",
        f"P {_fmt(vals.get('pitch'), 1, chr(176))}  R {_fmt(vals.get('roll'), 1, chr(176))}",
    )


#: widest captions the dials can show, for sizing the box once
CAPTION_SAMPLE = ("HDG 359°", "P -20.0°  R -30.0°")


def gauge_size(cfg: Layout) -> tuple[int, int]:
    """Box size in final pixels. Height matches the ROV inset beside it."""
    f = _font(cfg.text_size, "medium")
    dummy = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    capw = max(int(dummy.textlength(c, font=f)) for c in CAPTION_SAMPLE)
    w = max(cfg.gauge_diam, capw) + 2 * cfg.gauge_pad
    h = cfg.inset_height()
    return int(w), int(h)


def render_gauges(vals: dict, cfg: Layout) -> Image.Image:
    """One RGBA image holding both dials stacked, with captions."""
    w, h = gauge_size(cfg)
    img = Image.new("RGBA", (w * SS, h * SS), _rgba(cfg.panel_bg, cfg.panel_bg_alpha))
    d = ImageDraw.Draw(img)

    diam = cfg.gauge_diam * SS
    cap_h = cfg.gauge_caption_h * SS
    pad = cfg.gauge_pad * SS
    r = diam / 2

    content = 2 * diam + 2 * cap_h + pad
    top = (h * SS - content) / 2
    cx = w * SS / 2
    cy1 = top + r
    cy2 = top + diam + cap_h + pad + r

    draw_compass(d, cx, cy1, r, vals.get("heading"), cfg)
    draw_tilt(d, cx, cy2, r, vals.get("pitch"), vals.get("roll"), cfg)

    f = _font(cfg.text_size * SS, "medium")
    c1, c2 = captions(vals)
    d.text((cx, cy1 + r + cap_h * 0.48), c1, font=f, fill=_rgba(cfg.panel_fg), anchor="mm")
    d.text((cx, cy2 + r + cap_h * 0.48), c2, font=f, fill=_rgba(cfg.panel_fg), anchor="mm")

    img = img.resize((w, h), Image.LANCZOS)
    return _with_border(img, cfg)


def _with_border(img: Image.Image, cfg: Layout) -> Image.Image:
    if cfg.border_px <= 0:
        return img
    b = cfg.border_px
    out = Image.new("RGBA", (img.width + 2 * b, img.height + 2 * b), _rgba(cfg.border_color))
    out.paste(img, (b, b))
    return out
