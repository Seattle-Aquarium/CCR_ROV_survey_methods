"""Re-run just the compose stage from the existing cache, faithfully."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import compose as C, ffmpeg_tools as ff, overlay, rov_video
from utc.config import AppConfig, RENDITIONS
from utc.pipeline import cache_dir_for, describe_chapters
from utc.survey import Site, SurveyPlan, Transect, resolve_plan
from utc.telemetry import TelemetryStore

FLIGHT = Path(r"C:\Users\randellz\Seattle Aquarium Dropbox\Coastal_Climate_Resilience"
              r"\flights\testing\2026_08_21_DeepSea_light_testing")
app = AppConfig()
cache = cache_dir_for(FLIGHT, app.cache_root)
print("cache:", cache)

store = TelemetryStore.load(cache / "telemetry.csv")
rov = rov_video.prepare(cache, 23.976, progress=lambda f, m: None)
chapters = describe_chapters([FLIGHT / "video" / "GX014075.MP4",
                              FLIGHT / "video" / "GX024075.MP4"])
plan = SurveyPlan([Site("DeepSea", "testing", "2026-08-21",
                        [Transect("T1", "13:40:00", "13:40:40")])])
r = resolve_plan(plan, chapters)[0]
print(f"segments: {[(s.chapter.path.name, round(s.in_s,2), round(s.dur_s,2)) for s in r.segments]}")

ovl = cache / "overlay" / r.output_stem("dbg")
dur = sum(s.dur_s for s in r.segments)


def footer(epoch: float) -> str:
    import datetime as dt
    return f"{r.site.project}  |  {r.site.name}  |  {r.transect.name}  |  " \
           f"{dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime('%H:%M:%S')} UTC"


seq = overlay.render_sequence(ovl, store, r.epoch_start, dur, app.layout,
                              footer_text=footer, progress=lambda f, m: None)
print(f"seq: {seq.frames} frames  panel={seq.panel_size} gauge={seq.gauge_size} "
      f"footer={seq.footer_size}")
print("layout:", C._overlay_layout(app.layout, seq))

out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
out = C.compose_transect(
    resolved=r, seq=seq, rov=rov, out_dir=out_dir,
    scratch=cache / "scratch", app=app,
    rendition=RENDITIONS["720p"],
    progress=lambda f, m: print(f"\r  {f*100:5.1f}% {m[:60]:<60}", end="", flush=True),
)
print(f"\nwrote {out}  ({out.stat().st_size/1e6:.1f} MB)")
