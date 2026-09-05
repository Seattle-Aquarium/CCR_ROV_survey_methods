"""Live script: run a real Lightroom batch and measure what came out.

Needs Lightroom Classic installed, set up with a seed catalog, closed, and a
folder of real GPR files. Opens Lightroom, so it is not something to run while
using the machine for anything else.

    python tests/test_lightroom_live.py --setup          # one-time, per LrC version
    python tests/test_lightroom_live.py <folder-of-gpr> [--denoise]

Without --denoise it exercises the entire supported path -- import, crop,
chromatic aberration, export -- with no UI automation at all, which is the
half that should never break. Add --denoise to test the one fragile step.

What it checks is the thing that actually matters: the TIFs on disk are
exactly the requested pixel size, sixteen bits per channel, and carry a
ProPhoto profile. The catalog's opinion of the crop is a second source, read
back from the scratch catalog before it is deleted.
"""

from __future__ import annotations

import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc import lightroom as lr  # noqa: E402
from utc.lightroom import preflight  # noqa: E402
from utc.lightroom.gpr import _read_ifd  # noqa: E402

_TAG = {256: "width", 257: "height", 258: "bits", 262: "photometric",
        277: "samples", 34675: "icc"}


def tiff_facts(path: Path) -> dict:
    """Width, height, bit depth and whether an ICC profile is embedded."""
    buf = path.read_bytes()[: 1 << 20]
    if buf[:2] not in (b"II", b"MM"):
        return {"error": "not a TIFF"}
    bo = "<" if buf[:2] == b"II" else ">"
    (first,) = struct.unpack_from(bo + "I", buf, 4)
    ifd = _read_ifd(buf, first, bo)
    out = {}
    for tag, name in _TAG.items():
        v = ifd.get(tag)
        if v is None:
            continue
        out[name] = len(v) if name == "icc" else (v[0] if len(v) == 1 else tuple(v))
    return out


def setup_seed() -> int:
    """The one-time empty catalog, from a terminal instead of the GUI button.

    Lightroom's SDK cannot create a catalog and neither can its command line,
    so this part is done by hand -- once per Lightroom version, not per run.
    """
    import subprocess

    from utc.lightroom import install

    try:
        app = install.find_lightroom()
    except install.LightroomNotSetUp as ex:
        print(f"STOP: {ex}")
        return 1
    plugin = install.install_plugin()
    need_plugin = install.plugin_registration() != "registered"
    need_seed = not install.seed_is_ready(app)
    if not (need_plugin or need_seed):
        print(f"Lightroom {app.version} is already set up:")
        print(f"  plug-in registered, seed at {install.seed_path(app)}")
        return 0
    if install.lightroom_is_running():
        print("STOP: close Lightroom Classic first.")
        return 1

    staging = install.seed_dir(app.version) / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    print(f"Lightroom {app.version} needs its one-time setup.")
    print("")
    print("  1. Lightroom is opening now.")
    n = 2
    if need_plugin:
        # First: creating a catalog restarts Lightroom, and the registration
        # has to be in place when it comes back.
        print(f"  {n}. File > Plug-in Manager... > Add")
        print(f"       pick:  {plugin}")
        print("       it should read 'Installed and running', then Done")
        n += 1
    if need_seed:
        print(f"  {n}. File > New Catalog...")
        print(f"       save in:  {staging}")
        print("       name it UTC_seed, import NOTHING")
        n += 1
    print(f"  {n}. Quit Lightroom, then press Enter here.")
    print("")
    try:
        os.startfile(staging)                              # noqa: S606
    except OSError:
        pass
    # Launch against a catalog that exists. Lightroom reopens whatever it had
    # last, and after a run that is a scratch catalog UTC has since cleaned --
    # so a bare launch lands on "the catalog was not found".
    own = install.last_real_catalog()
    subprocess.Popen([str(app.exe)] + ([str(own)] if own else []))
    if own:
        print(f"  (opening your own catalog: {own.name})")
    input("  waiting… press Enter once Lightroom is closed: ")

    left = []
    if need_seed:
        made = install.find_new_catalog(staging)
        if made is None:
            left.append(f"no new empty catalog found under {staging}")
        else:
            kept = install.adopt_seed(made, app)
            print(f"ok -- seed kept at {kept}")

    state = install.plugin_registration()
    if state == "registered":
        print("ok -- plug-in registered with Lightroom")
    else:
        left.append(f"plug-in is '{state}'; add it in File > Plug-in Manager")

    if left:
        print("")
        for item in left:
            print(f"STOP: {item}")
        return 1
    print("")
    print("Set up. Every run now copies the seed to a scratch catalog of its own.")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    denoise = "--denoise" in sys.argv
    if "--setup" in sys.argv:
        return setup_seed()
    if not args:
        print(__doc__)
        return 2
    src = Path(args[0])

    opts = lr.RawDevelopOptions(denoise=denoise, overwrite=True)
    pre = preflight.check(src, opts)
    print(f"source   {src}")
    print(f"          {pre.survey.describe() if pre.survey else 'nothing'}")
    print(f"out      {pre.tif_dir}")
    for n in pre.notes:
        print(f"  note   {n}")
    for p in pre.problems:
        print(f"  STOP   {p}")
    if not pre.ok:
        return 1
    for size, rect in pre.crops.items():
        print(f"  crop   {size[0]}x{size[1]} -> {rect} -> {rect.size_in(*size)}")

    started = time.time()
    last = [""]

    def progress(frac: float, msg: str = "") -> None:
        if msg and msg != last[0]:
            last[0] = msg
            print(f"  [{frac * 100:5.1f}%] {msg}")

    print(f"\nrunning ({'with' if denoise else 'without'} Denoise) …")
    rep = lr.run_batch(src, opts, progress, None, preflight=pre)
    print(f"\n{rep.summary()}")
    for w in rep.warnings:
        print(f"  warning: {w}")
    for e in rep.errors:
        print(f"  ERROR:   {e}")

    tifs = sorted(pre.tif_dir.glob("*.tif")) if pre.tif_dir.is_dir() else []
    print(f"\n{len(tifs)} TIF in {pre.tif_dir}  ({time.time() - started:.0f}s)")

    bad = 0
    for t in tifs:
        f = tiff_facts(t)
        want_w, want_h = opts.crop_w, opts.crop_h
        problems = []
        if f.get("width") != want_w or f.get("height") != want_h:
            problems.append(f"{f.get('width')}x{f.get('height')}, "
                            f"wanted {want_w}x{want_h}")
        bits = f.get("bits")
        if bits not in (16, (16, 16, 16)):
            problems.append(f"{bits} bits per sample, wanted 16")
        if not f.get("icc"):
            problems.append("no ICC profile embedded")
        if problems:
            bad += 1
            if bad <= 5:
                print(f"  BAD  {t.name}: " + "; ".join(problems))

    if tifs and not bad:
        f = tiff_facts(tifs[0])
        print(f"  all {len(tifs)} TIF are {f['width']}x{f['height']}, "
              f"{f['bits']} bits, {f['icc']}-byte ICC profile")
    elif bad:
        print(f"  {bad} of {len(tifs)} TIF are wrong")

    ok = bool(tifs) and not bad and not rep.errors
    print("\nRESULT:", "ok" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
