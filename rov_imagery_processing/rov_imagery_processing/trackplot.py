"""
The reconstructed transect, drawn from above.

A plan view of where the ROV actually went, with the distance marks on it. It
answers the questions a table of numbers does not: did the run hold a line, did
it double back, and is the mark spacing even or bunched where the vehicle
slowed. A paused transect shows the pause as a break in the marks while the
track itself stays whole, which is what makes a mistyped pause obvious.

Pillow rather than matplotlib, for the reason given in `depthplot` -- the
packaged executable leaves matplotlib out to stay near 87 MB, and one figure is
not worth ~50 MB to put it back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from . import brand
from .depthplot import _font, _nice_step
from .metermark import Reconstruction


@dataclass
class TrackStyle:
    """Defaults match the dark GUI; `light()` flips it for a file on disk."""

    bg: str = brand.FATHOM
    panel: str = "#132C4C"
    grid: str = "#2A4A73"
    axis: str = "#A8BBD4"
    text: str = brand.WHITE
    muted: str = "#A8BBD4"
    trace: str = brand.SEAFOAM
    pause: str = "#2A4A73"
    mark: str = brand.ALGAE
    mark_unmatched: str = brand.CORAL
    start: str = brand.ALGAE
    end: str = brand.PURPLE_STAR

    pad_left: int = 62
    pad_right: int = 18
    pad_top: int = 46
    pad_bottom: int = 46

    @classmethod
    def light(cls) -> TrackStyle:
        return cls(
            bg=brand.WHITE, panel="#F7F7F7", grid="#CDCDCD", axis=brand.STONE,
            text=brand.STONE, muted=brand.STONE_TINTS[60],
            trace=brand.MEDITERRANEAN, pause="#C9C9C9", mark="#00795A",
            mark_unmatched="#C4452F", start="#00795A", end=brand.PURPLE_STAR,
        )


def _label_stride(n: int, target: int = 12) -> int:
    """Show about `target` labels however many marks there are."""
    return max(1, round(n / target)) if n else 1


def render_track(
    rec: Reconstruction,
    *,
    width: int = 900,
    height: int = 760,
    style: TrackStyle | None = None,
    tz_offset_hours: float = -7.0,
    scale: int = 2,
) -> Image.Image:
    """Plan view of one reconstructed transect, east right and north up."""
    st = style or TrackStyle()
    W, H = width * scale, height * scale
    im = Image.new("RGB", (W, H), st.bg)
    d = ImageDraw.Draw(im, "RGBA")

    L, R = st.pad_left * scale, st.pad_right * scale
    Tp, B = st.pad_top * scale, st.pad_bottom * scale
    x0, x1, y0, y1 = L, W - R, Tp, H - B
    d.rectangle([x0, y0, x1, y1], fill=st.panel)

    f_tick = _font(11 * scale, "regular")
    f_lab = _font(12 * scale, "semibold")
    f_title = _font(15 * scale, "semibold")

    track = rec.track
    if track is None or len(track.t) < 2:
        d.text(((x0 + x1) // 2, (y0 + y1) // 2),
               f"{rec.name}: no usable telemetry in this window",
               font=f_lab, fill=st.muted, anchor="mm")
        return im.resize((width, height), Image.LANCZOS)

    # ---- equal aspect, so a straight line looks straight -----------------
    east, north = track.y, track.x
    e_lo, e_hi = float(east.min()), float(east.max())
    n_lo, n_hi = float(north.min()), float(north.max())
    span = max(e_hi - e_lo, n_hi - n_lo, 1.0) * 1.12
    e_mid, n_mid = (e_lo + e_hi) / 2, (n_lo + n_hi) / 2
    plot_w, plot_h = x1 - x0, y1 - y0
    unit = min(plot_w, plot_h) / span

    def px(e: float) -> float:
        return (x0 + x1) / 2 + (e - e_mid) * unit

    def py(n: float) -> float:
        return (y0 + y1) / 2 - (n - n_mid) * unit      # north is up

    # ---- grid at round meters --------------------------------------------
    step = _nice_step(span, 6)
    g = math.floor((e_mid - span / 2) / step) * step
    while g <= e_mid + span / 2:
        x = px(g)
        if x0 < x < x1:
            d.line([x, y0, x, y1], fill=st.grid, width=max(1, scale // 2))
            d.text((x, y1 + 7 * scale), f"{g:g}", font=f_tick,
                   fill=st.muted, anchor="ma")
        g += step
    g = math.floor((n_mid - span / 2) / step) * step
    while g <= n_mid + span / 2:
        y = py(g)
        if y0 < y < y1:
            d.line([x0, y, x1, y], fill=st.grid, width=max(1, scale // 2))
            d.text((x0 - 8 * scale, y - 7 * scale), f"{g:g}", font=f_tick,
                   fill=st.muted, anchor="ra")
        g += step
    d.text((x0 - 8 * scale, y0 - 18 * scale), "north (m)", font=f_tick,
           fill=st.muted, anchor="la")
    d.text((x1, y1 + 24 * scale), "east (m)", font=f_tick,
           fill=st.muted, anchor="ra")

    # ---- the track, pauses drawn faint ------------------------------------
    stride = max(1, len(east) // (2 * max(1, int(plot_w))))
    idx = list(range(0, len(east), stride))
    spans = track.spans or ((float(track.t[0]), float(track.t[-1])),)

    def surveying(i: int) -> bool:
        t = float(track.t[i])
        return any(lo <= t <= hi for lo, hi in spans)

    run: list[tuple[float, float]] = []
    run_live = None
    for i in idx:
        live = surveying(i)
        if run_live is None:
            run_live = live
        if live != run_live and len(run) > 1:
            d.line(run, fill=st.trace if run_live else st.pause,
                   width=max(2, scale + 1) if run_live else max(1, scale),
                   joint="curve")
            run = run[-1:]
            run_live = live
        run.append((px(float(east[i])), py(float(north[i]))))
    if len(run) > 1:
        d.line(run, fill=st.trace if run_live else st.pause,
               width=max(2, scale + 1) if run_live else max(1, scale),
               joint="curve")

    # ---- marks -----------------------------------------------------------
    matched = {m.mark.number for m in rec.matches if m.matched}
    stride_lab = _label_stride(len(rec.marks))
    r = max(2, int(1.6 * scale))
    for i, mark in enumerate(rec.marks):
        x, y = px(mark.y), py(mark.x)
        hit = mark.number in matched or not rec.matches
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=st.mark if hit else st.mark_unmatched)
        if i % stride_lab == 0:
            d.text((x + 5 * scale, y - 6 * scale), f"{mark.distance_m:g}",
                   font=f_tick, fill=st.text)

    # start and end
    for e, n, col, tag in ((east[0], north[0], st.start, "start"),
                           (east[-1], north[-1], st.end, "end")):
        x, y = px(float(e)), py(float(n))
        rr = max(4, int(3 * scale))
        d.ellipse([x - rr, y - rr, x + rr, y + rr], outline=col,
                  width=max(2, scale))
        d.text((x + 9 * scale, y + 5 * scale), tag, font=f_tick, fill=col)

    # ---- title and footer -------------------------------------------------
    tz = timezone(timedelta(hours=tz_offset_hours))
    begin = datetime.fromtimestamp(float(track.t[0]), tz).strftime("%H:%M:%S")
    d.text((x0, 12 * scale),
           f"{rec.name}   {track.length:.1f} m   {len(rec.marks)} marks",
           font=f_title, fill=st.text)
    d.text((x1, 14 * scale),
           f"from {begin}   {track.duration/60:.1f} min   "
           f"{track.mean_speed:.2f} m/s",
           font=f_tick, fill=st.muted, anchor="ra")

    foot = (f"{track.source.label} x{track.source.scale:.3f}   "
            f"geometry {track.xy_source}   "
            f"straightness {track.straightness:.2f}")
    if rec.matches:
        foot += f"   frames {rec.matched}/{len(rec.matches)}"
    if track.paused_s > 1:
        foot += f"   pause {track.paused_s:.0f}s excluded"
    d.text((x0, y1 + 26 * scale), foot, font=f_tick, fill=st.muted)

    d.rectangle([x0, y0, x1, y1], outline=st.axis, width=max(1, scale // 2))
    return im.resize((width, height), Image.LANCZOS)


def save_track_png(rec: Reconstruction, path: Path, **kw) -> Path:
    """Render and write one transect. Light palette, for viewing off-screen."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    kw.setdefault("style", TrackStyle.light())
    im = render_track(rec, **kw)
    tmp = path.with_name(path.name + ".part")
    im.save(tmp, "PNG")
    tmp.replace(path)
    return path
