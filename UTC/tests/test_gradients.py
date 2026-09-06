"""The brand gradients, and the type that has to survive sitting on them.

Two separate concerns, both hermetic -- no window, no display, so this runs in
CI alongside everything else.

The first is that the gradient is the one the guidelines describe rather than a
plain ramp: p.19 puts the second colour at 95 and the 50/50 blend at 70, and
that bias is most of what keeps a two-colour background from reading as a flat
wash.

The second is legibility on a brand colour. Each chapter button carries its
own, and the type on it is chosen by measuring rather than from a table -- so
what has to hold is that the measuring works for every palette anyone might
switch to, not just the one in use today.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import brand  # noqa: E402
from utc.gui import gradients as G  # noqa: E402
from utc.gui import theme as T  # noqa: E402

A, B, C = brand.ALGAE, brand.SEAFOAM, brand.PURPLE_STAR


def _px(img, x, y) -> tuple[int, int, int]:
    return img.getpixel((x, y))[:3]


# --------------------------------------------------------------------------
#  geometry
# --------------------------------------------------------------------------


def test_a_two_colour_gradient_starts_and_ends_on_its_own_colours():
    img = G.render((200, 1), (brand.SALISH, brand.FATHOM), angle=0.0)
    assert G.sample(img, 0, 0) == brand.SALISH.lower()
    # The second colour is reached at 95 and holds flat to the end.
    assert G.sample(img, 199, 0) == brand.FATHOM.lower()
    assert G.sample(img, 191, 0) == brand.FATHOM.lower()


def test_the_blend_lands_at_seventy_percent_not_halfway():
    """The spec diagram's midpoint marker. Without it this is a plain ramp."""
    img = G.render((1001, 1), (brand.SALISH, brand.FATHOM), angle=0.0)
    a, b = brand.hex_to_rgb(brand.SALISH), brand.hex_to_rgb(brand.FATHOM)
    halfway = tuple((x + y) / 2 for x, y in zip(a, b, strict=True))

    at_70 = _px(img, 700, 0)
    at_50 = _px(img, 500, 0)
    # 70 is where the even blend lands...
    assert max(abs(p - q) for p, q in zip(at_70, halfway, strict=True)) <= 2
    # ...so the true midpoint is still nearer the first colour.
    d_first = sum(abs(p - q) for p, q in zip(at_50, a, strict=True))
    d_second = sum(abs(p - q) for p, q in zip(at_50, b, strict=True))
    assert d_first < d_second, "the gradient should hold Salish, then move"


def test_a_three_colour_gradient_passes_through_its_middle_colour():
    img = G.render((201, 1), (A, B, C), angle=0.0)
    assert G.sample(img, 0, 0) == A.lower()
    assert G.sample(img, 100, 0) == B.lower()
    assert G.sample(img, 200, 0) == C.lower()


def test_the_angle_decides_which_way_it_runs():
    across = G.render((60, 60), (A, C), angle=0.0)
    assert _px(across, 0, 30) != _px(across, 59, 30)
    assert _px(across, 30, 0) == _px(across, 30, 59), "0 deg must not vary down"

    down = G.render((60, 60), (A, C), angle=90.0)
    assert _px(down, 30, 0) != _px(down, 30, 59)
    assert _px(down, 0, 30) == _px(down, 59, 30), "90 deg must not vary across"

    diagonal = G.render((60, 60), (A, C), angle=45.0)
    assert _px(diagonal, 0, 0) != _px(diagonal, 59, 59)


def test_a_gradient_is_rendered_once_per_size():
    """A window drag fires a Configure per pixel; without the cache that would
    be a full render each time."""
    first = G.render((123, 45), (A, B))
    assert G.render((123, 45), (A, B)) is first
    assert G.render((123, 46), (A, B)) is not first


def test_one_colour_is_not_a_gradient():
    with pytest.raises(ValueError):
        G.render((10, 10), (A,))


def test_a_gradient_of_zero_width_still_produces_an_image():
    """A canvas reports 1x1 before it is laid out, and the banner draws then."""
    assert G.render((0, 0), (A, B)).size == (1, 1)


# --------------------------------------------------------------------------
#  legibility on those gradients
# --------------------------------------------------------------------------

#: p.20: large type -- the rail is set at 15pt semibold, which qualifies.
LARGE_TEXT = 3.0
NORMAL_TEXT = 4.5


@pytest.mark.parametrize("mode,index", [("light", 0), ("dark", 1)])
def test_the_banner_and_rail_read_on_their_flat_surfaces(mode, index):
    """Both are flat, so these are ordinary two-colour checks -- but the
    surface is Pumice in light mode and a lifted Fathom in dark, which are as
    far apart as two grounds get."""
    for ground in (T.HEADER_BG[index], T.RAIL_BG[index]):
        assert brand.contrast(T.TEXT[index], ground) >= NORMAL_TEXT
        assert brand.contrast(T.HEADING[index], ground) >= LARGE_TEXT
        assert brand.contrast(T.TEXT_MUTED[index], ground) >= LARGE_TEXT, (
            f"muted type is illegible in {mode} mode")


@pytest.mark.parametrize("palette", sorted(T.CHAPTER_PALETTES))
def test_every_chapter_palette_carries_legible_type(palette):
    """Each button is a brand colour, and `ink_for` picks the type by
    measuring against it. This is the guard on that: swap the palette and the
    type follows, without anyone remembering to change it.

    Seafoam is the one that catches people out -- it sits mid-range, so White
    fails on it and Fathom is needed.
    """
    for colour in T.CHAPTER_PALETTES[palette]:
        ink = T.ink_for(colour)
        assert brand.contrast(ink, colour) >= NORMAL_TEXT, (
            f"{palette}: no legible type for {colour}")


def test_seafoam_takes_dark_type_and_salish_takes_white():
    """Named explicitly, because it is the case that looks wrong until it is
    measured: the bright secondary needs dark type."""
    assert T.ink_for(brand.SEAFOAM) == brand.FATHOM
    assert T.ink_for(brand.SALISH) == brand.WHITE
    assert T.ink_for(brand.PURPLE_STAR) == brand.WHITE


def test_no_chapter_palette_uses_a_colour_that_is_spoken_for():
    """Fathom is the dark-mode ground and Algae marks the open tool inside a
    chapter. A chapter button in either would be saying something it does not
    mean."""
    for name, palette in T.CHAPTER_PALETTES.items():
        assert brand.FATHOM not in palette, name
        assert brand.ALGAE not in palette, name


def test_contrast_agrees_with_the_published_pairs():
    """Sanity-check the maths against values anyone can verify."""
    assert brand.contrast("#FFFFFF", "#000000") == pytest.approx(21.0)
    assert brand.contrast("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)
    # p.20 shows White type on Salish as an accessible pairing.
    assert brand.contrast(brand.WHITE, brand.SALISH) > 4.5
    # ...and Purple Star as carrying White but not much else.
    assert brand.contrast(brand.WHITE, brand.PURPLE_STAR) > 4.5


def test_the_bright_gradients_are_reserved_for_elements_not_type():
    """A guard on intent: these are markers, so nothing needs to read on them.

    Stated as a test because it would otherwise be a comment, and the failure
    mode is somebody setting a label in one.
    """
    for pair in brand.BRIGHT_GRADIENTS.values():
        assert any(brand.contrast(brand.WHITE, c) < NORMAL_TEXT for c in pair)
