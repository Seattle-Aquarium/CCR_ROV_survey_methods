"""
End-to-end orchestration.

Everything the GUI needs sits behind `run()`: discovery, extraction, sync
verification, the 1 Hz CSV, and one composite per transect per resolution.

Design notes:

* Progress is reported as a single 0..1 fraction with a message, computed from
  weighted stages, so the GUI needs no knowledge of the internals.
* A failure in one transect does not abandon the rest of the run -- it is
  recorded and the next one is attempted. A field user with six transects should
  not lose five of them to one bad set of times.
* Everything expensive is cached under `cache_root`, keyed by flight folder, so
  a second run (different resolution, corrected times) skips straight to
  compositing.
"""

from __future__ import annotations

import hashlib
import shutil
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import compose as compose_mod
from . import csv_export, discovery, ffmpeg_tools as ff, mcap_extract, overlay
from . import photos as photos_mod
from . import rov_video, sync as sync_mod
from .config import AppConfig, RENDITIONS
from .power import keep_awake
from .survey import (
    Chapter, ResolvedTransect, SurveyPlan, format_hhmmss, local_midnight_epoch,
    resolve_plan, utc_offset_hours,
)
from .telemetry import TelemetryStore

ProgressCB = Callable[[float, str], None]


@dataclass
class RunRequest:
    flight_dir: Path
    plan: SurveyPlan
    renditions: tuple[str, ...] = ("1080p",)
    app: AppConfig = field(default_factory=AppConfig)
    write_csv: bool = True
    force_extract: bool = False
    #: Stamp telemetry onto the flight's stills as well as the video.
    process_photos: bool = False
    #: What to do with stills outside every transect: keep / move / delete.
    off_transect: str = photos_mod.KEEP


@dataclass
class RunResult:
    outputs: list[Path] = field(default_factory=list)
    csv_path: Path | None = None
    sync: sync_mod.SyncReport = field(default_factory=sync_mod.SyncReport)
    resolved: list[ResolvedTransect] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    cancelled: bool = False
    photos: photos_mod.PhotoReport | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and not self.cancelled

    def summary(self) -> str:
        lines = []
        if self.cancelled:
            lines.append("Run cancelled.")
        lines.append(f"{len(self.outputs)} composite(s) written "
                     f"in {self.elapsed_s / 60:.1f} min")
        for p in self.outputs:
            try:
                lines.append(f"   {p.name}  ({p.stat().st_size / 1e6:.0f} MB)")
            except OSError:
                lines.append(f"   {p.name}")
        if self.csv_path:
            lines.append(f"telemetry CSV: {self.csv_path.name}")
        if self.photos is not None:
            lines.append(self.photos.summary())
        if self.sync.checked:
            lines.append(self.sync.summary())
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        for e in self.errors:
            lines.append(f"ERROR: {e}")
        return "\n".join(lines)


class _Stages:
    """Weighted progress across the pipeline."""

    def __init__(self, cb: ProgressCB | None):
        self.cb = cb
        self.weights: dict[str, float] = {}
        self.done: dict[str, float] = {}

    def plan(self, **weights: float) -> None:
        self.weights = dict(weights)
        self.done = {k: 0.0 for k in weights}

    def sub(self, name: str) -> ProgressCB:
        def cb(frac: float, msg: str = "") -> None:
            # Never let a stage's progress go backwards. Sub-steps that each
            # count 0..1 would otherwise rewind the bar, which reads as a hang.
            self.done[name] = max(self.done.get(name, 0.0),
                                  max(0.0, min(1.0, frac)))
            self._emit(msg)
        return cb

    def finish(self, name: str, msg: str = "") -> None:
        self.done[name] = 1.0
        self._emit(msg)

    def _emit(self, msg: str) -> None:
        if not self.cb:
            return
        total = sum(self.weights.values()) or 1.0
        acc = sum(self.weights[k] * self.done.get(k, 0.0) for k in self.weights)
        self.cb(acc / total, msg)


def cache_dir_for(flight_dir: Path, root: Path) -> Path:
    """A stable per-flight cache path.

    Keyed by name plus a hash of the full path, so two flights that happen to
    share a folder name (``2026`` under different projects) do not collide.
    """
    h = hashlib.sha1(str(Path(flight_dir).resolve()).encode("utf-8")).hexdigest()[:8]
    return Path(root) / f"{Path(flight_dir).name}_{h}"


def describe_chapters(paths: Sequence[Path], ffmpeg: str | None = None) -> list[Chapter]:
    """Probe each GoPro file and place it on the TC-25 clock."""
    out: list[Chapter] = []
    for p in paths:
        mi = ff.probe(p, ffmpeg=ffmpeg)
        tc = ff.timecode_to_seconds(mi.timecode, mi.fps)
        out.append(Chapter(
            path=Path(p),
            duration=mi.duration or 0.0,
            fps=mi.fps or 23.976,
            width=mi.width or 3840,
            height=mi.height or 2160,
            rotation=mi.rotation,
            tc_start_s=tc,
        ))
    return out


