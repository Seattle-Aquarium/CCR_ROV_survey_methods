# boot.py
# Runs once at every hard and soft reset, before main.py.
#
# Brings up the second USB serial port for telemetry (see datalink.py). It has
# to happen here rather than in main.py: on a hard reset boot.py runs before
# USB is started, so the controller enumerates once, with both ports, instead
# of appearing and then re-appearing with a second one.

import machine  # noqa: F401  (kept from the original boot.py)

try:
    import datalink
    datalink.start()
except Exception:
    # Telemetry falls back to the console; nothing here may stop the lights.
    pass
