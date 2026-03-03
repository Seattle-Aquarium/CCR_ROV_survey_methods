# Lutris Lighting System

MicroPython code for the ROV lighting control subsystem. Runs on a Raspberry Pi Pico 2 (RP2350).

## Overview

Controls 4 SeaLite LED lights (Kraken Solar Flare Mini 18,000) via a shared UART bus using the DSPL SeaSense protocol. Light brightness is controlled by a PWM signal from the BlueROV's servo output, allowing the pilot to dim lights with a joystick axis in Cockpit/QGroundControl.

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
