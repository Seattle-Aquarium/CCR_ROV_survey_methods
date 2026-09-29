"""Recover the LED SeaLite photometric curves from the operator's manual.

The manual's charts are vector art, not raster images, so the polylines carry the
manufacturer's actual measured values. This script pulls those polylines out and
converts them from PDF page coordinates into physical units, which is a great deal
more honest than reading numbers off a picture with a ruler.

Two charts are recovered:

* Appendix D, page 13 -- "Angular Distribution", relative luminous intensity
  against off-axis angle, for the Flood (075) and Spot (035) optics.
* Appendix C, page 10 -- the SeaSense serial dimming curve, which maps the
  ``LOUT`` command value our Pico sends (``lighting/code/src/lib/pydspl_seasense``)
  onto actual light output.

Usage::

    python extract_from_manual.py [path/to/LEDSeaLite_Manual.pdf]

Requires ``pymupdf``. The manual is not redistributed in this repository; it lives
in the team Dropbox at ``documents/ROV_documents/lights/LEDSeaLite_Manual.pdf``.
The generated CSVs *are* committed, so nobody needs the PDF or pymupdf just to run
the simulation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

#: Without an argument, the manual is looked for next to this script.
DEFAULT_PDF = Path(__file__).parent / "LEDSeaLite_Manual.pdf"
OUT_DIR = Path(__file__).parent

# --- Chart calibration ------------------------------------------------------
# Page coordinates of known axis positions, read from the gridlines and axis
# rules rather than guessed. Each is checked against a second, independent
# gridline in `_selfcheck` below.

# Appendix D, page 13 (0-based index 12), right-hand "Angular Distribution" panel.
# x is % of peak output, y is off-axis angle.
BEAM_PAGE = 12
BEAM_X_0PCT, BEAM_X_100PCT = 399.50, 548.46
BEAM_Y_PLUS90, BEAM_Y_MINUS90 = 249.87, 628.35
BEAM_CURVES = {  # drawing index -> optic name
    23: "flood",  # LSL-1000-6KA-DW-075-PV-SUBMC3, blue trace
    24: "spot",  # LSL-1000-6KA-DW-035-PV-SUBMC3, yellow trace
}

# Appendix C, page 10 (0-based index 9), upper "SeaSense Serial Dimming Curve".
DIM_PAGE = 9
DIM_CURVE = 6
DIM_X_LOUT0, DIM_X_LOUT100 = 134.7, 502.6
DIM_Y_0PCT, DIM_Y_100PCT = 417.5, 194.6

# Peak on-axis illuminance at 1 m, from the Appendix D annotation boxes. Because
# lux at 1 m is numerically equal to candela, these are the peak luminous
# intensities I0 used by the simulation.
PEAK_LUX_1M = {"flood": 5680.0, "spot": 14400.0}


def _polyline(drawing) -> np.ndarray:
    """Flatten one PyMuPDF drawing into an (n, 2) array of page-space points."""
    pts: list[tuple[float, float]] = []
    for item in drawing["items"]:
        if item[0] == "l":  # line segment
            pts.append((item[1].x, item[1].y))
            pts.append((item[2].x, item[2].y))
        elif item[0] == "c":  # cubic bezier; the control points sit on the trace
            pts.extend((p.x, p.y) for p in item[1:])
    return np.asarray(pts, dtype=float)


def _selfcheck(name: str, got: float, expect: float, tol: float, unit: str) -> None:
    if abs(got - expect) > tol:
        raise SystemExit(
            f"calibration self-check failed for {name}: got {got:.2f}{unit}, "
            f"expected {expect:.2f}{unit} (tol {tol}{unit}). "
            "The manual revision has probably changed; re-read the gridlines."
        )
    print(f"  check {name}: {got:+.2f}{unit} (expected {expect:+.2f}{unit}) OK")


def extract_beam_profiles(doc) -> np.ndarray:
    """Return columns [angle_deg, flood_rel, spot_rel] on a symmetric 0-90 grid."""
    drawings = doc[BEAM_PAGE].get_drawings()
    grid = np.arange(0.0, 90.5, 0.5)
    columns = [grid]

    for index, optic in BEAM_CURVES.items():
        raw = _polyline(drawings[index])
        pct = (raw[:, 0] - BEAM_X_0PCT) / (BEAM_X_100PCT - BEAM_X_0PCT) * 100.0
        deg = ((BEAM_Y_PLUS90 + BEAM_Y_MINUS90) / 2 - raw[:, 1]) / (
            (BEAM_Y_MINUS90 - BEAM_Y_PLUS90) / 180.0
        )
        order = np.argsort(deg)
        deg, pct = deg[order], pct[order]

        # The chart traces both sides of the beam. A real luminaire is nominally
        # axisymmetric, so fold the two halves together; the spread between them
        # is the measurement's own asymmetry and is reported below.
        pos = np.interp(grid, deg, pct)
        neg = np.interp(-grid, deg, pct)
        folded = np.clip((pos + neg) / 2.0, 0.0, None)
        folded /= folded.max()
        columns.append(folded)

        asym = np.abs(pos - neg).max()
        half = np.interp(-0.5, -folded, grid)  # first crossing below 50%
        print(
            f"  {optic:5s}: FWHM {2 * half:.1f} deg, "
            f"max +/- asymmetry {asym:.1f} percentage points"
        )

    _selfcheck("flood FWHM", 2 * np.interp(-0.5, -columns[1], grid), 76.0, 3.0, " deg")
    _selfcheck("spot FWHM", 2 * np.interp(-0.5, -columns[2], grid), 34.0, 3.0, " deg")
    return np.column_stack(columns)


def extract_dimming_curve(doc) -> np.ndarray:
    """Return columns [lout, output_pct] on an integer LOUT grid."""
    raw = _polyline(doc[DIM_PAGE].get_drawings()[DIM_CURVE])
    lout = (raw[:, 0] - DIM_X_LOUT0) / (DIM_X_LOUT100 - DIM_X_LOUT0) * 100.0
    out = (raw[:, 1] - DIM_Y_0PCT) / (DIM_Y_100PCT - DIM_Y_0PCT) * 100.0
    order = np.argsort(lout)
    grid = np.arange(0.0, 101.0, 1.0)
    curve = np.clip(np.interp(grid, lout[order], out[order]), 0.0, 100.0)

    _selfcheck("dimming at LOUT 0", curve[0], 0.0, 1.0, "%")
    _selfcheck("dimming at LOUT 100", curve[100], 100.0, 1.0, "%")
    _selfcheck("dimming knee at LOUT 60", curve[60], 15.0, 2.0, "%")
    return np.column_stack([grid, curve])


def main() -> None:
    try:
        import pymupdf
    except ImportError:  # pragma: no cover - only needed to regenerate the CSVs
        raise SystemExit("pip install pymupdf to regenerate the beam data")

    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    if not pdf.exists():
        raise SystemExit(f"manual not found: {pdf}")
    doc = pymupdf.open(pdf)

    print("Beam profiles (Appendix D, p.13):")
    beams = extract_beam_profiles(doc)
    header = (
        "Relative luminous intensity of the DeepSea Power & Light LED SeaLite,\n"
        "digitised from the vector art of Appendix D (Beam Patterns), p.13 of the\n"
        "Operator's Manual rev. 08/27/18. Folded about the axis and normalised so\n"
        "that peak = 1.0. Multiply by the peak intensity to get candela:\n"
        f"flood I0 = {PEAK_LUX_1M['flood']:.0f} cd, spot I0 = {PEAK_LUX_1M['spot']:.0f} cd\n"
        "(the manual quotes these as peak lux at 1 m, which is the same number).\n"
        "angle_deg,flood_rel,spot_rel"
    )
    np.savetxt(
        OUT_DIR / "lsl_beam_profiles.csv",
        beams,
        delimiter=",",
        fmt=["%.1f", "%.5f", "%.5f"],
        header=header,
    )

    print("Dimming curve (Appendix C, p.10):")
    dim = extract_dimming_curve(doc)
    np.savetxt(
        OUT_DIR / "lsl_seasense_dimming.csv",
        dim,
        delimiter=",",
        fmt=["%d", "%.3f"],
        header=(
            "SeaSense serial dimming curve, digitised from Appendix C, p.10 of the\n"
            "LED SeaLite Operator's Manual rev. 08/27/18. Maps the LOUT command\n"
            "value sent by Sealite.set_level() onto per-cent of full light output.\n"
            "lout,output_pct"
        ),
    )
    print(f"\nwrote {OUT_DIR / 'lsl_beam_profiles.csv'}")
    print(f"wrote {OUT_DIR / 'lsl_seasense_dimming.csv'}")


if __name__ == "__main__":
    main()
