"""
The four DeepSea lights on Lutris, as the BlueOS Light Monitor extension sees
them.

This program never talks to the light controller. The controller hangs off
the Pi's USB, the extension on the Pi is the one thing that reads it, and
this package reads the extension's API (`/api/v1/state`, `/api/v1/records`)
over the tether -- so the extension's own page, Cockpit (through MAVLink) and
this program can all watch at once without competing for anything.

See ``blueos_extensions/light_monitor`` for the extension and
``specifications/lights_data_sources.md`` for what each number is.
"""

from .client import DEFAULT_PORT, LightsClient, lights_folder

__all__ = ["DEFAULT_PORT", "LightsClient", "lights_folder"]
