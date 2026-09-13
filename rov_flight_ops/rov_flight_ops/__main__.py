"""``python -m rov_flight_ops`` opens the program."""
import multiprocessing

from .gui.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
