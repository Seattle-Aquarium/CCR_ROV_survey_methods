"""
Entry point for a packaged build of ROV Imagery Processing.

PyInstaller runs its target script as ``__main__``, so pointing it straight at
the package's GUI module breaks relative imports; importing the package from a
top-level script keeps the package context intact.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    # Overlay rendering runs across several processes, and on Windows a new
    # process is started by re-launching this executable. It must come first.
    multiprocessing.freeze_support()

    # `--selftest` checks that a build is healthy -- bundled ffmpeg, fonts,
    # timezone data, and that rendering really does run across processes.
    if "--selftest" in sys.argv:
        from rov_imagery_processing.selftest import run
        sys.exit(run(sys.argv))

    from rov_imagery_processing.gui.app import main
    main()
