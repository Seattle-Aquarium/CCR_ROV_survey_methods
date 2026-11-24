"""
Where the window is: the usable part of its monitor, in logical pixels.

Tk cannot be asked this reliably. CustomTkinter makes the process aware of the
display's scaling only *after* Tk has started, so Tk goes on reporting the size
the screen had before -- 1280 x 720 for the field laptop's 1920 x 1080 panel --
and it knows nothing of the taskbar. Windows is asked directly instead: the
work area (the screen less the taskbar) of the monitor the window is on, in
physical pixels, divided by CustomTkinter's scaling for that window.
"""

from __future__ import annotations

import sys


def _scaling(window) -> float:
    try:
        import customtkinter as ctk
        return float(ctk.ScalingTracker.get_window_scaling(window)) or 1.0
    except Exception:
        return 1.0


def work_area(window) -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of the work area of the monitor `window` is on,
    in CustomTkinter's logical pixels -- the units `geometry` takes."""
    scale = _scaling(window)
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
            monitor = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # nearest
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcWork
                return (int(r.left / scale), int(r.top / scale),
                        int((r.right - r.left) / scale),
                        int((r.bottom - r.top) / scale))
        except Exception:
            pass
    try:
        return (0, 0, int(window.winfo_screenwidth()),
                int(window.winfo_screenheight()))
    except Exception:
        return None
