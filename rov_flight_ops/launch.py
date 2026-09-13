"""
Entry point for a packaged build of ROV Flight Operations.

PyInstaller runs its target script as ``__main__``, so pointing it straight at
the package's GUI module breaks relative imports; importing the package from a
top-level script keeps the package context intact.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    # `--probe-rov` asks the ROV what it offers, read-only, and writes a report.
    if "--probe-rov" in sys.argv:
        from rov_flight_ops.blueos import run as probe_run
        sys.exit(probe_run(sys.argv))

    # `--netcheck` reads the topside network: which adapter carries the tether,
    # whether it is a bridge, and whether Windows may power any of it down.
    if "--netcheck" in sys.argv:
        from rov_flight_ops.netdiag import run as netcheck_run
        sys.exit(netcheck_run(sys.argv))

    from rov_flight_ops.gui.app import main
    main()
