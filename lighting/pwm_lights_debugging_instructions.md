# Lighting Flicker Debugging Guide (Windows)

Instructions for remotely diagnosing lighting flicker on the Lutris ROV.
See [issue #34](https://github.com/Seattle-Aquarium/CCR_development/issues/34)
for context.

## What we're trying to learn

At high brightness the lights sometimes flicker (occasionally brighter than
commanded). Two suspects:

1. **Bad PWM input** — the BlueROV -> Pico PWM signal is noisy or intermittent
   (loose cable, EMI). The Pico would then send spurious brightness commands
   to the lights.
2. **Power fluctuations** — the 24 V rail out of the voltage converter sags or
   spikes under load.

This guide covers **suspect #1** by watching the Pico's serial console for
PWM value jitter. If the PWM reading is rock steady during a flicker event,
move on to scoping the 24 V rail.

## What you need

- Windows laptop
- USB cable (micro-USB for the Pico inside the lighting box)
- Physical access to open the lighting box and plug into the Pico's USB port
- ~20 min

## 1. Install prerequisites

Open **PowerShell** and run:

```powershell
# Check Python is installed (3.9+). If not, install from https://www.python.org/downloads/
python --version

# Install mpremote (MicroPython remote control tool)
pip install --user mpremote

# Confirm it works
mpremote --help
```

If `mpremote` isn't found after install, close and reopen PowerShell.

## 2. Get the code

```powershell
cd $HOME\Documents
git clone https://github.com/Seattle-Aquarium/CCR_ROV_survey_methods.git
cd CCR_ROV_survey_methods\lighting\code\src
```

(If you already have the repo, just `git pull` and `cd` into the same folder.)

## 3. Increase the status print rate

Open [lighting/code/src/main.py](lighting/code/src/main.py) in any text editor
(Notepad is fine). Find this line near the top of the `LightingController`
class:

```python
    # Status print interval
    STATUS_INTERVAL_MS = 2000
```

Change `2000` to `100`. This makes the Pico print the measured PWM pulse
width 10× per second instead of once every 2 s — fast enough to catch the
jitter that would correspond to a visible flicker.

Save the file.

## 4. Connect to the Pico

1. Power the ROV / lighting box so the Pico boots normally.
2. Open the lighting box and plug the USB cable into the Pico's micro-USB
   port, then into your laptop.
3. In PowerShell, confirm the Pico shows up:

```powershell
mpremote devs
```

You should see one COM port listed (e.g. `COM5 ... Board in FS mode` or
similar). If nothing shows up, try a different USB cable — many micro-USB
cables are charge-only.

## 5. Deploy the modified code and watch the serial output

From the `lighting\code\src` folder:

```powershell
# Copy the edited main.py (and everything else) to the Pico
mpremote cp -r . :

# Reset the Pico and attach to its REPL
mpremote reset
mpremote repl
```

`mpremote repl` leaves your terminal attached to the Pico's serial console.
You should see the startup banner followed by a continuous stream of lines
like:

```
PWM: 1523 us | Level: 52%
PWM: 1523 us | Level: 52%
PWM: 1524 us | Level: 52%
...
```

Press **Ctrl-]** to exit the REPL when you're done.

## 6. Reproduce the flicker and capture the log

1. Leave the REPL attached.
2. Drive the lights up to the brightness where flicker occurs (from
   Cockpit/QGC, as usual).
3. Let it run for ~30 seconds while watching the output. You can copy the
   PowerShell buffer to a text file for later review (right-click the title
   bar -> Edit -> Select All -> Copy).

### What to look for

- **Steady `PWM:` value (± a few us), lights flicker** → PWM input is fine.
  Flicker is almost certainly a **power issue**. Next step: oscilloscope on
  the 24 V rail out of the voltage converter while flickering.
- **`PWM:` value jumps around (e.g. 1520 -> 1480 -> 1550 -> 1510)** or
  occasionally reads `NO SIGNAL` → **PWM input is noisy**. Check the GP16
  signal wire from the BlueROV servo output for loose terminals, poor
  ground, or routing near the 24 V / light cables.
- **`Level:` value changes even when PWM looks steady** → software bug,
  worth filing a separate issue.

## 7. Revert the change when finished

Don't leave `STATUS_INTERVAL_MS = 100` on the deployed Pico — it's noisy
and unnecessary in normal operation. Change it back to `2000`, redeploy,
and reset:

```powershell
mpremote cp main.py :
mpremote reset
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mpremote: command not found` | Reopen PowerShell after `pip install`. If still missing, `python -m mpremote ...` works too. |
| `mpremote devs` shows nothing | Try another USB cable (many are charge-only). Check Windows Device Manager for an unknown device. |
| `mpremote repl` shows garbage / no banner | The Pico is probably already running `main.py`. Press Ctrl-C in the REPL to drop to the MicroPython prompt. |
| REPL works but no `PWM:` lines | The control loop isn't running. At the `>>>` prompt: `from main import main; main()`. |
| `Access is denied` on the COM port | Close any other serial terminal (Thonny, PuTTY) holding the port. |
