"""
The dive profile, drawn for the transect sanity check.

Depth against time for the whole flight, with the transect windows shaded and
labelled. It exists for one purpose: before imagery is pulled off a card --
and the card is wiped -- the user needs to see that the times they typed land
on the part of the dive they think they do.

Drawn with Pillow rather than matplotlib on purpose. The packaged executable
excludes matplotlib to stay near 87 MB, and pulling it back in costs roughly
another 50 MB for one figure. Pillow is already bundled for the overlays, and
drawing it here keeps the plot on the same palette as the rest of the app.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import brand
from .telemetry import TelemetryStore

#: ArduSub reports height above the surface in mm, negative once submerged.
DEPTH_FIELD = "GLOBAL_POSITION_INT.relative_alt"


@dataclass
class PlotStyle:
    """Colours and geometry. Defaults match the dark GUI; `light()` flips it."""

    bg: str = brand.FATHOM
    panel: str = "#132C4C"
    grid: str = "#2A4A73"
    axis: str = "#A8BBD4"
    text: str = brand.WHITE
    muted: str = "#A8BBD4"
    trace: str = brand.SEAFOAM
    band: str = brand.ALGAE
    band_alpha: float = 0.20
    band_edge: str = brand.ALGAE
    label_on_band: str = brand.FATHOM

    pad_left: int = 68
    pad_right: int = 18
    pad_top: int = 34
    pad_bottom: int = 40

    @classmethod
    def light(cls) -> PlotStyle:
        return cls(
            bg=brand.WHITE, panel="#F7F7F7", grid="#CDCDCD", axis=brand.STONE,
            text=brand.STONE, muted=brand.STONE_TINTS[60],
            trace=brand.MEDITERRANEAN, band="#00795A", band_edge="#00795A",
            label_on_band=brand.WHITE,
        )


def _font(size: int, weight: str = "medium") -> ImageFont.FreeTypeFont:
    p = brand.font_path(weight)
    if p:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def depth_series(store: TelemetryStore) -> tuple[np.ndarray, np.ndarray] | None:
    """(epoch, metres below surface). None when the field is absent."""
    s = store.series.get(DEPTH_FIELD)
    if s is None or len(s.t) == 0:
        return None
    t = np.asarray(s.t, dtype=np.float64)
    v = np.asarray(s.v, dtype=np.float64)
    ok = np.isfinite(v)
    if not ok.any():
        return None
    return t[ok], -v[ok] / 1000.0


def _nice_step(span: float, target: int) -> float:
    """A round-numbered tick step near span/target."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target)
    mag = 10.0 ** np.floor(np.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def render_profile(
    store: TelemetryStore,
    windows: Sequence[tuple[str, float, float]] = (),
    *,
    width: int = 1000,
    height: int = 340,
    style: PlotStyle | None = None,
    tz_offset_hours: float = -7.0,
    scale: int = 2,
) -> Image.Image:
    """Draw the dive profile with the transects marked.

    Rendered at `scale` and downsampled, because Pillow does not antialias
    lines and a 1px trace on a plot is exactly where that shows.
    """
    st = style or PlotStyle()
    W, H = width * scale, height * scale
    im = Image.new("RGB", (W, H), st.bg)
    d = ImageDraw.Draw(im, "RGBA")

    L, R = st.pad_left * scale, st.pad_right * scale
    Tp, B = st.pad_top * scale, st.pad_bottom * scale
    x0, x1 = L, W - R
    y0, y1 = Tp, H - B
    d.rectangle([x0, y0, x1, y1], fill=st.panel)

    f_tick = _font(11 * scale, "regular")
    f_lab = _font(12 * scale, "semibold")

    series = depth_series(store)
    if series is None:
        d.text(((x0 + x1) // 2 - 90 * scale, (y0 + y1) // 2),
               "no depth telemetry in this recording",
               font=f_lab, fill=st.muted)
        return im.resize((width, height), Image.LANCZOS)

    t, depth = series
    t_lo, t_hi = float(t.min()), float(t.max())
    if windows:
        t_lo = min(t_lo, min(w[1] for w in windows))
        t_hi = max(t_hi, max(w[2] for w in windows))
    if t_hi <= t_lo:
        t_hi = t_lo + 1.0
    d_hi = max(1.0, float(np.nanmax(depth)))
    d_hi *= 1.08                                    # headroom under the deepest point

    def px(ts: float) -> float:
        return x0 + (ts - t_lo) / (t_hi - t_lo) * (x1 - x0)

    def py(m: float) -> float:
        return y0 + (m / d_hi) * (y1 - y0)           # depth increases downward

    # ---- grid ------------------------------------------------------------
    tz = timezone(timedelta(hours=tz_offset_hours))
    dstep = _nice_step(d_hi, 5)
    m = 0.0
    while m <= d_hi + 1e-9:
        y = py(m)
        d.line([x0, y, x1, y], fill=st.grid, width=max(1, scale // 2))
        d.text((x0 - 10 * scale, y - 7 * scale), f"{m:g}",
               font=f_tick, fill=st.muted, anchor="ra")
        m += dstep

    tstep = _nice_step(t_hi - t_lo, 6)
    tstep = max(60.0, round(tstep / 60.0) * 60.0)    # whole minutes read better
    first = np.ceil(t_lo / tstep) * tstep
    ts = first
    while ts <= t_hi:
        x = px(ts)
        d.line([x, y0, x, y1], fill=st.grid, width=max(1, scale // 2))
        d.text((x, y1 + 8 * scale),
               datetime.fromtimestamp(ts, tz).strftime("%H:%M"),
               font=f_tick, fill=st.muted, anchor="ma")
        ts += tstep

    d.text((x0 - 10 * scale, y0 - 20 * scale), "depth (m)",
           font=f_tick, fill=st.muted, anchor="la")

    # ---- transect bands, behind the trace ---------------------------------
    br, bg_, bb = brand.hex_to_rgb(st.band)
    fill = (br, bg_, bb, int(st.band_alpha * 255))
    for name, a, b in windows:
        xa, xb = px(max(a, t_lo)), px(min(b, t_hi))
        if xb <= xa:
            continue
        d.rectangle([xa, y0, xb, y1], fill=fill)
        d.line([xa, y0, xa, y1], fill=st.band_edge, width=max(1, scale))
        d.line([xb, y0, xb, y1], fill=st.band_edge, width=max(1, scale))
        mins = (b - a) / 60.0
        tag = f"{name}  {mins:.1f} min"
        tw = d.textlength(tag, font=f_lab)
        cx = (xa + xb) / 2
        pad = 5 * scale
        box = [cx - tw / 2 - pad, y0 + 4 * scale,
               cx + tw / 2 + pad, y0 + 4 * scale + 18 * scale]
        d.rectangle(box, fill=st.band_edge)
        d.text((cx, box[1] + 9 * scale), tag, font=f_lab,
               fill=st.label_on_band, anchor="mm")

    # ---- the trace --------------------------------------------------------
    step = max(1, len(t) // (2 * (x1 - x0)))          # ~2 points per pixel
    pts = [(px(float(a)), py(float(b))) for a, b in zip(t[::step], depth[::step], strict=True)]
    if len(pts) > 1:
        d.line(pts, fill=st.trace, width=max(2, scale + 1), joint="curve")

    d.rectangle([x0, y0, x1, y1], outline=st.axis, width=max(1, scale // 2))
    return im.resize((width, height), Image.LANCZOS)


def transect_stats(
    store: TelemetryStore, windows: Sequence[tuple[str, float, float]]
) -> list[dict]:
    """Per-transect summary for the table beside the plot."""
    series = depth_series(store)
    out: list[dict] = []
    for name, a, b in windows:
        row: dict = {"name": name, "seconds": max(0.0, b - a)}
        if series is not None:
            t, depth = series
            m = (t >= a) & (t <= b)
            if m.any():
                row["depth_min"] = float(np.nanmin(depth[m]))
                row["depth_max"] = float(np.nanmax(depth[m]))
                row["depth_mean"] = float(np.nanmean(depth[m]))
        out.append(row)
    return out
