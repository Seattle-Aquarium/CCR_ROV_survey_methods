"""
Entry point for the packaged application.

PyInstaller runs its target script as ``__main__``, so pointing it straight at
``utc/gui/app.py`` breaks that module's relative imports ("attempted
relative import with no known parent package"). Importing the package from a
top-level script keeps the normal package context intact.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    # Overlay rendering runs across several processes, and on Windows a new
    # process is started by re-launching this executable. Without this call
    # each worker would reach `main()` and open another copy of the GUI, which
    # would then start workers of its own. It must come before anything else.
    multiprocessing.freeze_support()

    # `--selftest` checks that this build is healthy -- bundled ffmpeg, fonts
    # and timezone data, and that rendering really does run across processes.
    # Worth having because the ways a packaged build differs from a working
    # source tree are exactly the ways it fails silently.
    if "--selftest" in sys.argv:
        from utc.selftest import run
        sys.exit(run(sys.argv))

    # `--probe-rov` asks the ROV what it offers, read-only. Run it once beside
    # the vehicle and send the report back; it is how the download feature
    # gets built against what BlueOS actually serves rather than a guess.
    if "--probe-rov" in sys.argv:
        from utc.blueos import run as probe_run
        sys.exit(probe_run(sys.argv))

    from utc.gui.app import main
    main()
