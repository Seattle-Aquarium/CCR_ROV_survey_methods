"""
Drawing the brand gradients.

CustomTkinter has no gradient fill -- every colour it takes is flat. So the
gradients are rendered to bitmaps here and drawn onto a `tk.Canvas`, which is
the one surface in Tk that lets text and widgets sit over an image with no
rectangle of their own behind them. A CTkLabel holding the image cannot do
that: a child label's "transparent" fill resolves to its master's flat
*colour*, which paints a visible block across the gradient.

The geometry follows the spec diagram on p.19 of the guidelines rather than a
plain linear ramp. A two-colour gradient there reaches its second colour at 95
and puts the 50/50 blend at 70, not at the midpoint -- so it holds the first
colour and then moves. That bias is most of what stops a two-colour background
reading as a flat wash, and it is why this does not simply call PIL.

Bitmaps are cached by (size, colours, angle). A window resize regenerates at a
new width, so without the cache a drag across the screen would render hundreds
of images; with it, a size that has been seen before costs a dictionary lookup.
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from .. import brand

#: Rendered images, keyed by everything that determines their pixels.
_CACHE: dict[tuple, Image.Image] = {}

#: Enough entries for a window being dragged between a few sizes in both
#: themes, and small enough that the memory never matters.
_CACHE_MAX = 64


def _rgb(colour: str) -> np.ndarray:
    return np.array(brand.hex_to_rgb(colour), dtype=np.float64)


def _ramp(size: tuple[int, int], angle: float) -> np.ndarray:
    """A 0..1 field across the box, running along `angle`.

    Angle is in degrees, measured on screen with y increasing downward, so 0
    runs left to right and 45 runs from the top-left corner to the bottom-right
    one -- which is how the guidelines' own background panels read.
    """
    w, h = max(1, size[0]), max(1, size[1])
    theta = math.radians(angle)
    x = np.linspace(0.0, 1.0, w)[None, :] * math.cos(theta)
    y = np.linspace(0.0, 1.0, h)[:, None] * math.sin(theta)
    u = x + y
    lo, hi = float(u.min()), float(u.max())
    if hi - lo < 1e-9:
        return np.zeros((h, w))
    return (u - lo) / (hi - lo)


def _blend(t: np.ndarray, c1: np.ndarray, c2: np.ndarray,
           midpoint: float = 0.5) -> np.ndarray:
    """Interpolate c1..c2 over t in 0..1, with the 50/50 blend at `midpoint`.

    A midpoint below 0.5 pushes the blend early, above 0.5 holds the first
    colour. Implemented as an exponent so the curve stays smooth: at
    t == midpoint the exponent yields exactly 0.5.
    """
    t = np.clip(t, 0.0, 1.0)
    m = min(max(midpoint, 0.01), 0.99)
    if abs(m - 0.5) > 1e-6:
        t = t ** (math.log(0.5) / math.log(m))
    return c1[None, None, :] + (c2 - c1)[None, None, :] * t[:, :, None]


def two_colour(size: tuple[int, int], c1: str, c2: str,
               angle: float = brand.GRADIENT_ANGLE) -> Image.Image:
    """A background gradient built to the guidelines' two-colour geometry."""
    t = _ramp(size, angle)
    # The second colour is reached at 95 and holds flat to the end.
    t = np.clip(t / brand.TWO_COLOR_END, 0.0, 1.0)
    px = _blend(t, _rgb(c1), _rgb(c2),
                midpoint=brand.TWO_COLOR_MIDPOINT / brand.TWO_COLOR_END)
    return Image.fromarray(np.round(px).astype(np.uint8), "RGB")


def three_colour(size: tuple[int, int], c1: str, c2: str, c3: str,
                 angle: float = brand.GRADIENT_ANGLE) -> Image.Image:
    """Three colours at 0, 50 and 100, each half blending evenly."""
    t = _ramp(size, angle)
    first = _blend(np.clip(t * 2.0, 0.0, 1.0), _rgb(c1), _rgb(c2))
    second = _blend(np.clip(t * 2.0 - 1.0, 0.0, 1.0), _rgb(c2), _rgb(c3))
    px = np.where((t < 0.5)[:, :, None], first, second)
    return Image.fromarray(np.round(px).astype(np.uint8), "RGB")


def render(size: tuple[int, int], colours: tuple[str, ...],
           angle: float = brand.GRADIENT_ANGLE) -> Image.Image:
    """Two or three brand colours, drawn to `size`. Cached."""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    key = (w, h, tuple(colours), round(angle, 3))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    if len(colours) == 3:
        img = three_colour((w, h), *colours, angle=angle)
    elif len(colours) == 2:
        img = two_colour((w, h), *colours, angle=angle)
    else:
        raise ValueError(f"a gradient needs two or three colours, got {len(colours)}")
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = img
    return img


def chip(size: tuple[int, int], fill: str, border: str = "",
         border_w: int = 0, radius: int = 8, ground: str = "#000000",
         ) -> Image.Image:
    """A rounded, optionally bordered rectangle -- a button, drawn.

    Tk's canvas has no rounded rectangle and no anti-aliasing, so a chapter
    button drawn with canvas primitives comes out with stepped corners next to
    CustomTkinter's smooth ones. PIL draws it properly; `ground` is the colour
    behind it, composited in so the corners do not show black.

    Cached like the gradients: the rail repaints on hover, and four buttons at
    two states each would otherwise be redrawn on every mouse move.
    """
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    key = ("chip", w, h, fill, border, border_w, radius, ground)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    img = Image.new("RGB", (w, h), ground)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=fill,
                        outline=border or None,
                        width=border_w if border else 0)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = img
    return img


def sample(img: Image.Image, x: int, y: int) -> str:
    """The hex colour at a point, for a widget that has to sit on the gradient.

    CustomTkinter widgets carry their own flat background. One placed over a
    gradient can be told the colour underneath it, which hides the seam; this
    is how the theme switch disappears into the banner.
    """
    px = img.getpixel((min(max(int(x), 0), img.width - 1),
                       min(max(int(y), 0), img.height - 1)))
    return "#{:02x}{:02x}{:02x}".format(*px[:3])
