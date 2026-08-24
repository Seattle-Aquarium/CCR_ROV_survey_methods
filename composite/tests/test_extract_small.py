"""Extract two small mcaps from the C3 flight and sanity-check the result."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from composite.mcap_extract import extract

LOGS = Path(r"C:\Users\randellz\Seattle Aquarium Dropbox\Coastal_Climate_Resilience\flights\testing\2026_06_12_C3-cam-testing\logs")
CACHE = Path(r"C:\Users\randellz\AppData\Local\ccr_composite_cache\_smoketest")
mcaps = [LOGS / "recorder_20260612_203245.mcap", LOGS / "recorder_20260612_203310.mcap"]

def prog(f, msg): print(f"    [{f*100:5.1f}%] {msg}", flush=True)

t0 = time.time()
r = extract(mcaps, CACHE, progress=prog, force=True)
print(f"\nelapsed {time.time()-t0:.1f}s")
print(f"video frames : {r.video.frames:,}")
print(f"resolution   : {r.video.width}x{r.video.height}")
print(f"video span   : {r.video.first_ts} .. {r.video.last_ts}")
print(f"telem rows   : {r.telemetry_rows:,}")
print(f"telem span   : {r.t_start} .. {r.t_end}  ({(r.t_end-r.t_start):.1f}s)")
print(f"h264 bytes   : {r.h264_path.stat().st_size:,}")
print(f"warnings     : {r.warnings}")

import collections
c = collections.Counter()
with open(r.telemetry_csv) as f:
    next(f)
    for line in f:
        c[line.split(",")[1]] += 1
print(f"\ndistinct fields: {len(c)}")
for k, v in sorted(c.items())[:60]:
    print(f"   {k:44s} {v}")
