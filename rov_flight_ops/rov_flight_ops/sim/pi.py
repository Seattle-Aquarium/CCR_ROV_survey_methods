"""
What is on the simulated vehicle's Pi, for the BlueOS logs tab.

Shaped like a real Pi at the end of a survey morning: the recordings for the
flight in progress (the newest still growing) and yesterday's, the autopilot's
dataflash logs, a few tlogs, C3 imagery from the three cameras, and the UGPS
External extension's own logs. File names, folders and sizes follow what the
Port of Seattle downloads in September 2026 actually found -- recorder files
from 18 MB to half a gigabyte, dataflash logs of tens of MB.

Nothing here is on any vehicle, and nothing the tab does in simulation reaches
one: downloads are reported, not made, and deleting is refused while the
simulated ROV is armed -- which, mid-flight, it is, exactly as on the real
vehicle.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

from .. import blueos
from .. import pifiles as PF
from .world import SimWorld

C3_ROOT = "/system_root/usr/blueos/userdata/madrona/session_001"


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d_%H%M%S")


def inventory(world: SimWorld, kinds) -> PF.Inventory:
    """A listing of the simulated Pi, for the types asked for."""
    rng = random.Random(59)
    now = time.time()
    dive0 = world.wall(0.0)
    yesterday = dive0 - 86_400 + 1_800
    inv = PF.Inventory(host="simulated ROV", token="simulated", vehicle="Nereo",
                       listed_at=now, skew=0.0)
    want = set(kinds)

    def add(kind: str, root: str, rel: str, size: int, start: float, end: float,
            growing: bool = False) -> None:
        inv.files.setdefault(kind, []).append(PF.PiFile(
            category=kind, path=f"{root}/{rel}", rel=rel, size=int(size),
            modified=now - 5 if growing else end, start=start,
            end=end if not growing else now))

    if "mcap" in want or "video" in want:
        root = blueos.RECORDER_FB_PATH
        if "mcap" in want:
            inv.roots["mcap"] = [root]
            t = yesterday
            for size in (481e6, 18e6, 54e6, 312e6):
                dur = max(60.0, size / 0.55e6)
                add("mcap", root, f"recorder_{_stamp(t)}.mcap", size, t, t + dur)
                t += dur + rng.uniform(40, 200)
            t = dive0 + 30
            for size in (226e6, 41e6):
                dur = size / 0.55e6
                add("mcap", root, f"recorder_{_stamp(t)}.mcap", size, t, t + dur)
                t += dur + 25
            add("mcap", root, f"recorder_{_stamp(t)}.mcap",
                (now - t) * 0.55e6, t, now, growing=True)
            inv.notes["mcap"] = []
        if "video" in want:
            inv.roots["video"] = [root]
            for day0 in (yesterday, dive0 + 30):
                folder = f"video_{_stamp(day0)}"
                for k in range(3):
                    s = day0 + k * 420
                    add("video", root, f"{folder}/stream_{k + 1:02d}.mp4",
                        rng.uniform(380e6, 720e6), s, s + 410)
            inv.notes["video"] = []
    if "bin" in want:
        root = blueos.DATAFLASH_FB_PATH
        inv.roots["bin"] = [root]
        add("bin", root, "00000171.BIN", 61.8e6, yesterday - 600, yesterday + 3600)
        add("bin", root, "00000172.BIN", 44.1e6, yesterday + 3700, yesterday + 6200)
        add("bin", root, "00000173.BIN", (now - dive0) * 22e3 + 2e6, dive0 - 120, now,
            growing=True)
        inv.notes["bin"] = []
    if "tlog" in want:
        root = PF.TLOG_FB_PATHS[0]
        inv.roots["tlog"] = [root]
        add("tlog", root, f"{datetime.fromtimestamp(yesterday):%Y-%m-%d %H-%M-%S}.tlog",
            9.4e6, yesterday, yesterday + 3500)
        add("tlog", root, f"{datetime.fromtimestamp(dive0):%Y-%m-%d %H-%M-%S}.tlog",
            (now - dive0) * 2.6e3, dive0, now, growing=True)
        inv.notes["tlog"] = []
    if "ugps" in want:
        root = PF.UGPS_EXT_FB_PATH
        inv.roots["ugps"] = [root]
        for day0, grow in ((yesterday, False), (dive0, True)):
            day = datetime.fromtimestamp(day0, timezone.utc)
            folder = f"{day:%Y-%m-%d}"
            stamp = f"{day:%Y-%m-%d_%H-%M-%S}"
            for what, rate in (("g2_acoustic", 520.0), ("g2_position", 140.0),
                               ("g2_orientation", 60.0), ("gga", 90.0),
                               ("hdt", 40.0), ("pashr", 70.0)):
                end = now if grow else day0 + 3500
                add("ugps", root, f"{folder}/{stamp}_{what}.csv",
                    (end - day0) * rate, day0, end, growing=grow)
        inv.notes["ugps"] = []
    if "c3" in want:
        inv.roots["c3"] = [f"{C3_ROOT}/{eye}" for eye in ("left", "right", "center")]
        for eye in ("left", "right", "center"):
            root = f"{C3_ROOT}/{eye}"
            t = dive0 + 150
            for k in range(220):
                inv.files.setdefault("c3", []).append(PF.PiFile(
                    category="c3", path=f"{root}/IMG_{k + 1:05d}.jpg",
                    rel=f"{eye}/IMG_{k + 1:05d}.jpg", size=int(rng.uniform(2.1e6, 3.9e6)),
                    modified=t, start=t, end=t))
                t += 2.0
        inv.notes["c3"] = []
    for kind in list(inv.files):
        inv.files[kind].sort(key=lambda f: f.rel)
    return inv


#: What deleting says in simulation: what the real vehicle would say mid-flight.
REFUSED_ARMED = ("Nothing was deleted: the simulated ROV is armed. Files are only "
                 "deleted when a current heartbeat confirms the ROV is disarmed — "
                 "on a real survey, at the start of the next day.")


def download_summary(files, flight_dir) -> str:
    total = sum(f.size for f in files)
    return (f"Simulated download: {len(files)} file(s), {total / 2 ** 30:.2f} GiB "
            f"would have been copied from the vehicle into {flight_dir}. "
            f"Nothing was copied — this is the ROV flight simulator.")
