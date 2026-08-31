"""``python -m ccr_m2c`` -- the command line. The GUI is ``python -m ccr_m2c.gui``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
