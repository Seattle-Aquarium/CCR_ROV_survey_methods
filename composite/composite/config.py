"""
Pipeline configuration.

Defaults live here; the GUI overrides a handful of them per run. Anything a
field user would plausibly want to change should be reachable from the GUI
rather than only from this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import brand

# --------------------------------------------------------------------------
#  Output resolutions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rendition:
    key: str
    label: str
    height: int | None      # None = native (4K)
    codec: str
    crf: int
    preset: str
    pix_fmt: str
    extra: tuple[str, ...] = ()

    @property
    def suffix(self) -> str:
        return self.label


#: Selectable in the GUI. 4K keeps 10-bit so the Native white balance survives;
#: 720p is 8-bit H.264 because 10-bit HEVC often will not play in browsers,
#: phones or chat clients.
RENDITIONS: dict[str, Rendition] = {
    "4K": Rendition("4K", "4K", None, "libx265", 16, "medium", "yuv420p10le",
                    ("-tag:v", "hvc1")),
    "1080p": Rendition("1080p", "1080p", 1080, "libx265", 20, "medium", "yuv420p10le",
                       ("-tag:v", "hvc1")),
    "720p": Rendition("720p", "720p", 720, "libx264", 24, "medium", "yuv420p",
                      ("-profile:v", "high", "-movflags", "+faststart")),
}

DEFAULT_RENDITIONS = ("1080p",)


# --------------------------------------------------------------------------
#  Overlay layout  (matches the v1 R pipeline, which the user signed off on)
# --------------------------------------------------------------------------


@dataclass
class Layout:
    """Geometry of the overlay strip, in pixels at 4K (3840x2160).

    Left to right: ROV inset, stacked gauges, telemetry panel. The whole right
    half of the frame is deliberately left clear.
    """

    margin: int = 40
    border_px: int = 4
    border_color: str = brand.WHITE

    # ROV camera inset
    inset_width: int = 1100
    inset_gap: int = 24

    # gauges
    show_gauges: bool = True
    gauge_diam: int = 200
    gauge_pad: int = 20
    gauge_caption_h: int = 62
    gauge_gap: int = 24

    # telemetry panel
    panel_pad_x: int = 28
    panel_pad_y: int = 22
    panel_width: int | None = None      # None = size to the text
    value_gap: int = 26                 # gap between label column and value column
    unit_gap: int = 10

    # type
    text_size: int = 34
    #: Multiplier on the font's own ascent+descent. Tuned so the 13-row panel
    #: sits within the height of the ROV inset beside it, keeping the top strip
    #: visually level.
    line_spacing: float = 1.04
    footer_size: int = 40
    show_footer: bool = True

    # colours (semi-transparent brand ground so the video reads through)
    panel_bg: str = "#000000"
    panel_bg_alpha: float = 0.60
    panel_fg: str = brand.WHITE
    panel_muted: str = "#C9D6E4"

    # gauge colours
    gauge_face: str = "#00000022"
    gauge_fg: str = brand.WHITE
    gauge_dim: str = "#FFFFFF73"
    gauge_index: str = "#FFC24D"
    gauge_north: str = brand.CORAL
    gauge_sky: str = brand.MEDITERRANEAN
    gauge_sky_alpha: float = 0.80
    gauge_ground: str = "#6B563A"
    gauge_ground_alpha: float = 0.90
    gauge_pitch_px: float = 2.6

    #: Overlay redraw rate. The MAVLink streams update at 2-10 Hz, so drawing
    #: every video frame would be ~4x the work for no visible difference.
    overlay_fps: float = 6.0

    def inset_height(self, src_w: int = 1920, src_h: int = 1080) -> int:
        """Height the inset scales to; ffmpeg's -2 rounds to an even number."""
        return 2 * round(self.inset_width * src_h / src_w / 2)


# --------------------------------------------------------------------------
#  Telemetry panel contents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    label: str
    var: str
    unit: str = ""
    digits: int | None = 1
    spacer: bool = False


SPACER = Row("", "", spacer=True)

#: Order agreed with the user. Every `var` must be produced by
#: telemetry.TelemetryStore.sample().
PANEL_ROWS: tuple[Row, ...] = (
    Row("ALTITUDE", "altitude", "m", 2),
    Row("SPEED", "speed", "m/s", 2),
    Row("DEPTH", "depth", "m", 2),
    SPACER,
    Row("MODE", "mode", "", None),
    Row("LIGHTS", "lights", "%", 0),
    Row("GAIN", "gain", "%", 0),
    Row("CAM TILT", "cam_tilt", "", 2),
    Row("TEMP", "temp_c", "C", 1),
    SPACER,
    Row("POWER", "power_w", "W", 0),
    Row("BATTERY", "voltage_v", "V", 2),
    Row("CURRENT", "current_a", "A", 1),
)


# --------------------------------------------------------------------------
#  Sync / validation
# --------------------------------------------------------------------------


@dataclass
class SyncConfig:
    """How the GoPro clock is tied to the mcap clock.

    Production footage is synced with GoPro Labs precision time, so the camera's
    timecode track *is* the answer -- we only need the UTC offset that was in
    force locally, which is derived from the flight date rather than typed in.

    The light-based check then confirms it: the ROV's own lights are ramped to
    full at the start of a transect and back to zero before ascending, and both
    recorders see that. `max_residual_s` is how far apart the two may land
    before the run is flagged.
    """

    timezone: str = "America/Los_Angeles"
    utc_offset_hours: float | None = None   # None = derive from the flight date
    validate_with_lights: bool = True
    max_residual_s: float = 3.0
    dark_luma: float = 35.0
    lights_off: float = 0.05
    min_overlap_frac: float = 0.60


# --------------------------------------------------------------------------
#  Paths
# --------------------------------------------------------------------------


def default_cache_root() -> Path:
    """Bulk intermediates live outside the flight folder.

    A flight's cache is ~4 GB (remuxed ROV video plus its constant-rate proxy).
    Writing that into the Dropbox-synced flight folder would push gigabytes of
    disposable working files to the whole team.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "ccr_composite_cache"


@dataclass
class AppConfig:
    layout: Layout = field(default_factory=Layout)
    sync: SyncConfig = field(default_factory=SyncConfig)
    cache_root: Path = field(default_factory=default_cache_root)
    renditions: tuple[str, ...] = DEFAULT_RENDITIONS
    theme: str = "dark"

    # ROV constant-rate proxy: only ever scaled down into the inset, so crf 18
    # is well beyond visible.
    #
    # Two things keep this cheap. It is encoded on the GPU when NVENC is
    # available -- the proxy's quality-per-bit genuinely does not matter, and
    # NVENC is ~2.7x faster. And only the span the transects need is built:
    # on a 59-minute dive with four 2-minute transects, 84% of a whole-recording
    # proxy is never read.
    proxy_codec: str = "libx264"          # CPU fallback when NVENC is absent
    proxy_crf: int = 18
    proxy_preset: str = "veryfast"
    proxy_use_gpu: bool = True

    #: The GoPro audio track is thruster whine.
    keep_audio: bool = False
