# Lighting System Bringup Procedure

Manual test procedure for the Lutris Lighting System.

## Prerequisites

- Raspberry Pi Pico 2 with MicroPython installed
- USB cable for REPL access
- Serial terminal (Thonny, mpremote, or screen)
- At least one SeaLite LED light
- 48V power supply for lights (if testing at full power)

## mpremote Workflow

### Development (mounted filesystem)

Best for iterating quickly - files stay on your computer:

```bash
cd lighting/code/src
mpremote mount . repl
```

Then in the REPL:
```python
from main import main
c = main()
```

### Deployment (copy to flash)

For standalone operation without USB:

```bash
cd lighting/code/src
mpremote cp -r . :
```

### Useful commands

```bash
mpremote ls              # List files on device
mpremote rm main.py      # Remove a file
mpremote reset           # Reset the board
mpremote repl            # Connect to REPL (no mount)
```

## Pin Assignments

| Light | UART | TX Pin | RX Pin |
|-------|------|--------|--------|
| 0     | HW UART0 | GP0 | GP1 |
| 1     | HW UART1 | GP4 | GP5 |
| 2     | PIO UART | GP8 | GP9 |
| 3     | PIO UART | GP10 | GP11 |

| I2C Device | SDA | SCL |
|------------|-----|-----|
| INA238     | GP14 | GP15 |

## Procedure

### 1. Start the Controller

Connect via mpremote (see workflow above), then start the controller:

```python
from main import main
c = main()
```

You should see output like:
```
Lutris Lighting System Starting...
----------------------------------------
UART0 initialized: TX=GP0, RX=GP1
UART1 initialized: TX=GP4, RX=GP5
PIO UART2 initialized: TX=GP8, RX=GP9
PIO UART3 initialized: TX=GP10, RX=GP11
Initialized 4 SeaLite lights
I2C devices found: ['0x40']
INA238 power monitor initialized
----------------------------------------
System ready. Commands:
  c.set_light_level(0, 50)  # Light 0 to 50%
  c.get_light_level(0)      # Read level
  c.get_light_temperature(0)
  c.set_all_lights(50)
  c.all_off()
  c.read_power()
  c.status()
```

If INA238 is not connected, you'll see a warning instead - this is OK.

### 2. Test Individual Lights

Test each light one at a time. Replace `N` with the light index (0-3).

#### 2.1 Basic Communication Test

```python
# Set light N to 10% - low power for initial test
c.set_light_level(N, 10)
```

- Returns `True`: Command sent successfully
- Returns `False`: Communication error

#### 2.2 Verify Light Responds

```python
# Read back the level
c.get_light_level(N)
```

- Returns `10`: Light is communicating properly
- Returns `None`: No response from light

#### 2.3 Temperature Check

```python
# Read temperature (sanity check)
c.get_light_temperature(N)
```

- Returns a number (typically 20-40C at idle): Light is healthy
- Returns `None`: Communication issue

#### 2.4 Brightness Ramp Test

```python
# Ramp up brightness
for level in [0, 25, 50, 75, 100]:
    c.set_light_level(N, level)
    print(f"Level: {level}")
    time.sleep(2)

# Turn off
c.set_light_level(N, 0)
```

### 3. Test All Lights Together

Once individual lights pass:

```python
# All lights to 50%
c.set_all_lights(50)

# Check status
c.status()

# All lights off
c.all_off()
```

### 4. Power Monitoring (if INA238 connected)

```python
# Read voltage, current, power
c.read_power()
```

Returns dict with `voltage`, `current`, `power` keys, or `None` if not available.

## Troubleshooting

### Light doesn't respond

1. Check TX/RX wiring (may be swapped)
2. Verify 48V power to light
3. Check light address is set to 1 (factory default)
4. Try lower baud rate if using long cables

### set_light_level returns False

1. Check UART TX pin connection
2. Verify ground is connected between Pico and light

### get_light_level returns None

1. Check UART RX pin connection
2. Light may not be powered
3. Try increasing timeout in code

### I2C devices found: []

1. Check SDA/SCL wiring to INA238
2. Verify INA238 address jumpers (default 0x40)
3. Check pull-up resistors on I2C lines

### PIO UART issues (lights 2-3)

PIO UARTs are software-based and more timing sensitive:
1. Keep wires short
2. Ensure good ground connection
3. Avoid running other intensive code while communicating

## Quick Reference

```python
# Single light control
c.set_light_level(0, 50)    # Light 0 to 50%
c.get_light_level(0)        # Read light 0 level
c.get_light_temperature(0)  # Read light 0 temp

# All lights
c.set_all_lights(50)        # All to 50%
c.all_off()                 # All off
c.status()                  # Full status

# Power
c.read_power()              # Voltage/current/power
```
