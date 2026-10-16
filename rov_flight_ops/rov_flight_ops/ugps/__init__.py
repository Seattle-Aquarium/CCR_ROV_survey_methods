"""
The Water Linked Underwater GPS G2: its acoustic solution, receiver by receiver.

`capture` reads the G2 topside directly from the laptop and logs it into the
flight folder; `health` compares the four receivers for the Navigation tab;
`simulator` stands in for a G2 in tests and on the bench.
"""

from .capture import UgpsCapture, ugps_folder
from .g2 import DEFAULT_HOST

__all__ = ["DEFAULT_HOST", "UgpsCapture", "ugps_folder"]
