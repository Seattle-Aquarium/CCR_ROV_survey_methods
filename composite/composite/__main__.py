"""``python -m composite`` opens the GUI; ``python -m composite.cli`` is headless."""
from .gui.app import main

if __name__ == "__main__":
    main()
