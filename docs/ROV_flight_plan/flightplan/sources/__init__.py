"""Data-source clients: NOAA CO-OPS, NWS api.weather.gov, Open-Meteo, NDBC.

Nothing here imports matplotlib or the GUI. Each ``get_*`` function returns a
plain dataclass (or None on failure) and records where its numbers came from.
"""