def run(
    req: RunRequest,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
) -> RunResult:
    """Execute a full job, keeping the machine awake while it works.

    Never raises for expected problems -- inspect the returned `RunResult`.
    """
    with keep_awake() as awake:
        res = _run(req, progress=progress, cancel=cancel)
    if not awake:
        res.warnings.append(
            "Could not stop this machine from sleeping during the run. If it "
            "slept, the encode paused until it woke and the run took longer "
            "than it needed to."
        )
    return res


def _run(
    req: RunRequest,
    *,
    progress: ProgressCB | None = None,
    cancel=None,
) -> RunResult:
    res = RunResult()
    started = time.time()
    st = _Stages(progress)

    try:
        # ---- 1. discovery -------------------------------------------
        st.plan(discover=1, extract=22, rov=18, sync=14, csv=5,
                photos=12 if req.process_photos else 0,
                render=40)
        disc = discovery.discover(req.flight_dir)
        res.warnings.extend(disc.warnings)
        if not disc.mcaps:
            res.errors.append("No .mcap telemetry found in the flight folder.")
            return _finish(res, started)
        if not disc.videos:
            res.errors.append("No downward GoPro video found in the flight folder.")
            return _finish(res, started)
        cloud = discovery.check_local(list(disc.mcaps) + disc.video_paths)
        if cloud:
            # Proceeding would appear to hang for hours, so stop and say why.
            res.errors.append(
                "Some inputs are still in the cloud rather than on this "
                "machine:\n  " + "\n  ".join(cloud)
            )
            return _finish(res, started)
        st.finish("discover", f"found {len(disc.mcaps)} mcap(s), "
                              f"{len(disc.videos)} video file(s)")

        errs = req.plan.validate()
        if errs:
            res.errors.extend(errs)
            return _finish(res, started)

        cache = cache_dir_for(req.flight_dir, req.app.cache_root)
        cache.mkdir(parents=True, exist_ok=True)
        ffmpeg = ff.find_ffmpeg()

        # ---- 2. mcap -------------------------------------------------
        ex = mcap_extract.extract(disc.mcaps, cache, progress=st.sub("extract"),
                                  force=req.force_extract)
        res.warnings.extend(ex.warnings)
        if ex.video.frames == 0:
            res.errors.append(
                "The mcap contains no video stream, so there is no inset to "
                "composite. Check that the recorder was capturing video."
            )
            return _finish(res, started)
        store = TelemetryStore.load(ex.telemetry_csv)
        st.finish("extract", f"{ex.video.frames:,} ROV frames, "
                             f"{len(store.series)} telemetry fields")

        # ---- 3. chapters + transects ---------------------------------
        # Transects are resolved BEFORE the proxy is built, so the proxy can
        # cover only the span they need instead of the whole recording.
        chapters = describe_chapters(disc.video_paths, ffmpeg)
        gopro_fps = next((c.fps for c in chapters if c.fps), 23.976)

        res.resolved = resolve_plan(req.plan, chapters)
        for r in res.resolved:
            for w in r.warnings:
                res.warnings.append(f"{r.site.name}/{r.transect.name}: {w}")

        renderable = [r for r in res.resolved if r.segments]
        if not renderable:
            res.errors.append(
                "None of the transect times fall inside the recorded video. "
                "Check the TC-25 times and the flight date."
            )

        # ---- 4. ROV proxy over just the needed span ------------------
        needed = [(r.epoch_start, r.epoch_end) for r in renderable]
        rov = rov_video.prepare(
            cache, gopro_fps, needed_epochs=needed,
            codec=req.app.proxy_codec, crf=req.app.proxy_crf,
            preset=req.app.proxy_preset, use_gpu=req.app.proxy_use_gpu,
            progress=st.sub("rov"), force=req.force_extract, cancel=cancel,
        )
        res.warnings.extend(rov.warnings)
        st.finish("rov", "ROV proxy ready")

        # ---- 5. sync check -------------------------------------------
        first_date = req.plan.sites[0].date_obj()
        midnight = local_midnight_epoch(first_date, req.plan.timezone)
        offset_h = (req.app.sync.utc_offset_hours
                    if req.app.sync.utc_offset_hours is not None
                    else utc_offset_hours(first_date, req.plan.timezone))
        res.sync = sync_mod.validate(
            chapters, store.lights_series(), midnight, cache, req.app.sync,
            ffmpeg=ffmpeg, progress=st.sub("sync"), cancel=cancel,
        )
        res.warnings.extend(res.sync.warnings)
        st.finish("sync", res.sync.message or "sync checked")

        # ---- 6. telemetry CSV ----------------------------------------
        _composites, logs_dir = discovery.output_dirs(req.flight_dir, create=True)
        if req.write_csv:
            stem = f"{first_date.isoformat()}_{_slug(req.plan.sites[0].project)}_telemetry_1Hz"
            try:
                out = csv_export.export_1hz(
                    store, logs_dir / f"{stem}.csv",
                    plan=req.plan, resolved=res.resolved,
                    utc_offset_hours=offset_h,
                    progress=st.sub("csv"), cancel=cancel,
                )
                res.csv_path = out.path
            except ff.CancelledError:
                raise
            except Exception as ex_:
                res.warnings.append(f"telemetry CSV failed: {ex_}")
        st.finish("csv", "telemetry CSV written" if res.csv_path else "CSV skipped")

        # ---- 6b. flight stills ---------------------------------------
        # Photos are placed on the timeline from their own EXIF, so this needs
        # nothing from the video path -- but the transect windows come from the
        # resolved transects, which have been checked against the lights.
        if req.process_photos:
            photo_dir = photos_mod.find_photo_dir(disc.photos_dir)
            if photo_dir is None:
                res.warnings.append(
                    "Photo stamping was requested but no stills were found "
                    "under the flight's photos/ folder."
                )
            else:
                windows = [(r.transect.name, r.epoch_start, r.epoch_end)
                           for r in res.resolved]
                if not windows:
                    res.warnings.append(
                        "No transect resolved, so no still could be assigned "
                        "to one; photos were left untouched."
                    )
                else:
                    try:
                        rep = photos_mod.process_flight(
                            photo_dir, store, windows,
                            off_transect=req.off_transect,
                            progress=st.sub("photos"), cancel=cancel,
                        )
                        res.photos = rep
                        res.warnings.extend(rep.warnings)
                        res.errors.extend(rep.errors)
                    except ff.CancelledError:
                        raise
                    except Exception as ex_:
                        res.warnings.append(f"photo stamping failed: {ex_}")
        st.finish("photos", "photos done" if res.photos else "photos skipped")

        # ---- 7. composites -------------------------------------------
        rends = [RENDITIONS[k] for k in req.renditions if k in RENDITIONS]
        if not rends:
            res.errors.append("No output resolution selected.")
            return _finish(res, started)

        jobs = [(r, rd) for r in renderable for rd in rends]
        if jobs:
            per = 1.0 / len(jobs)
            for i, (r, rd) in enumerate(jobs):
                if cancel is not None and cancel.is_set():
                    raise ff.CancelledError("cancelled")
                base = i * per
                label = f"{r.site.name}/{r.transect.name} {rd.label}"

                def jp(frac: float, msg: str = "", _b=base) -> None:
                    st.sub("render")(_b + frac * per, msg or label)

                try:
                    out = _render_one(r, rd, rov, store, cache, _composites,
                                      req.app, jp, cancel)
                    res.outputs.append(out)
                except ff.CancelledError:
                    raise
                except Exception as ex_:
                    res.errors.append(f"{label}: {ex_}")
                    ff.log_cb and ff.log_cb(traceback.format_exc())
        st.finish("render", "composites complete")

    except ff.CancelledError:
        res.cancelled = True
    except Exception as ex_:                      # unexpected: report, don't crash
        res.errors.append(f"{type(ex_).__name__}: {ex_}")

    return _finish(res, started)


