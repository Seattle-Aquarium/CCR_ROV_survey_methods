"""
Entry point for the packaged application.

PyInstaller runs its target script as ``__main__``, so pointing it straight at
``utc/gui/app.py`` breaks that module's relative imports ("attempted
relative import with no known parent package"). Importing the package from a
top-level script keeps the normal package context intact.
"""

import multiprocessing

from utc.gui.app import main

if __name__ == "__main__":
    # Overlay rendering runs across several processes, and on Windows a new
    # process is started by re-launching this executable. Without this call
    # each worker would reach `main()` and open another copy of the GUI, which
    # would then start workers of its own. It must come before anything else.
    multiprocessing.freeze_support()
    main()
