"""Exercise discovery against the real (varied) flight folders on this machine."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utc.discovery import discover

F = Path(r"C:\Users\randellz\Seattle Aquarium Dropbox\Coastal_Climate_Resilience\flights")
CASES = [
    F / "testing/2026_08_21_DeepSea_light_testing",
    F / "testing/2026_06_12_C3-cam-testing",
    F / "testing/2026_05_12_Lutris_lights",
    F / "HSIL/2025/2025_09_27_Shaw_Island",
    F / "Olympic_Coast/2026",
]
for c in CASES:
    print("=" * 78)
    d = discover(c)
    print(d.summary())
    print(f"  -> ok={d.ok}")
