"""``python -m rov_imagery_processing`` opens the program."""
import multiprocessing

from .gui.app import main

if __name__ == "__main__":
    # Overlay rendering runs across several processes; on Windows each worker
    # re-imports the main module, and must not open another window.
    multiprocessing.freeze_support()
    main()
