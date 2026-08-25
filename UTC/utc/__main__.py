"""``python -m utc`` opens the GUI; ``python -m utc.cli`` is headless."""
from .gui.app import main

if __name__ == "__main__":
    main()
