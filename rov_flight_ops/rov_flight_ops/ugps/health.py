"""
Is each UGPS receiver behaving like the other three?

The four receivers of a G2 antenna sit within half a metre of each other and
hear the same locator, so in normal operation their signal and noise track
together within a few decibels. A receiver whose signal-to-noise sits well
below the rest, whose noise floor sits well above it, or which keeps dropping
out while the others hold, is the one to look at. That comparison is all this
does: it is a screening aid for the operator, not a diagnosis, and its
thresholds are first guesses to be tuned against real dives.

Nothing here touches Tk, so it is tested on its own.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

#: The window judged, in seconds of history.
WINDOW_S = 60.0
#: A receiver this many dB below the median of the other three is flagged.
SNR_GAP_DB = 6.0
#: Its noise this many dB above theirs is flagged.
NSD_GAP_DB = 6.0
#: Valid in less than this share of solutions while the others manage
#: `OTHERS_VALID` is flagged.
LOW_VALID = 0.5
OTHERS_VALID = 0.8
#: Fewer solutions than this in the window and nothing is judged.
MIN_SOLUTIONS = 10


@dataclass
class ReceiverHealth:
    index: int
    solutions: int = 0
    valid_fraction: float | None = None
    snr_db: float | None = None          # median while valid
    nsd_db: float | None = None          # median
    rssi_db: float | None = None         # median while valid
    flags: list[str] = field(default_factory=list)
    #: SNR of each valid solution in the window, oldest first, for a sparkline.
    trace: list[float] = field(default_factory=list)


@dataclass
class Health:
    receivers: list[ReceiverHealth]
    solutions: int
    verdict: str
    flagged: bool


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def judge(history, now: float, window_s: float = WINDOW_S) -> Health:
    """History entries are (monotonic s, position_valid, per receiver
    (valid, snr, nsd, rssi)), as `UgpsCapture.snapshot()["history"]` gives."""
    recent = [h for h in history if now - h[0] <= window_s]
    n_rx = max((len(h[2]) for h in recent), default=4)
    recs = []
    for i in range(n_rx):
        rows = [h[2][i] for h in recent if i < len(h[2])]
        valid = [r for r in rows if r[0]]
        rh = ReceiverHealth(index=i, solutions=len(rows))
        if rows:
            rh.valid_fraction = len(valid) / len(rows)
            rh.snr_db = _median(r[1] for r in valid)
            rh.nsd_db = _median(r[2] for r in rows)
            rh.rssi_db = _median(r[3] for r in valid)
            rh.trace = [r[1] for r in valid if r[1] is not None]
        recs.append(rh)

    if len(recent) < MIN_SOLUTIONS:
        verdict = ("no acoustic solutions from the G2 in the last minute"
                   if not recent else f"only {len(recent)} solutions in the last minute")
        return Health(recs, len(recent), verdict, False)

    for rh in recs:
        others = [o for o in recs if o is not rh]
        o_snr = _median(o.snr_db for o in others)
        o_nsd = _median(o.nsd_db for o in others)
        o_valid = _median(o.valid_fraction for o in others)
        if rh.snr_db is not None and o_snr is not None and o_snr - rh.snr_db >= SNR_GAP_DB:
            rh.flags.append(f"SNR {o_snr - rh.snr_db:.0f} dB below the others")
        if rh.nsd_db is not None and o_nsd is not None and rh.nsd_db - o_nsd >= NSD_GAP_DB:
            rh.flags.append(f"noise {rh.nsd_db - o_nsd:.0f} dB above the others")
        if (rh.valid_fraction is not None and o_valid is not None
                and rh.valid_fraction < LOW_VALID and o_valid >= OTHERS_VALID):
            rh.flags.append(f"valid in {rh.valid_fraction:.0%} of solutions")

    flagged = [rh for rh in recs if rh.flags]
    if flagged:
        verdict = "; ".join(f"Rx {rh.index}: {', '.join(rh.flags)}" for rh in flagged)
    else:
        snrs = [rh.snr_db for rh in recs if rh.snr_db is not None]
        if len(snrs) == len(recs) and snrs:
            verdict = (f"all {len(recs)} alike: SNR {min(snrs):.0f}–{max(snrs):.0f} dB "
                       f"over {len(recent)} solutions")
        else:
            verdict = "no receiver stands out"
    return Health(recs, len(recent), verdict, bool(flagged))
