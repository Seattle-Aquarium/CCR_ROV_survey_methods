"""
Entry point for the packaged application.

PyInstaller runs its target script as ``__main__``, so pointing it straight at
``utc/gui/app.py`` breaks that module's relative imports ("attempted
relative import with no known parent package"). Importing the package from a
top-level script keeps the normal package context intact.
"""

from utc.gui.app import main

if __name__ == "__main__":
    main()
