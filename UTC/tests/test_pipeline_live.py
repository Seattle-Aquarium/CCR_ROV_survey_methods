"""End-to-end pipeline run against the 2026-08-21 test flight.

That flight's GoPro was NOT synced with GoPro Labs precision time -- its clock
runs ~29 minutes fast -- so this doubles as a test that the sync validator
*notices*. A clean pass here would actually be a bug.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import ffmpeg_tools as ff
from utc.config import AppConfig
from utc.pipeline import RunRequest, run
from utc.survey import Site, SurveyPlan, Transect

FLIGHT = Path(r"C:\Users\randellz\Seattle Aquarium Dropbox\Coastal_Climate_Resilience"
              r"\flights\testing\2026_08_21_DeepSea_light_testing")

# GX014075's timecode track starts at 13:37:31, so this lands ~149 s in.
plan = SurveyPlan([
    Site(name="DeepSea", project="testing", date="2026-08-21",
         transects=[Transect("T1", "13:40:00", "13:40:40")])
])

app = AppConfig()
app.renditions = ("720p",)

last = [0.0]


def prog(f: float, msg: str) -> None:
    if f - last[0] > 0.01 or f >= 1.0:
        last[0] = f
        print(f"  [{f*100:5.1f}%] {msg}", flush=True)


ff.log_cb = None
t0 = time.time()
res = run(RunRequest(flight_dir=FLIGHT, plan=plan, renditions=("720p",), app=app),
          progress=prog)
print("\n" + "=" * 70)
print(res.summary())
print("=" * 70)
print(f"\nsync checked={res.sync.checked} ok={res.sync.ok} "
      f"residual={res.sync.residual_s} agreement={res.sync.agreement} "
      f"transitions={res.sync.n_transitions}")
print(f"elapsed {time.time()-t0:.0f}s")
