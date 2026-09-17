"""
Draw the desktop icon: the vehicle, seen head on, with its lights on.

    python assets/make_rov_icon.py              # write every option + the sheet
    python assets/make_rov_icon.py --pick bold  # make that one the app's icon

An original drawing of *our* vehicle -- a Blue Robotics heavy configuration in
the state it is actually flown in: red enclosures either side of the centre
tube, black float blocks with the red tape across them, the camera dome in the
middle, and the two lights on their stainless arms pointing down and lit.

**What an icon has to survive is being shrunk.** Windows asks for this at 256,
and it also asks for it at 16, which is nine or ten usable pixels of vehicle.
A photoreal render has nothing left at that size -- every edge greys into its
neighbour and the result is a smudge. So each size is drawn rather than
resampled from one bitmap, and each drops whatever it can no longer hold:

  * **full** (>= 96 px) -- thrusters, frame rails, dome highlight, tether
  * **simple** (>= 32 px) -- float blocks, red enclosures, dome, arms, beams
  * **minimal** (< 32 px) -- the silhouette, the two red stripes, the dome,
    and the beams. Four things, and they are the four that say which vehicle
    this is.

**And being seen on somebody's wallpaper.** The real vehicle is black, and a
black icon disappears on a dark desktop. The frame is drawn in a graphite that
still reads as black against white while staying visible against navy, the red
does most of the identifying work either way, and the `badge` options put the
whole thing on a coloured ground where contrast is guaranteed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

#: Drawn at four times the size asked for and reduced: Pillow does not
#: antialias, and almost every edge here is a curve or a diagonal.
SUPERSAMPLE = 4

#: The sizes Windows actually asks for. 16 and 20 are the taskbar and the
#: small views, 32 and 48 Explorer's medium and large, 256 the extra-large.
SIZES = (16, 20, 24, 32, 48, 64, 128, 256)

# --------------------------------------------------------------------------
#  colours
# --------------------------------------------------------------------------

#: The frame. Not black: see the note about wallpaper above.
FRAME = "#2E3944"
FRAME_LIT = "#485666"                # the edge catching the light
FRAME_DEEP = "#161D25"               # the keyline, and shadow in the frame
#: The enclosures and the tape on the float blocks, off the photograph.
RED = "#C62828"
RED_LIT = "#E14343"
#: The stainless arms the lights hang off.
STEEL = "#AFBCC8"
STEEL_LIT = "#D7E0E8"
#: The dome, and the glass in it.
GLASS = "#20303F"
GLASS_LIT = "#7FB6CE"
#: The tube behind the dome.
TUBE = "#C9D3DB"
#: The lights themselves. Saturated, because a beam is drawn over whatever
#: wallpaper is behind it: a pale yellow at low opacity comes out beige on a
#: light desktop and disappears entirely on a dark one.
BEAM = "#FFC21F"
BEAM_CORE = "#FFF3C4"
LENS = "#FFE9A3"
WHITE = "#FFFFFF"


def _rgba(colour: str, alpha: int = 255) -> tuple[int, int, int, int]:
    colour = colour.lstrip("#")
    return (int(colour[0:2], 16), int(colour[2:4], 16), int(colour[4:6], 16),
            alpha)


# --------------------------------------------------------------------------
#  the drawing
# --------------------------------------------------------------------------


class Pen:
    """A 256-unit design space, drawn at whatever size is being asked for."""

    def __init__(self, size: int, detail: str, outline: int = 0):
        self.size = size
        self.detail = detail
        self.s = size * SUPERSAMPLE
        self.im = Image.new("RGBA", (self.s, self.s), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)
        #: Width of the dark keyline around each shape, in design units. A
        #: keyline is what stops flat fills melting into each other when the
        #: icon is reduced; 0 leaves them touching.
        self.outline = outline

    # ---- units --------------------------------------------------------

    def u(self, v: float) -> float:
        """A coordinate given out of 256, at this icon's scale."""
        return v * self.s / 256.0

    def box(self, x0, y0, x1, y1):
        return [self.u(x0), self.u(y0), self.u(x1), self.u(y1)]

    @property
    def full(self) -> bool:
        return self.detail == "full"

    @property
    def simple(self) -> bool:
        return self.detail in ("full", "simple")

    @property
    def tiny(self) -> bool:
        return self.detail == "tiny"

    # ---- shapes -------------------------------------------------------

    def slab(self, x0, y0, x1, y1, fill, radius=6, edge=True):
        self.d.rounded_rectangle(
            self.box(x0, y0, x1, y1), radius=self.u(radius), fill=_rgba(fill),
            outline=_rgba(FRAME_DEEP) if (edge and self.outline) else None,
            width=max(1, int(self.u(self.outline))))

    def disc(self, cx, cy, r, fill, edge=True):
        self.d.ellipse(
            self.box(cx - r, cy - r, cx + r, cy + r), fill=_rgba(fill),
            outline=_rgba(FRAME_DEEP) if (edge and self.outline) else None,
            width=max(1, int(self.u(self.outline))))

    def _falloff(self, top: float) -> Image.Image:
        """An alpha ramp: full at the lamp, gone by the bottom of the icon."""
        y0 = int(self.u(top))
        ramp = Image.new("L", (1, self.s))
        px = ramp.load()
        for y in range(self.s):
            if y <= y0:
                px[0, y] = 255
            else:
                t = (y - y0) / max(1, self.s - y0)
                px[0, y] = int(255 * max(0.0, 1.0 - t) ** 1.5)
        return ramp.resize((self.s, self.s))

    def beam(self, cx, top, spread, strength=1.0):
        """A cone of light falling from a lamp, fading as it goes.

        Drawn through a gradient rather than as flat translucent wedges. Flat
        wedges are what made the first attempt beige: a pale yellow at half
        opacity over a light desktop is beige, and the same thing over navy is
        olive. Keeping the colour saturated and varying only the *alpha* keeps
        it yellow on both, and a beam that fades is what a light looks like
        anyway.
        """
        from PIL import ImageChops

        fade = self._falloff(top)
        for half, colour, peak in ((1.00, BEAM, 150), (0.42, BEAM_CORE, 215)):
            cone = Image.new("L", self.im.size, 0)
            ImageDraw.Draw(cone).polygon(
                [(self.u(cx - 7 * half), self.u(top)),
                 (self.u(cx + 7 * half), self.u(top)),
                 (self.u(cx + spread * half), self.u(256)),
                 (self.u(cx - spread * half), self.u(256))], fill=255)
            mask = ImageChops.multiply(cone, fade)
            if peak * strength < 255:
                mask = mask.point(lambda v, p=peak * strength: int(v * p / 255))
            layer = Image.new("RGBA", self.im.size, _rgba(colour))
            layer.putalpha(mask)
            self.im.alpha_composite(layer)
        self.d = ImageDraw.Draw(self.im)

    # ---- the vehicle, head on -----------------------------------------

    def draw_rov(self, *, beams: bool = True, thrusters: bool = True) -> None:
        """Everything, at whatever level of detail this size can hold.

        Laid out from the photograph, top to bottom: the two float blocks with
        the red tape across them, the corner thrusters behind their outer
        ends, the two red enclosures flanking the centre tube, the camera dome
        between them, the frame down each side, and the two lamps on their
        stainless arms at the bottom, lit.

        Three things are deliberately *not* drawn to scale. The dome is a
        little larger than life, because below about 32 px an accurate one is
        a single grey pixel. The gap between the enclosures is a little wider,
        to give the dome that room without burying it. And the whole vehicle
        is flattened slightly, because a wide shape survives reduction better
        than a square one -- a square silhouette at 16 px is a blob, and a
        wide one is still a vehicle.
        """
        if self.tiny:
            return self._draw_tiny(beams=beams)

        mirrored = self._mirror

        # ---- the lights first, so the vehicle sits on top of the glow ---
        if beams:
            for cx in mirrored(32):
                self.beam(cx, 216, 40 if self.simple else 34)

        # ---- corner thrusters, behind the float blocks ------------------
        if thrusters and self.full:
            for cx in mirrored(24):
                self.disc(cx, 66, 21, FRAME_DEEP)
                self.disc(cx, 66, 13, FRAME)
                self.disc(cx, 66, 5, FRAME_LIT, edge=False)

        # ---- the centre tube, seen end on -------------------------------
        # Its top is level with the float blocks', so the three of them read
        # as one row across the top of the vehicle. Standing it any higher,
        # or rounding its cap above them, turns it into a bottle neck.
        if self.simple:
            self.slab(110, 44, 146, 100, TUBE, radius=8)

        # ---- the frame ---------------------------------------------------
        # Drawn at every size, before the parts it holds. It is what makes
        # this one machine rather than a pile of components: without the
        # rails, the float blocks hover above the enclosures and the lamps
        # float below them, and the whole thing falls apart at small sizes.
        for x0, x1 in ((26, 42), (214, 230)):
            self.slab(x0, 60, x1, 184, FRAME, radius=7)
        self.slab(34, 166, 222, 184, FRAME, radius=7)

        # ---- float blocks, with the red tape ----------------------------
        self.slab(22, 44, 116, 82, FRAME, radius=8)
        self.slab(140, 44, 234, 82, FRAME, radius=8)
        for x0, x1 in ((31, 107), (149, 225)):
            self.slab(x0, 52, x1, 74, RED, radius=3, edge=False)

        # ---- the enclosures either side of the tube ---------------------
        # The biggest colour in the icon, and what carries it at 16 px.
        for x0, x1 in ((44, 106), (150, 212)):
            self.slab(x0, 88, x1, 146, RED, radius=10)
            if self.full:                     # the gloss along the top edge
                self.slab(x0 + 7, 94, x1 - 7, 105, RED_LIT, radius=4,
                          edge=False)

        # ---- forward thrusters, out from under the dome -----------------
        if thrusters and self.full:
            for cx in mirrored(78):
                self.disc(cx, 156, 15, FRAME_DEEP)
                self.disc(cx, 156, 9, FRAME)

        # ---- the camera dome --------------------------------------------
        # A bubble with a glint off the glass, never a dark circle with a
        # white dot in the middle of it: that is an eye, and it is all
        # anybody sees once they have noticed it.
        dome_r = 26 if self.simple else 29
        self.disc(128, 116, dome_r, FRAME)
        self.disc(128, 116, dome_r - 7, GLASS)
        if self.simple:
            self.d.ellipse(self.box(114, 102, 126, 112),
                           fill=_rgba(GLASS_LIT, 235))

        # ---- the arms, the lamps, and their lenses ----------------------
        # The arms overlap the bar across the foot of the frame, so the lamps
        # hang off the vehicle rather than beside it.
        self.slab(4, 174, 84, 192, STEEL, radius=7)
        self.slab(172, 174, 252, 192, STEEL, radius=7)
        if self.full:
            for x0, x1 in ((11, 77), (179, 245)):
                self.slab(x0, 177, x1, 183, STEEL_LIT, radius=3, edge=False)
        for cx in mirrored(32):
            self.slab(cx - 17, 188, cx + 17, 210, FRAME, radius=8)
            self.slab(cx - 12, 202, cx + 12, 214, LENS, radius=5, edge=False)

        # ---- the downward camera between them ---------------------------
        if self.simple:
            self.slab(114, 180, 142, 198, FRAME, radius=6)

    def _draw_tiny(self, *, beams: bool = True) -> None:
        """At 24 px and under, which is the taskbar and the small views.

        Not the same drawing with pieces removed -- a different drawing of the
        same vehicle. At this size the whole icon is about fifteen pixels
        across, so a frame rail is half a pixel, the gap between a rail and an
        enclosure is a quarter of one, and shrinking the large version turns
        all of it into one red-brown smear.

        So the frame becomes a single dark body, the lamps hang straight off
        its bottom corners with no arms between, and what is left is the five
        things that are still legible: the wide dark shape, two red bars
        across the top, two red blocks under them, the dome between, and the
        two lights. Those five are also what somebody recognises the vehicle
        by, which is why they are the five that are kept.
        """
        mirrored = self._mirror
        if beams:
            for cx in mirrored(38):
                self.beam(cx, 206, 44)

        # the body: one shape, so nothing inside it can turn to mud
        self.slab(20, 40, 236, 176, FRAME, radius=16)

        # the red tape across the float blocks
        for x0, x1 in ((30, 118), (138, 226)):
            self.slab(x0, 50, x1, 80, RED, radius=5, edge=False)

        # the enclosures
        for x0, x1 in ((34, 110), (146, 222)):
            self.slab(x0, 94, x1, 158, RED, radius=9, edge=False)

        # the dome
        self.disc(128, 122, 26, FRAME_DEEP, edge=False)
        self.disc(128, 122, 19, GLASS, edge=False)
        self.d.ellipse(self.box(114, 108, 126, 118), fill=_rgba(GLASS_LIT))

        # the lamps, straight off the bottom corners
        for cx in mirrored(38):
            self.slab(cx - 24, 168, cx + 24, 206, FRAME, radius=10)
            self.slab(cx - 18, 194, cx + 18, 210, LENS, radius=6, edge=False)

    @staticmethod
    def _mirror(x: float) -> tuple[float, float]:
        """A point and its reflection about the centre line."""
        return (x, 256 - x)

    # ---- finishing ----------------------------------------------------

    def finish(self, margin_pct: float = 0.03) -> Image.Image:
        """Crop to what was drawn, centre it, and reduce to the real size.

        The drawing does not fill its box -- the beams run to the bottom edge
        and there is air at the top -- and an icon adrift in the middle of its
        own square wastes the pixels it has fewest of.
        """
        im = self.im
        box = im.getbbox()
        if box:
            im = im.crop(box)
        margin = max(1, round(self.size * margin_pct)) * SUPERSAMPLE
        side = max(im.width, im.height) + 2 * margin
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
        return square.resize((self.size, self.size), Image.LANCZOS)


