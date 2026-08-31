"""
Entry point for the packaged application.

PyInstaller runs its target script as ``__main__``, so pointing it straight at
``ccr_m2c/gui.py`` would break that module's relative imports ("attempted relative
import with no known parent package"). Importing the package from a top-level
script keeps the normal package context intact.

A ``--cli`` first argument (or any argument at all, when the build is a console
one) hands off to the command line instead, so a single .exe serves both.
"""

import sys

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--cli":
        from ccr_m2c.cli import main
        sys.exit(main(argv[1:]))
    if argv:
        from ccr_m2c.cli import main
        sys.exit(main(argv))
    from ccr_m2c.gui import main
    main()
