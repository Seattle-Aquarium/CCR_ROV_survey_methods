"""Tests for the Annex B NAL scanner.

This code walks every byte of the ROV bitstream -- around 2 GB per dive -- so it
is written with ``bytes.find`` rather than a Python index loop. That rewrite was
worth ~26x on extraction, and these tests pin the behaviour so a later
"simplification" cannot quietly undo it.

A fuzz comparison against an index-stepping reference also caught a real
off-by-one in the original: a start code followed by a lone NAL header byte at
the very end of a buffer was missed, because the loop guard required four bytes
to remain. Real slices always carry payload so it never mattered in practice,
but the reference below is the corrected one.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from composite.mcap_extract import _nal_iter, has_idr, sps_resolution  # noqa: E402

SC3 = b"\x00\x00\x01"
SC4 = b"\x00\x00\x00\x01"


def reference(data: bytes) -> list[tuple[int, int]]:
    """Straightforward scanner: every 3-byte start code with a byte after it."""
    out: list[tuple[int, int]] = []
    p, n = 0, len(data)
    while p < n:
        i = data.find(SC3, p)
        if i < 0:
            break
        h = i + 3
        if h < n:
            out.append((h, data[h] & 0x1F))
        p = h
    return out


def test_matches_reference_on_fuzz():
    random.seed(7)
    for trial in range(2000):
        buf = bytearray()
        for _ in range(random.randint(0, 6)):
            buf += random.choice([SC3, SC4])
            buf += bytes([random.randint(0, 255)])
            buf += bytes(random.randint(0, 30))
        b = bytes(buf)
        assert list(_nal_iter(b)) == reference(b), (trial, b.hex(" "))


def test_tail_nal_is_found():
    """The case the original implementation missed."""
    assert [t for _h, t in _nal_iter(b"\x00\x00\x01\x1f")] == [31]
    assert [t for _h, t in _nal_iter(b"\x00\x00\x00\x01\x1f")] == [31]


def test_four_byte_start_code_yields_one_nal():
    # SC3 is a suffix of SC4, so a naive search must not report it twice
    assert [t for _h, t in _nal_iter(SC4 + b"\x65payload")] == [5]


def test_empty_and_degenerate():
    for b in (b"", b"\x00", b"\x00\x00", b"\x00\x00\x01", SC4):
        list(_nal_iter(b))            # must not raise
    assert has_idr(b"") is False
    assert has_idr(b"\x00\x00\x01") is False


def test_has_idr_stops_at_first_picture_nal():
    aud, sps, pps = b"\x09\xf0", b"\x67\x4d\x40\x29", b"\x68\xee\x38\x80"
    idr = SC4 + aud + SC4 + sps + SC4 + pps + SC4 + b"\x65" + b"\x00" * 400
    non = SC4 + aud + SC4 + sps + SC4 + pps + SC4 + b"\x41" + b"\x00" * 400
    assert has_idr(idr) is True
    assert has_idr(non) is False
    # a later IDR must not rescue a frame that opened on a non-IDR slice
    assert has_idr(non + SC4 + b"\x65") is False


def test_sps_resolution_on_a_real_header():
    """SPS captured from the BlueROV low-light camera: 1920x1080."""
    sps = bytes.fromhex("674d402996540 3c0113f2a".replace(" ", ""))
    frame = SC4 + b"\x09\xf0" + SC4 + sps + SC4 + b"\x68\xee\x38\x80"
    assert sps_resolution(frame) == (1920, 1080)


def test_sps_resolution_absent_is_none():
    assert sps_resolution(SC4 + b"\x41abc") is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as ex:
                failed += 1
                print(f"  FAIL  {name}: {ex}")
    print(f"\n{'all passed' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)
