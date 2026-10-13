# Lutris Lighting System

MicroPython code for the ROV lighting control subsystem. Runs on a Raspberry Pi Pico 2
(RP2350) — Keenan Johnson's light-control PCB — or the Adafruit QT Py RP2040 that Lutris
carries until the PCB is fitted (#47). The board is recognised at start-up (`board.py`).

It also reports what the lights say about themselves — level, power-on count,
temperatures, faults — as JSON lines over USB to the Pi, where the BlueOS Light Monitor
extension records them and puts a summary into MAVLink (so into the tlog on BlueOS 1.4
and the mcap on 1.5). **See [TELEMETRY.md](TELEMETRY.md)** for the format, how it gets
off the vehicle, the second USB port, and the bench checklist.

## Overview

Controls 4 [DeepSea Power & Light LED SeaLite](https://www.deepsea.com/portfolio-items/led-sealite/) lamps via a shared UART bus using the DSPL SeaSense protocol. Light brightness is controlled by a PWM signal from the BlueROV's servo output, allowing the pilot to dim lights with a joystick axis in Cockpit/QGroundControl.

Note that the SeaSense dimming curve is strongly non-linear — `LOUT` 60 buys only
15% of full output, and two thirds of the light lives in the top quarter of the
command range — so the linear PWM mapping in step 3 below is linear in *command
value*, not in light. See [`../simulation/`](../simulation/) for the measured
curve and what it means for exposure.

## How It Works

1. The BlueROV sends a standard servo PWM signal (1100-1900 us at ~50 Hz) to GP16 on the Pico
2. The Pico measures pulse width using pin edge interrupts
3. Pulse width is linearly mapped to brightness: 1100 us = 0% (off), 1500 us = 50%, 1900 us = 100%
4. Brightness commands are sent to all 4 lights over UART using the SeaSense protocol,
   50 ms apart so each light's ACK clears the bus (#43)
5. If the PWM signal is lost for >1 second, lights turn off automatically
6. On loop turns where the level did not change, the controller asks one light one
   question (`LOUT?`, `STAT?`, …) and sends the answer on as telemetry (`poller.py`)

## Pin Assignments

| Function | Pico / Pico 2 (PCB) | QT Py RP2040 (Lutris now) |
|---|---|---|
| UART TX to the RS-485 transceiver | GP0 (UART0) | TX pad, GPIO20 (UART1) |
| UART RX from it | GP1 | RX pad, GPIO5 |
| PWM input from the Navigator (servo 15) | GP16 | A0, GPIO29 |
| I2C SDA, power monitor | GP14 (J5 on the PCB) | STEMMA QT, GPIO22 |
| I2C SCL, power monitor | GP15 | STEMMA QT, GPIO23 |

Lutris has **no power monitor fitted** (October 2026). `lib/ina2xx.py` drives an INA226
or INA238 if one appears, telling them apart by their ID registers.

## BlueROV Setup

1. In BlueOS, configure a servo output as an actuator function (e.g., Servo 9)
2. In Cockpit/QGroundControl, map a joystick axis or button to that actuator
3. See the [Newton Gripper installation guide](https://bluerobotics.com/learn/newton-subsea-gripper-installation/#setup-in-qgroundcontrol) for the general pattern of configuring auxiliary servo outputs

## Development

Mount the local directory and test interactively:

```
cd lighting/code/src
mpremote mount . repl
```

Then in the REPL:

```python
from main import main
c = main()
# The PWM control loop starts automatically.
# Press Ctrl-C to stop the loop and use manual commands:
c.set_all_lights(50)
c.all_off()
c.read_power()
c.status()
```

## Deploy to Device

```
cd lighting/code/src
mpremote mip install usb-device-cdc     # once: the second USB port for telemetry
mpremote cp -r . :
mpremote reset
```

Once deployed, the system starts automatically on boot and requires no manual intervention.

**On the vehicle, mpremote stops the lights.** Its commands that touch files or run code
interrupt `main.py`, which turns all four lights off until a reset. Do it on deck, never
mid-dive — and press *Release port* on the Light Monitor's page first if it is reading
the console. Telemetry without `usb-device-cdc` (or on MicroPython < 1.23) falls back to
the console, prefixed `@L `.

## Tests

The telemetry logic runs under CPython against scripted hardware:

```
python -m pytest lighting/code/tests
```

## File Structure

```
src/
  main.py          - LightingController and PWMReader (main application)
  boot.py          - Boot configuration; brings up the telemetry USB port
  board.py         - which board this is, and its pins
  datalink.py      - the second USB serial port, with its safety valve
  telemetry.py     - ccr.lights/1 lines, numbered, never blocking
  lightbus.py      - one question to one light, with a hard deadline
  poller.py        - what to ask each light, and when
  pwm_test.py      - Standalone PWM input test utility
  lib/
    uart_wrapper.py              - pyserial-compatible UART wrapper for MicroPython
    ina2xx.py                    - INA226 / INA238 power monitors, identified by ID
    ina238.py                    - the original INA238 driver (no longer used by main.py)
    pydspl_seasense/             - DSPL SeaSense protocol library
      sealite.py                 - SeaLite light control
      seasense.py                - Base protocol implementation
      peripheral_base.py         - Peripheral abstraction
```