def _lighten(colour: str, amount: float) -> str:
    colour = colour.lstrip("#")
    rgb = [int(colour[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{int(v + (255 - v) * amount):02x}" for v in rgb)


def _badge(icon: Image.Image, colour: str, radius_pct: float = 0.22,
           inset_pct: float = 0.09) -> Image.Image:
    """The icon on a rounded square, for contrast on any desktop.

    The plate is flat, and it carries a thin lighter rim. The flatness is
    because the first version put a soft white band across the top to make it
    look like an object, and at icon sizes that does not read as light -- it
    reads as a white halo somebody forgot to delete. The rim is because a dark
    plate on a dark wallpaper is no plate at all: navy on navy simply vanished,
    which defeats the entire point of putting the vehicle on a plate.
    """
    size = icon.width
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    big = size * SUPERSAMPLE
    plate = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(plate)
    rim = max(1, int(big * 0.018))
    d.rounded_rectangle([0, 0, big - 1, big - 1],
                        radius=int(big * radius_pct),
                        fill=_rgba(colour),
                        outline=_rgba(_lighten(colour, 0.30)), width=rim)
    out.alpha_composite(plate.resize((size, size), Image.LANCZOS))

    inset = max(1, int(size * inset_pct))
    inner = icon.resize((size - 2 * inset, size - 2 * inset), Image.LANCZOS)
    out.alpha_composite(inner, (inset, inset))
    return out


# --------------------------------------------------------------------------
#  the options
# --------------------------------------------------------------------------


def _detail_for(size: int, floor_simple: int = 32, floor_full: int = 96) -> str:
    if size >= floor_full:
        return "full"
    if size >= floor_simple:
        return "simple"
    return "tiny"


def design_faithful(size: int) -> Image.Image:
    """Everything the vehicle has, dropped a piece at a time as it shrinks."""
    p = Pen(size, _detail_for(size), outline=0)
    p.draw_rov()
    return p.finish()


def design_bold(size: int) -> Image.Image:
    """The same vehicle with a keyline round every part.

    The keyline is the whole point: flat fills that touch each other blur into
    one shape when the icon is reduced, and a dark line between them survives
    the reduction and keeps the parts apart.
    """
    p = Pen(size, _detail_for(size, floor_full=128), outline=4)
    p.draw_rov()
    return p.finish()


def design_minimal(size: int) -> Image.Image:
    """The small drawing, used at *every* size.

    So the 256 and the 16 are recognisably the same picture rather than two
    different ones -- which is what makes an icon feel like a single thing,
    and is worth more than the detail it gives up.
    """
    p = Pen(size, "tiny", outline=4 if size >= 32 else 0)
    p.draw_rov()
    return p.finish(margin_pct=0.02)


def design_badge_blue(size: int) -> Image.Image:
    """`bold`, on Mediterranean blue. Contrast on any wallpaper, guaranteed."""
    return _badge(design_bold(size), "#1963B0")


def design_badge_teal(size: int) -> Image.Image:
    """`bold`, on Salish -- the brand's deep teal.

    Where the navy one used to be. Fathom navy is the window's own ground and
    looked right on paper, but it is also very close to a great many desktop
    wallpapers, and a plate that disappears is worse than no plate.
    """
    return _badge(design_bold(size), "#0A4E52")


def design_no_beams(size: int) -> Image.Image:
    """`bold` with the lights off, in case the glow reads as a fault."""
    p = Pen(size, _detail_for(size, floor_full=128), outline=4)
    p.draw_rov(beams=False)
    return p.finish()


def design_clean(size: int) -> Image.Image:
    """`bold` without the thrusters: the frame, the red, the dome, the lights.

    The thrusters are the first thing to become clutter -- eight dark discs
    around the edge of a shape that is already mostly dark. Without them the
    red and the lights have the icon to themselves.
    """
    p = Pen(size, _detail_for(size, floor_full=96), outline=4)
    p.draw_rov(thrusters=False)
    return p.finish()


DESIGNS = {
    "faithful": design_faithful,
    "bold": design_bold,
    "clean": design_clean,
    "minimal": design_minimal,
    "badge_blue": design_badge_blue,
    "badge_teal": design_badge_teal,
    "no_beams": design_no_beams,
}

#: What the application uses. Chosen 16 September 2026 from the seven above:
#: the keyline is what makes the parts survive being reduced, and the
#: thrusters are worth keeping at the sizes that can hold them.
DEFAULT = "bold"


# --------------------------------------------------------------------------
#  writing them out
# --------------------------------------------------------------------------


def build_ico(draw, out: Path) -> Path:
    frames = [draw(n) for n in SIZES]
    frames[-1].save(out, format="ICO", sizes=[(n, n) for n in SIZES],
                    append_images=frames[:-1])
    return out


def _font(px: int):
    from PIL import ImageFont

    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


#: The grounds an icon has to survive. A desktop can be any of them.
GROUNDS = (("light", (246, 247, 249), (20, 24, 30)),
           ("mid", (122, 128, 136), (255, 255, 255)),
           ("dark", (12, 35, 64), (255, 255, 255)))


def sheet_large(path: Path, cell: int = 250, grounds=("light", "dark")) -> Path:
    """Every option at 256, big enough to judge the drawing itself."""
    names = list(DESIGNS)
    pad, head, label = 18, 44, 32
    w = pad + len(names) * (cell + pad)
    block = head + cell + label
    bands = [g for g in GROUNDS if g[0] in grounds]
    out = Image.new("RGB", (w, block * len(bands)), (255, 255, 255))
    d = ImageDraw.Draw(out)
    f, f_head = _font(20), _font(26)

    for b, (ground_name, ground, ink) in enumerate(bands):
        top = b * block
        d.rectangle([0, top, w, top + block], fill=ground)
        d.text((pad, top + 10), f"on a {ground_name} desktop", font=f_head,
               fill=ink)
        for i, name in enumerate(names):
            x = pad + i * (cell + pad)
            icon = DESIGNS[name](256).resize((cell, cell), Image.LANCZOS)
            out.paste(icon, (x, top + head), icon)
            d.text((x, top + head + cell + 4), name, font=f, fill=ink)
    out.save(path)
    return path


def sheet_small(path: Path, ground_name: str, sizes=(48, 32, 24, 16),
                zoom: int = 5) -> Path:
    """The sizes that decide it, on one ground, blown up pixel by pixel.

    This is the sheet that matters. Anything can be made to look good at 256;
    what an icon is actually chosen on is whether it still says "our ROV" in
    the taskbar. Magnified with no smoothing, so what is judged is what is
    really on screen, and one ground per file so the picture is a shape a
    screen can show rather than a ribbon two thousand pixels long.
    """
    ground, ink = next((g, i) for n, g, i in GROUNDS if n == ground_name)
    names = list(DESIGNS)
    pad, head, side, gap = 20, 54, 96, 18
    col_w = max(sizes) * zoom + gap
    row_h = max(sizes) * zoom + gap
    w = pad + side + len(names) * col_w + pad
    h = head + 26 + len(sizes) * row_h + pad
    out = Image.new("RGB", (w, h), ground)
    d = ImageDraw.Draw(out)
    f, f_head = _font(18), _font(26)

    d.text((pad, pad), f"on a {ground_name} desktop", font=f_head, fill=ink)
    for i, name in enumerate(names):
        d.text((pad + side + i * col_w, head + 2), name, font=f, fill=ink)
    for r, n in enumerate(sizes):
        y = head + 26 + r * row_h
        d.text((pad, y + row_h // 2 - 12), f"{n}px", font=f_head, fill=ink)
        for i, name in enumerate(names):
            x = pad + side + i * col_w
            icon = DESIGNS[name](n)
            big = icon.resize((n * zoom, n * zoom), Image.NEAREST)
            out.paste(big, (x, y), big)
    out.save(path)
    return path


def main(argv: list[str]) -> int:
    pick = DEFAULT
    if "--pick" in argv:
        pick = argv[argv.index("--pick") + 1]
        if pick not in DESIGNS:
            print(f"unknown design {pick!r}; one of {', '.join(DESIGNS)}")
            return 2

    options = HERE / "icon_options"
    options.mkdir(exist_ok=True)
    for name, draw in DESIGNS.items():
        build_ico(draw, options / f"rov_{name}.ico")
        draw(256).save(options / f"rov_{name}_256.png")
    print(f"wrote {len(DESIGNS)} options to {options}")

    print(f"wrote {sheet_large(options / '1_at_256px.png')}")
    for ground, _bg, _ink in GROUNDS:
        print(f"wrote {sheet_small(options / f'2_small_on_{ground}.png', ground)}")
    print(f"wrote {sheet_large(HERE / 'rov_flight_ops_preview.png', cell=200)}")

    chosen = build_ico(DESIGNS[pick], HERE / "rov_flight_ops.ico")
    print(f"wrote {chosen}  (design: {pick})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
