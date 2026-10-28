"""
What the web demo's display shows until the program's window covers it.

From a cold start the program takes about half a minute to open its window,
and until it does a visitor would be looking at an empty black display and
could take the demo for broken. start.sh opens this first, so the program's
window opens over it and covers it; it stays underneath, and shows again only
while the program is being started again.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rov_flight_ops import brand  # noqa: E402

TITLE = "ROV Flight Operations is starting"
DETAIL = ("The simulator takes about half a minute to open.\n"
          "This page shows it as soon as it does.")


def main() -> None:
    brand.register_bundled_fonts()
    root = tk.Tk()
    root.title(TITLE)
    # The whole display: there is no window manager to place it.
    root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
    root.configure(bg=brand.FATHOM)
    tk.Label(root, text=TITLE, font=(brand.FONT_FAMILY, 26), fg=brand.WHITE,
             bg=brand.FATHOM).place(relx=0.5, rely=0.45, anchor="center")
    tk.Label(root, text=DETAIL, font=(brand.FONT_FAMILY, 14), fg=brand.PUMICE,
             bg=brand.FATHOM, justify="center").place(relx=0.5, rely=0.53,
                                                      anchor="center")
    root.mainloop()


if __name__ == "__main__":
    main()
