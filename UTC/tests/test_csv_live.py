"""Load the smoke-test extraction, sample it, and write a 1 Hz CSV."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utc.telemetry import TelemetryStore
from utc.csv_export import export_1hz

CACHE = Path(r"C:\Users\randellz\AppData\Local\ccr_composite_cache\_smoketest")
t0 = time.time()
st = TelemetryStore.load(CACHE / "telemetry.csv")
print(f"loaded {len(st.series)} fields in {time.time()-t0:.1f}s")
print(f"span {st.t_start:.1f} .. {st.t_end:.1f} ({st.t_end-st.t_start:.1f}s)")

mid = (st.t_start + st.t_end) / 2
s = st.sample(mid)
print("\nsample at midpoint:")
for k, v in s.items():
    print(f"   {k:12s} {v}")

out = CACHE / "test_1hz.csv"
r = export_1hz(st, out, utc_offset_hours=-7.0,
               progress=lambda f, m: None)
print(f"\nwrote {r.rows} rows, {len(r.columns)} columns -> {out.name}")
import csv as _csv
with open(out) as f:
    rows = list(_csv.reader(f))
print("header:", ", ".join(rows[0][:14]), "...")
print(f"\nfirst data row ({len(rows[1])} cells):")
for k, v in zip(rows[0], rows[1]):
    if v != "":
        print(f"   {k:26s} {v}")
