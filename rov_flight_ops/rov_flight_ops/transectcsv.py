"""
The 1 Hz transect CSVs, made from a flight folder without a window.

The transect extractor lives in the sibling ``mcap_to_csv`` package. This is
the part of the old *Analyze transects* tab that drove it -- the flight's
recordings, its saved transects, the vehicle's own origin as a fallback --
lifted out so that **BlueOS logs → Download files** can make the CSVs as soon
as the recordings are down, on the boat, with nothing else to press.

The recordings are the flight's **mcaps** (BlueOS 1.5) or, failing those, its
**tlogs** (BlueOS 1.4 and earlier): the extractor reads either, the same way,
into the same 45 columns.

**The .BIN logs are the check, and the fallback for the origin.** The
autopilot writes them whatever BlueOS recorded, so for every transect the
report says how much of it the recordings cover and whether a BIN covers it --
a transect the recordings missed but a BIN has is the case the BIN exists for
(a router that died mid-dive, as on 1 September 2026), and the report names
the BIN. The vehicle's own ORIGIN_LAT/ORIGIN_LON, read from a BIN, seed the
DVL track when the recording has no fix of its own, exactly as Analyze
transects did.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

ProgressCB = Callable[[float, str], None]

MISSING = ("The transect extractor (mcap_to_csv) is not installed in this "
           "program's environment. Run run_rov_flight_ops.bat --repair, or: "
           "python -m pip install -e ../mcap_to_csv")


class Stopped(Exception):
    """Stop was pressed part way."""


def extractor():
    """(pipeline, tide) from the extractor, or None if it is not installed."""
    try:
        from ccr_m2c import pipeline, tide
        return pipeline, tide
    except ImportError:
        return None


def stations() -> list[str]:
    mod = extractor()
    return [s[0] for s in mod[1].STATIONS] if mod else []


def station_id(name: str) -> str | None:
    mod = extractor()
    if mod is None or not name:
        return None
    return dict(mod[1].STATIONS).get(name)


def origin_fallback(flight_dir: Path):
    """The dive's own ORIGIN_LAT/ORIGIN_LON from whichever .BIN has them."""
    from . import binlog
    try:
        bins = binlog.list_bins(Path(flight_dir))
    except Exception:
        return None
    for path in reversed(bins):
        try:
            fix = binlog.read_origin_params(path)
        except Exception:
            continue
        if fix:
            return fix
    return None


def coverage(flight_dir: Path, windows) -> list[str]:
    """For each transect: which recordings and which .BIN logs span it.

    Read from the files' own heads and tails -- no full read -- so it costs
    a moment. A transect the recordings do not reach but a BIN does is said
    plainly, because that is when the BIN is the only telemetry there is.
    """
    from . import discovery, previewsource, tlog_extract
    disc = discovery.discover(flight_dir)
    spans: list[tuple[str, float, float]] = []
    if disc.mcaps:
        from . import mcap_extract
        for info in mcap_extract.probe_mcaps(disc.mcaps):
            if getattr(info, "start", None) and getattr(info, "end", None):
                spans.append((info.path.name, info.start, info.end))
    else:
        for p in disc.tlogs:
            a, b = tlog_extract.tlog_span(p)
            if a is not None:
                spans.append((p.name, a, b))
    bins = []
    for p in disc.bins:
        a, b = previewsource.local_bin_span(p)
        if a is not None:
            bins.append((p.name, a, b))
    out = []
    for name, lo, hi in windows:
        length = max(1.0, hi - lo)
        got = sum(max(0.0, min(hi, b) - max(lo, a)) for _n, a, b in spans)
        covered = min(1.0, got / length)
        by_bin = [n for n, a, b in bins if a <= hi and b >= lo]
        kind = disc.telemetry_kind or "recording"
        if covered >= 0.99:
            line = f"{name}: the {kind}s cover all of it"
        elif covered > 0:
            line = f"{name}: the {kind}s cover {covered:.0%} of it"
        else:
            line = f"{name}: NOT in any {kind}"
        if by_bin:
            line += f"; BIN {', '.join(by_bin)} covers it too"
            if covered < 0.99:
                line += (" — the autopilot's log has this stretch; the preview "
                         "on Transects can draw it from the BIN")
        elif disc.bins:
            line += "; no BIN covers it (BINs are placed by file time)"
        out.append(line)
    return out


def make(flight_dir: Path, plan, *, station_name: str = "",
         make_map: bool = True, progress: ProgressCB | None = None,
         cancel=None) -> list[str]:
    """Write one CSV per transect into <flight>/transects. Returns report lines.

    Raises RuntimeError with a sentence when there is nothing to work from.
    """
    from . import discovery

    mod = extractor()
    if mod is None:
        raise RuntimeError(MISSING)
    pipeline, _tide = mod
    flight_dir = Path(flight_dir)
    errors = plan.validate() if hasattr(plan, "validate") else []
    if errors:
        raise RuntimeError("the transects on the Transects tab need fixing first: "
                           + "; ".join(errors[:4]))
    sites = [s for s in plan.sites if s.transects]
    if not sites:
        raise RuntimeError("no transects are saved for this flight")
    disc = discovery.discover(flight_dir)
    sources = disc.telemetry
    if not sources:
        raise RuntimeError("no mcap or tlog in this flight folder to read the "
                           "transects from" + (" — only .BIN logs" if disc.bins else ""))
    kind = disc.telemetry_kind
    fix = origin_fallback(flight_dir)
    manual_origin = (fix.lat, fix.lon) if fix else None
    sid = station_id(station_name)
    prefix = len(sites) > 1

    lines = [f"1 Hz transect CSVs from {len(sources)} {kind}(s): "
             + ", ".join(p.name for p in sources[:6])
             + (" …" if len(sources) > 6 else "")]
    if fix and not fix.confirmed:
        lines.append(f"! ORIGIN_LAT/ORIGIN_LON from the vehicle's BIN "
                     f"({fix.lat:.6f}, {fix.lon:.6f}) seed the DVL track where the "
                     f"recording has no fix; the EKF never confirmed it, so treat "
                     f"the map as dead reckoning from there.")
    for i, site in enumerate(sites):
        if cancel is not None and cancel.is_set():
            raise Stopped(f"before site {site.name}")
        base, span = i / len(sites), 1.0 / len(sites)

        def report(f, m, b=base, s=span):
            if cancel is not None and cancel.is_set():
                raise Stopped(f"while {m}" if m else "")
            if progress:
                progress(b + s * f, m)

        specs = [pipeline.TransectSpec(
                     f"{site.name}_{t.name}" if prefix else t.name,
                     [(t.start_tc, t.end_tc)],
                     pauses=[(p.start_tc, p.end_tc) for p in t.pauses])
                 for t in site.transects]
        out = flight_dir / site.name if prefix else flight_dir
        result = pipeline.run(sources, site_name=site.name,
                              survey_date=site.date.replace("-", ""),
                              station_id=sid, save_location=out, transects=specs,
                              make_map=make_map, manual_origin=manual_origin,
                              progress=report)
        lines.append(f"{site.name}: " + result.summary_lines()[0])
        lines += [f"   {r.message}" for r in result.results]
        if result.map_path:
            lines.append(f"   map: {result.map_path.name}")
        if not result.tide_ok:
            lines.append(f"   Depth_std blank — {result.tide_error}")
        lines += [f"   ! {w}" for w in (result.warnings + result.read.warnings)[:8]]
    return lines