def _render_one(
    r: ResolvedTransect,
    rd,
    rov: rov_video.RovVideo,
    store: TelemetryStore,
    cache: Path,
    out_dir: Path,
    app: AppConfig,
    progress: ProgressCB,
    cancel,
) -> Path:
    """Overlays + composite for one transect at one resolution."""
    dur = sum(s.dur_s for s in r.segments)
    ovl_dir = cache / "overlay" / f"{r.output_stem('x')}"

    def footer(epoch: float) -> str:
        import datetime as _dt
        clock = _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc)
        return (f"{r.site.project}  |  {r.site.name}  |  {r.transect.name}"
                f"  |  {clock.strftime('%H:%M:%S')} UTC")

    def op(f: float, m: str = "") -> None:
        progress(f * 0.35, m)

    seq = overlay.render_sequence(
        ovl_dir, store, r.epoch_start, dur, app.layout,
        footer_text=footer if app.layout.show_footer else None,
        progress=op, cancel=cancel,
    )

    def cp(f: float, m: str = "") -> None:
        progress(0.35 + f * 0.65, m)

    return compose_mod.compose_transect(
        resolved=r, seq=seq, rov=rov, out_dir=out_dir,
        scratch=cache / "scratch", app=app, rendition=rd,
        progress=cp, cancel=cancel,
    )


def _slug(s: str) -> str:
    import re
    s = re.sub(r"[\\/:*?\"<>|]+", "", str(s)).strip()
    return re.sub(r"\s+", "-", s) or "unnamed"


def _finish(res: RunResult, started: float) -> RunResult:
    res.elapsed_s = time.time() - started
    return res
