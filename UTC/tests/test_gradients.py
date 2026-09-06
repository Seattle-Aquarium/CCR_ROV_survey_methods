"""The brand gradients, and the type that has to survive sitting on them.

Two separate concerns, both hermetic -- no window, no display, so this runs in
CI alongside everything else.

The first is that the gradient is the one the guidelines describe rather than a
plain ramp: p.19 puts the second colour at 95 and the 50/50 blend at 70, and
that bias is most of what keeps a two-colour background from reading as a flat
wash.

The second is the failure that actually happened. The chrome is drawn over a
gradient, so a colour chosen against one end of it can disappear at the other:
the rail's muted ink read fine on Salish and sat at 1.6:1 on Mediterranean --
invisible -- because only the dark mode had ever been looked at.
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

#: Every ground the chrome type is drawn on, across both appearance modes.
CHROME_GROUNDS = sorted({c for pair in T.HEADER_GRADIENT.values() for c in pair}
                        | {c for pair in T.RAIL_GRADIENT.values() for c in pair})

#: p.20: large type -- the rail is set at 15pt semibold, which qualifies.
LARGE_TEXT = 3.0
NORMAL_TEXT = 4.5


@pytest.mark.parametrize("ground", CHROME_GROUNDS)
def test_the_rail_reads_at_both_ends_of_its_own_gradient(ground):
    """The regression: an ink picked against the dark mode alone.

    The rail runs Salish to Fathom in dark and Salish to Mediterranean in
    light, so a chapter name has to hold up on all three.
    """
    assert brand.contrast(T.CHROME_TEXT, ground) >= NORMAL_TEXT
    assert brand.contrast(T.CHROME_TEXT_MUTED, ground) >= LARGE_TEXT, (
        f"unselected chapter names are illegible on {ground}")


def test_the_deep_gradients_can_all_carry_white_type():
    """Which is why the banner uses a deep gradient and not a medium one."""
    for name, pair in brand.DEEP_GRADIENTS.items():
        for colour in pair:
            assert brand.contrast(brand.WHITE, colour) >= NORMAL_TEXT, name


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
