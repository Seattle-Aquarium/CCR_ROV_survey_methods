# Light controller telemetry — `ccr.lights/1`

What the light controller (QT Py RP2040 on Lutris now, a Pico on Keenan
Johnson's PCB later) sends about itself and the four DeepSea lights, and how
it gets off the vehicle. Written 13 October 2026 for
[CCR_development#49](https://github.com/Seattle-Aquarium/CCR_development/issues/49)
/ [#50](https://github.com/Seattle-Aquarium/CCR_development/issues/50).

**Nothing in this document has been run on the vehicle yet.** The firmware's
logic is tested under CPython against scripted hardware
(`lighting/code/tests`) and compiles for MicroPython 1.29 (`mpy-cross`); the
real UART, USB and I2C behaviour is for the [bench checklist](#bench-checklist).

## The path

```
4 lights --RS-485, SeaSense, 9600 baud--> controller (main.py)
   one question per quiet turn of the 50 ms control loop (poller.py)
controller --USB, second serial port "telemetry" (datalink.py)--> Raspberry Pi
   or, without that port, the console with an "@L " prefix
Pi: BlueOS Light Monitor extension (blueos_extensions/light_monitor)
   records every line, works out each light's state, publishes a summary to
   MAVLink (NAMED_VALUE_FLOAT / STATUSTEXT) -> tlog on BlueOS 1.4, mcap on 1.5,
   Cockpit, QGC; serves a page and an API -> rov_flight_ops tab 5 "Lights"
```

The lights only ever **answer**: SeaSense is strict command/response (DeepSea's
spec, p.6). Every number below is a question the controller asked.

## Lines

One JSON object per line, `\n`-terminated. On the telemetry port every line
is one of these; on the console each is prefixed `@L ` so it can be told from
anything else printed there. Every line has:

| key | meaning |
|---|---|
| `k` | kind, below |
| `n` | sequence number from 1 at boot. A gap = lines lost on the way; a drop back = the controller restarted |
| `ms` | milliseconds since the controller started (it has no clock). The reader stamps each line with the Pi's clock on arrival — the clock the tlog and mcap are recorded on |

| `k` | when | fields |
|---|---|---|
| `hello` | at start, then every 60 s | `proto`, `fw`, `board` (`pico`/`qtpy`), `machine`, `mpy` (MicroPython version), `reset` (machine.reset_cause()), `port` (`data`/`console`), `datalink` (why there is no telemetry port, if there is not), `lights`, `power` (monitors found), `power_notes`, `poll`, `resync`, `deadband` |
| `ctl` | every 500 ms and on every change | `pwm` (filtered µs), `age` (ms since last PWM edge), `sig` (1/0), `cmd` (level commanded, %), `tgt` (level the PWM asks for), `rej` (pulses rejected as glitches, total) |
| `set` | each LOUT command sent | `id`, `lvl`, `why` (`pwm`, `resync`, `off`, `manual`) |
| `q` | each question to a light | `id`, `c` (`lout`/`stat`/`stby`/`temp`/`rhum`/`info`), `ok` (1/0), `rtt` (ms), `raw` (the answer, **verbatim**), `err` (`timeout`/`nak`/`partial`/…) |
| `pwr` | every 250 ms, per power monitor | `ch` (`bus` for 0x40), `chip`, `addr`, `v`, `shunt_mv`, and with a shunt value set `a`, `w`; INA238 `t`; `uv` (1 = bus went below its limit since the last read) |
| `log` | a fault in the controller | `lvl`, `msg` |
| `stats` | every 10 s | `sent`, `drop`, `unread`, `asked`, `answered`, `failed`, `loop_max` (ms), `mem`, `passes` |

Answers are passed on raw on purpose: a parser on the Pi can be fixed and
re-run over a recording; one in the controller that threw a value away has
lost it for good.

## What is asked, how often

| question | every | what it is for |
|---|---|---|
| `LOUT?` | pass | the level the light *thinks* it is at — did a command arrive; did a restarted light come back at the right level |
| `STAT?` | 3rd pass | **power-on count** (rises when a light loses power, however briefly), run time, temperatures, humidity, DeepSea's fault flags |
| `STBY?` | 10th | standby: a light can answer and still be dark |
| `TEMP?`, `RHUM?` | 60th | read 0.0 on all four of Lutris's lights (Aaron Marburg, #50) — kept rarely in case that changes |
| `INFO?` | 600th | serial number and firmware; confirms which light is at which address |

Everything is asked on the first pass. One pass of `LOUT?` over four lights is
about a third of a second; with `STAT?` about a second. A light that does not
answer costs the bus deadline (150 ms, 450 ms for `STAT?`), not the UART's
two-second timeout. 25 ms of quiet is left between questions (Aaron saw
corruption at 10 ms, none at ~25 ms).

**`STAT?` on Lutris's lights has eight columns**, not the spec's five: run
time, power-on count, max temperature, three temperatures, humidity, flags
(Aaron, #50). The firmware sends the header row with the values; the
extension reads by header, or by position when the header is missing.

## Steering comes first

A turn of the control loop where the PWM level changed sends the new level
and asks nothing. Otherwise it may ask one question, so a brightness change
waits at most for one answer already in flight (≤ 450 ms, usually ~40 ms).
After a change each light is asked `LOUT?` next, ahead of the rotation.

`RESYNC_LEVELS` (in `main.py`, **off**) would re-send a light's level when it
reports something else three times in a row — Keenan's suggestion from May. It
is off until telemetry shows how often that happens and the team decides it
wants the controller correcting the lights on its own.

## The second USB port

`datalink.py`, called from `boot.py`. Needs **MicroPython ≥ 1.23** and,
installed once on the bench:

```
mpremote mip install usb-device-cdc
```

With it the controller shows on the Pi as one USB device with two serial
ports: `…-if00` the console (mpremote, the REPL), `…-if02` telemetry. Written
without waiting (`timeout=0`): a reader that stalls costs counted lines, never
time in the control loop. Without it, telemetry goes to the console (`@L `).

**Safety valve**, because the box is sealed: `datalink.off` on the controller
turns the port off; `datalink.try` is written before setting the port up and
removed when that returns — if a boot finds it still there, that boot leaves
the port off and renames it `datalink.failed`, which keeps it off until
deleted with mpremote. A soft reset (mpremote does one) removes the second
port and `boot.py` brings it back; `mpremote run` may fail once and work the
second time — MicroPython's documented behaviour.

**Never send Ctrl-C to the console during a dive.** `mpremote` commands that
touch files or run code (`cp`, `ls`, `run`, `mount`, `mip`) interrupt
`main.py`, whose clean-up turns all four lights off until a reset. The Light
Monitor never writes to either port; its page has a *Release port* button that
closes the console for a while so mpremote can use it.

## Power monitor

`lib/ina2xx.py` identifies an **INA226** (BOM) or **INA238** (Keenan's PCB, J5)
by its ID registers and drives only what it recognises; the old `ina238.py`
read an INA226's registers as nonsense and reported success. Pins come from
`board.py`: GP14/GP15 on the Pico/PCB, GPIO22/GPIO23 (STEMMA QT) on the QT Py.
**Lutris has none fitted** (Aaron, #50) — the scan says so and the controller
carries on.

Voltage is right whatever the shunt; current and power need `INA_SHUNT_OHMS`
set to the fitted shunt (left `None`: not guessed). Four lights at full output
are ~390 W — about 16 A at 24 V, 30 A at 13 V — beyond a 10 A breakout's own
shunt. The under-voltage latch is armed at 20 V (monitor on the 24 V side) or
10 V (on the ~13 V side), chosen from the first reading, so a dip shorter than
the 250 ms polling is still caught.

## Bench checklist

To run once with the controller on the bench (mpremote is fine there):

1. `mpremote exec "import sys; print(sys.implementation)"` — MicroPython
   ≥ 1.23? (QT Py build `ADAFRUIT_QTPY_RP2040`, not the Pico 2 `.uf2` in
   `firmware/`.)
2. `mpremote mip install usb-device-cdc`, copy `src/` (`mpremote cp -r . :`),
   reset. `ls -l /dev/serial/by-id/` on the Pi: is there an `-if02` as well as
   `-if00`? Does mpremote still work on `-if00`?
3. Watch `-if02` (`cat /dev/serial/by-id/…-if02` on the Pi): `hello`, `ctl`
   every 500 ms, `q` lines for all four lights?
4. Do `LOUT?` answers end CR LF or bare CR? Does `STAT?` include its header
   row, and what are its column names? Does `STBY?` answer?
5. Pull light 4's power (or its connector) while watching: `q` → `timeout` for
   light 4 only; on reconnect its power-on count +1.
6. Step the PWM: does each light answer `LOUT?` with the new level within a
   second? Any `partial` or `timeout` on the others while commands go out?
7. `stats`: `drop` stays 0 with the extension reading; `loop_max` under ~500.
8. With the extension stopped (nothing reading `-if02`): the lights still
   follow the PWM normally (`unread` climbs, nothing else changes).
