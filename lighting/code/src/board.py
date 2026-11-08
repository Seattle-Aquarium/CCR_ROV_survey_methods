"""
We are testing this firmware on two different boards.

* **pico** -- a Raspberry Pi Pico / Pico 2, on the original bench wiring and on
  Keenan Johnson's light-control PCB (keenanjohnson/deepsea-light-pcba). The
  PCB brings out I2C1 on GP14/GP15 at screw terminal J5 for an external
  INA238 power monitor at 0x40.

* **qtpy** -- the Adafruit QT Py RP2040 that Lutris carries while the PCB is
  built (CCR_ROV_survey_methods#47). RS-485 is on the TX/RX pads (UART1,
  GPIO20/GPIO5 -- MicroPython's defaults for this board), the PWM input on
  A0 (GPIO29), and I2C on the STEMMA QT plug (I2C1, GPIO22/GPIO23 --
  MicroPython's defaults for this board). Lutris has **no** power monitor
  fitted as of 13 October 2026, so the I2C scan finds nothing and the
  firmware carries on without power readings.

The board is recognised from `os.uname().machine`, which MicroPython builds
from the board name ("Adafruit QT Py RP2040 with RP2040", "Raspberry Pi Pico 2
with RP2350"). Anything unrecognised is treated as a Pico, which is what the
firmware assumed before there was more than one board.
"""

import os

PICO = {
    "name": "pico",
    "uart_id": 0,
    "uart_tx": 0,  # GP0
    "uart_rx": 1,  # GP1
    "pwm_pin": 16,  # GP16
    "i2c_id": 1,
    "i2c_sda": 14,  # GP14, J5 on the PCB
    "i2c_scl": 15,  # GP15, J5 on the PCB
    "local_echo": False,
}

QTPY = {
    "name": "qtpy",
    "uart_id": 1,
    "uart_tx": None,  # None = MicroPython's default for the board (GPIO20)
    "uart_rx": None,  # (GPIO5)
    "pwm_pin": 29,  # A0
    "i2c_id": 1,
    "i2c_sda": 22,  # STEMMA QT
    "i2c_scl": 23,
    "local_echo": True,  # The QtPy can't run the RTS signal on the transceiver
}


def machine_name():
    try:
        return os.uname().machine
    except Exception:
        return ""


def detect(name=None):
    """The pin map for this board."""
    name = machine_name() if name is None else name
    if "QT Py" in name or "QTPY" in name.upper().replace(" ", ""):
        return QTPY
    return PICO
