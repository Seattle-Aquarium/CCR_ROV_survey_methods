"""
What a light *should* be drawing at the level it reports -- an expectation,
never a measurement.

There is one power monitor's worth of wiring for all four lights, and as of
13 October 2026 Lutris has no monitor fitted at all. So per-light power,
current and voltage cannot be measured. What can be said per light is what it
would draw at the level it reports, from two measured facts:

* one light at full output draws about **98 W** (Keenan Johnson, bench test,
  CCR_development#34, 15 January 2026; ~100 W per light in the flight power
  logs of 12 May 2026);
* DeepSea's serial dimming curve maps the LOUT value onto per-cent of full
  light output (`data/lsl_seasense_dimming.csv`, digitised from the SeaLite
  manual; see `lighting/simulation`).

The model is that electrical power follows light output. That is a
simplification -- LED efficacy is higher at low current, so the true draw at
low levels is probably somewhat below this -- and it is labelled "expected"
everywhere it is shown. Its use is the comparison: with a monitor fitted, a
measured total well below the expected total says a light is dark while
still answering, which is exactly the failure light 4 has shown.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: One light at LOUT 100, watts.
FULL_W = 98.0
#: The light supply, for turning watts into amps when no monitor reads it.
NOMINAL_RAIL_V = 24.0

_CURVE = Path(__file__).resolve().parent / "data" / "lsl_seasense_dimming.csv"


@lru_cache(maxsize=1)
def curve() -> tuple[float, ...]:
    """Per-cent of full output for LOUT 0..100."""
    out = [0.0] * 101
    for line in _CURVE.read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("lout"):
            continue
        a, b = line.split(",")[:2]
        out[int(a)] = float(b)
    return tuple(out)


def output_pct(lout: float | None) -> float | None:
    if lout is None:
        return None
    i = max(0, min(100, int(round(lout))))
    return curve()[i]


def expected_w(lout: float | None) -> float | None:
    pct = output_pct(lout)
    return None if pct is None else FULL_W * pct / 100.0


def expected_a(lout: float | None, rail_v: float | None = None) -> float | None:
    w = expected_w(lout)
    v = rail_v if rail_v and rail_v > 5 else NOMINAL_RAIL_V
    return None if w is None else w / v
