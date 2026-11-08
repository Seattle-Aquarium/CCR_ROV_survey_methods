# Lutris Lighting System

MicroPython code for the ROV lighting control subsystem. Runs on a Raspberry Pi Pico 2 (RP2350) or other RP2040 / RP2350-based microcontroller boards.

## Overview

Controls four [DeepSea Power & Light LED SeaLite](https://www.deepsea.com/portfolio-items/led-sealite/) lamps via a shared RS485 bus using the DSPL SeaSense protocol.  Light brightness is controlled by a PWM signal from the BlueROV's servo output, allowing the pilot to dim lights with a joystick axis in Cockpit/QGroundControl.

Note that the SeaSense dimming curve is strongly non-linear — `LOUT` 60 buys only
15% of full output, and two thirds of the light lives in the top quarter of the
command range — so the linear PWM mapping in step 3 below is linear in *command
value*, not in light. See [`../simulation/`](../simulation/) for the measured
curve and what it means for exposure.

## How It Works

1. The BlueROV sends a standard servo PWM signal (1100-1900 us at ~50 Hz) to GP16 on the Pico
2. The Pico measures pulse width using pin edge interrupts
3. Pulse width is linearly mapped to brightness: 1100 us = 0% (off), 1500 us = 50%, 1900 us = 100%
4. Brightness commands are sent to all 4 lights over UART using the SeaSense protocol
5. If the PWM signal is lost for >1 second, lights turn off automatically

## Pin Assignments

| Pin  | Function                    |
|------|-----------------------------|
| GP0  | UART TX to SeaLite lights   |
| GP1  | UART RX from SeaLite lights |
| GP14 | I2C SDA (INA238 power monitor) |
| GP15 | I2C SCL (INA238 power monitor) |
| GP16 | PWM input from BlueROV servo output |

## BlueROV Setup

1. In BlueOS, configure a servo output as an actuator function (e.g., Servo 9)
2. In Cockpit/QGroundControl, map a joystick axis or button to that actuator
3. See the [Newton Gripper installation guide](https://bluerobotics.com/learn/newton-subsea-gripper-installation/#setup-in-qgroundcontrol) for the general pattern of configuring auxiliary servo outputs

## Output linearization

Out of the box, the mapping between the LOUT command (between 0 and 100) and
output luminosity is non-linear, with 50% of the additional brightness
occuring between LOUT 84 and 100.

By default, we map PWM (1100-1900) linearly to LOUT, so (for example), PWM of 1500ms is LOUT=50 is about 15% luminosity from a light:

```math
\texttt{LOUT} = 100 * \frac{\texttt{PWM} - 1100}{800}
```

*(Rounding `LOUT` to the nearest integer value is ommited for clarity)*

If `LINEARIZE_LIGHTING` is set to `True `main.py`, a logarithmic function will be applied when mapping PWM to LOUT:

```math
\texttt{LOUT} = 100 * \left( 1 + \alpha \ln \left\( \frac{\texttt{PWM} - 1100}{800} \right) \right)
```
From the published DSPL light curves, $`\alpha = 0.246`$

## Development

> [!IMPORTANT] Adding the second telemetry port adds a USB re-enumeration to the
> Circuitpython startup process.  `mpremote` typically does a soft reset
> when it performs new commands, the bus reset interferes with this.
>
> When calling `mpremote` commands, use `mpremote resume` which will attempt the
> command **without** reseting the device first.  You may need to run a command
> multiple times to get it to stick.

Also note, the Pico is now connected to **the Raspberry Pi in the ROV.**  The development steps detailed below should be run **on the Pi.**   As of 11/26, the `mpremote` package and a copy of this repo have been installed on the Pi.

As a side note, as Lutris currently runs a 32-bit kernel, it can't be accessed using the standard VSCode remote extension.   I (Aaron) was able to develop remotely on the Pi using the [SSHFS](https://marketplace.visualstudio.com/items?itemName=Kelvin.vscode-sshfs) extension.

Mount the local directory and test interactively:

```
cd lighting/code/src
mpremote resume mount . repl
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

Once to install the CDC library to the device:

```
mpremote mip install usb-device-cdc
```

then

```
cd lighting/code/src
mpremote resume cp -r . :
mpremote reset
```

Once deployed, the system starts automatically on boot and requires no manual intervention.

## File Structure

```
src/
  main.py          - LightingController and PWMReader (main application)
  boot.py          - Boot configuration
  pwm_test.py      - Standalone PWM input test utility
  lib/
    uart_wrapper.py              - pyserial-compatible UART wrapper for MicroPython
    ina238.py                    - INA238 current/voltage sensor driver
    pydspl_seasense/             - DSPL SeaSense protocol library
      sealite.py                 - SeaLite light control
      seasense.py                - Base protocol implementation
      peripheral_base.py         - Peripheral abstraction
```
