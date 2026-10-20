# Lutris Lighting System

MicroPython code for the ROV lighting control subsystem. Runs on a Raspberry Pi Pico 2 (RP2350).

## Overview

Controls 4 [DeepSea Power & Light LED SeaLite](https://www.deepsea.com/portfolio-items/led-sealite/) lamps via a shared UART bus using the DSPL SeaSense protocol. Light brightness is controlled by a PWM signal from the BlueROV's servo output, allowing the pilot to dim lights with a joystick axis in Cockpit/QGroundControl.

Note that the SeaSense dimming curve is strongly non-linear — `LOUT` 60 buys only
15% of full output, and two thirds of the light lives in the top quarter of the
command range — so the Pico does not map PWM straight onto `LOUT`. It steps
through a table of `LOUT` values chosen so that each step adds 10% of full light
output (see [Brightness Steps](#brightness-steps)). See
[`../simulation/`](../simulation/) for the measured curve and what it means for
exposure.

## How It Works

1. The BlueROV sends a standard servo PWM signal (1100-1900 us at ~50 Hz) to GP16 on the Pico
2. The Pico measures pulse width using pin edge interrupts
3. Pulse width is snapped to the nearest of 11 steps, 80 us apart: 1100 us = off, and each step up adds 10% of full light output
4. The step's `LOUT` is sent to all 4 lights over UART using the SeaSense protocol
5. If the PWM signal is lost for >1 second, lights turn off automatically

## Brightness Steps

The table is `LOUT_STEPS` in `main.py`. Each `LOUT` is the whole number nearest
its 10% on the manual's dimming curve
([`lsl_seasense_dimming.csv`](../simulation/data/lsl_seasense_dimming.csv)).
`LOUT` only takes whole numbers, and near the top one `LOUT` is worth 3–4% of
output, so the upper steps land a little off the mark.

| Step | PWM (us) | LOUT | Light output |
|-----:|---------:|-----:|-------------:|
| 0  | 1100 | 0   | off  |
| 1  | 1180 | 39  | 10%  |
| 2  | 1260 | 67  | 20%  |
| 3  | 1340 | 74  | 30%  |
| 4  | 1420 | 80  | 41%  |
| 5  | 1500 | 84  | 50%  |
| 6  | 1580 | 88  | 60%  |
| 7  | 1660 | 92  | 71%  |
| 8  | 1740 | 95  | 81%  |
| 9  | 1820 | 97  | 88%  |
| 10 | 1900 | 100 | 100% |

Light output is a share of full brightness, from the manual. The electrical
power drawn should roughly follow it but has not been measured step by step;
`c.read_power()` (the INA238) can do that.

The PWM range is split evenly into one step fewer than there are entries in
`LOUT_STEPS`, so ArduSub's `JS_LIGHTS_STEPS` has to be 10 for each press of
lights brighter/dimmer to move exactly one step. Shortening the table (to stop at
80%, say) means changing `JS_LIGHTS_STEPS` to match.

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
4. Set `JS_LIGHTS_STEPS` to 10, so each lights brighter/dimmer press moves one [brightness step](#brightness-steps). The reference parameters in [`nereo.params`](../../rov_flight_ops/vehicle_parameters/nereo.params) have it, and the Parameters tab of ROV Flight Operations flags a vehicle that differs

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
mpremote cp -r . :
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
