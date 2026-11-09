# Lutris lights: check and debug

For a Claude Code session on the ROV laptop. Written 9 November 2026 for the
first flight that records the four DeepSea SeaLites' own telemetry. Your job:
confirm that every link from the pilot's button to the saved files works, and
if one doesn't, find out why. With luck you only need to verify.

## What you are checking

1. **Buttons.** Cockpit (the desktop app on this laptop) sends MAVLink
   `MANUAL_CONTROL` button bits. ArduSub maps each bit through `BTNn_FUNCTION`
   and steps Lights1 by (1900 − 1100 µs) / `JS_LIGHTS_STEPS` per press, on
   Navigator servo output 15 (`SERVO15_FUNCTION = 181`).
2. **Light controller.** A QT Py RP2040 in the light box runs
   `lutris-lights 2.0.1-telemetry` (`lighting/code`, Aaron Marburg's
   firmware). It reads that PWM, sends `LOUT` to the four lights over RS-485,
   asks them `LOUT?` and `STAT?` in turn, and writes one JSON line per event
   on its second USB serial port.
3. **Light Monitor**, a BlueOS extension (image
   `zhrandell/blueos-light-monitor:zhr_light_telemetry`, version 0.2.0, port
   9420). It reads that port, records every line on the Pi, publishes a
   summary to MAVLink, and serves the state at `/api/v1/state`.
4. **rov_flight_ops**, this branch. The Lights tab (5th) reads the extension
   and copies its recording into the flight folder. After the flight,
   BlueOS logs → light logs downloads the Pi's own files.

Code: `blueos_extensions/light_monitor/` (its README has the install steps),
`rov_flight_ops/rov_flight_ops/lights/client.py` and `gui/lightspage.py`, and
`lighting/code/` (the firmware; read it, don't edit it).

## Ground rules

- Sections 0–7 only read. Do them in order: each one rests on the one before.
- Ask the user first before you:
  - change an ArduSub parameter or Cockpit's joystick mapping;
  - restart, reinstall, edit or disable the extension;
  - call `/api/v1/port/release`.
- Never touch the QT Py's serial ports. A Ctrl-C on its console stops
  `main.py`, which turns all four lights off.
- Don't delete or write anything in the Pi's `light_monitor` folder.
- Code changes go on `zhr_light_telemetry` only. Never merge to `main`, and
  ask before pushing. A push that touches `blueos_extensions/light_monitor/`
  or `lighting/code/` rebuilds the Docker image (a couple of minutes on GitHub
  Actions). Lutris then needs internet to pull it (Extensions → Light Monitor
  → Edit → Save).

## 0. Set up

- Check `git branch --show-current` in the repo says `zhr_light_telemetry`.
- `PY` below means rov_flight_ops's own Python:
  `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\venv\Scripts\python.exe`
  (`run_rov_flight_ops.bat` creates it). In Git Bash:
  `PY="$LOCALAPPDATA/CCR_ROV/rov_flight_ops/venv/Scripts/python.exe"`.
- Save the four scripts in the appendix to a scratch folder outside the repo.
- Run `curl -s http://192.168.2.2:9420/api/v1/health`. It should print
  `{"ok":true}`.

## 1. Is the extension installed and running?

Run `curl -s http://192.168.2.2:9134/v2.0/installed_extensions`, or look in
BlueOS → Extensions → Installed. It should list
`seattleaquarium.lightmonitor`, tag `zhr_light_telemetry`, enabled.

- **Not listed:** it was never installed. Walk the user through
  `blueos_extensions/light_monitor/README.md`, "Installing it on a vehicle".
  The Pi needs internet for the ~50 MB download.
- **Listed, but port 9420 is silent:** open BlueOS → Extensions → the Light
  Monitor card → its logs. Likely causes:
  - the image is still downloading;
  - another program already holds port 9420 ("Address already in use");
  - the container keeps restarting.

## 2. Does the extension hear the controller?

Run `PY lights_state.py`. The extension's page, `http://192.168.2.2:9420`
(also *Light Monitor* in BlueOS's menu), shows the same information.

| Line | Good | If not |
|---|---|---|
| `extension` | `0.2.0` | 0.1.x is the old image, which can't read this firmware. Re-pull it: Extensions → Light Monitor → Edit → Save. |
| `link` | `data`, a `/dev/serial/by-id/usb-…-if02` port (expected `usb-MicroPython_Board_in_FS_mode_<serial>-if02`, not yet seen on the vehicle), "telemetry port", connected | `console …`: the QT Py's telemetry port did not start. Data is still recorded (console `@L` lines); report it. `none` / "no light controller found on USB": the light box's USB to the Pi, or the box has no power. "could not open": the install is missing `Privileged` or the `/dev:/dev` bind; reinstall with the README's JSON. |
| `lines/s` | roughly 10–25 | Much lower: a light isn't answering (each unanswered question waits 2 s). 0: the controller is silent. |
| `bad` / `lost` | 0 / 0 (a few `lost` after a reconnect is fine) | Many `bad`: the firmware's format no longer matches the parser. Collect raw lines (section 7) and stop there. |
| `legacy` | `False` | `True`: only old-style console status text is arriving. |
| `firmware` | `lutris-lights 2.0.1-telemetry`, `Adafruit QT Py RP2040 with RP2040`, port `data` | |
| `pwm` | 1100 µs with lights off, signal `True` | Signal `False`: no PWM is reaching the QT Py (servo 15 wiring, or `SERVO15_FUNCTION`). |
| `light 1–4` | `off` or `on`, answered ≈ asked, power-on count present, temperatures plausible (near ambient while off) | `not_answering`: that light's power or RS-485. `unknown` for over a minute: no polling. |
| `mavlink` | enabled, `sent` rising, errors 0, publishing `True` | Errors: read `last_error`. Usually mavlink2rest isn't reachable at 127.0.0.1:6040 on the Pi. |
| `recording` | `lights_<UTC>.jsonl`, records rising, no error | An error means the disk or `/usr/blueos/userdata/light_monitor`. |
| `Pi clock` | within a few seconds | The Pi's clock stamps every record (`rx`) and the file names. Note any offset for whoever aligns the data later. |

## 3. Button mapping (ArduSub parameters)

**Background.** On 11/4 and 11/8 each press moved the lights three steps
(30 / 60 / 90 / 100 %). Lights1 brighter (function 32) sat on `BTN7`,
`BTN14` and `BTN17`, and dimmer (33) on `BTN8`, `BTN13` and `BTN16`. Cockpit
sets the bit of *every* slot that carries the function. See
`upadteManualControlState` (sic) in bluerobotics/cockpit
`src/libs/joystick/protocols/mavlink-manual-control.ts`. Three slots therefore
means three presses. QGC sends only the slot of the physical button.
`JS_LIGHTS_STEPS` ended 11/8 at 8. Full analysis:
https://claude.ai/artifact/7rxqnaCb1FaYNSigjkqK3A

**Wanted:**
- exactly one regular slot for lights1 brighter (32) and one for lights1
  dimmer (33);
- `JS_LIGHTS_STEPS = 10`;
- `SERVO15_FUNCTION = 181`;
- `SERVO15_MIN`/`MAX` and `RC9_MIN`/`MAX` at 1100/1900.

The pair to keep is `BTN13 = 33` and `BTN14 = 32`. That is QGC's D-pad
left/right and Nereo's mapping. Cockpit finds whichever slot is left by
itself. Clear `BTN7`, `BTN8`, `BTN16` and `BTN17` to 0.

**Check:** `PY lights_params.py`. It reads the newest dataflash log on the Pi
and sends the vehicle nothing; that takes a few seconds. Lutris logs while
disarmed (`LOG_DISARMED = 1`), so bench changes are in it. If that is 0, a
change made since the last arming isn't in the log yet; read the live values
in BlueOS → Autopilot Parameters, search `BTN`.

**If duplicates remain**, tell the user. The fix is in BlueOS → Autopilot
Parameters, with the vehicle disarmed: set the extra slots to 0 and
`JS_LIGHTS_STEPS` to 10. Don't change any `SFUNCTION`.

Nothing needs changing in Cockpit. Its mapping names functions, not slots.
Every second it checks that each function it maps has a slot. Apart from
keeping a shift button, it writes a `BTN` parameter only when a function has
no slot, and then takes the first disabled one.

Make the parameter change yourself only if the user explicitly asks.

Re-assigning lights in QGC's joystick page is how the duplicates were
created on 11/4; avoid it.

**Also on Lutris (11/8):**
- `BTN20 = 31` (lights1 cycle). Whichever Cockpit button carries it also
  moves the lights, one step per press, up and then back down. Possibly the
  source of the unexplained single steps on 11/8.
- `BTN19`, `BTN21` and `BTN22` carry lights2 functions. No output is set to
  Lights2 (182), so they do nothing.

## 4. Press test (needs the pilot)

Run `PY lights_watch.py 192.168.2.2:9420 180`. Ask the pilot to press
*brighter* ten times in Cockpit, about two seconds apart, then *dimmer* ten
times.

With `JS_LIGHTS_STEPS = 10` and one slot, each press should show:

| Press | ArduSub % | PWM (µs) | Told (LOUT %) |
|---|---|---|---|
| 0 | 0 | 1100 | 0 |
| 1 | 10 | 1180 | 43 |
| 2 | 20 | 1260 | 60 |
| 3 | 30 | 1340 | 70 |
| 4 | 40 | 1420 | 77 |
| 5 | 50 | 1500 | 83 |
| 6 | 60 | 1580 | 87 |
| 7 | 70 | 1660 | 91 |
| 8 | 80 | 1740 | 94 |
| 9 | 90 | 1820 | 97 |
| 10 | 100 | 1900 | 100 |

"Told" is the firmware's linearised brightness (`LINEARIZE_LIGHTING`, alpha
0.246 over 1100–1890 µs). It is meant to differ from ArduSub's %. The PWM can
read a few µs off, which can move "told" by 1. Each light's level should
match "told" within a second or two.

| You see | Means |
|---|---|
| Steps of 37.5 % (0, 37.5, 75, 100) | Nothing changed since 11/8: three slots per function and `JS_LIGHTS_STEPS` 8. |
| Steps of 30 % (0, 30, 60, 90, 100) | Three slots per function: section 3. 20 % steps mean two slots. |
| Steps of 12.5 % | One slot, but `JS_LIGHTS_STEPS` is still 8. |
| ArduSub % moves, PWM doesn't | `SERVO15_FUNCTION`, or the PWM wire to the box (signal `False`). |
| "Told" moves, one light's level doesn't | That light missed the `LOUT`. `RESYNC_LEVELS` is off, so it stays at the old level until the next change. After three `LOUT?` answers it shows as a mismatch. Note it: this is data Aaron wants. |
| ArduSub % is `None` | The extension isn't getting ArduSub's `Lights1` value from mavlink2rest. Logging still works. |

## 5. The MAVLink copy

- Section 2's `mavlink` line: `sent` rising, errors 0.
- Run
  `curl -s http://192.168.2.2:6040/v1/mavlink/vehicles/1/components/191/messages/NAMED_VALUE_FLOAT`.
  If that path fails, drop the `/v1`. It should return a fresh message whose
  `name` (a list of characters) starts `LT`. The names are listed in
  `blueos_extensions/light_monitor/light_monitor/mavlink.py`.
- Events also go out as STATUSTEXT from component 191, starting "Lights: ".
  Cockpit shows them.
- All of this should reach the Pi's tlog on BlueOS 1.4, but that is not yet
  confirmed on the vehicle (see section 7). None of it is in the `.BIN`.

## 6. rov_flight_ops: the Lights tab and its copy

- Start rov_flight_ops the usual way. The launcher installs this checkout in
  editable mode, so it runs the branch.
- On Monitoring, choose today's flight folder. Without a folder, nothing is
  copied.
- The **Lights** tab (5th) should show:
  - the extension connected at `http://192.168.2.2:9420`;
  - four lights matching the extension's page;
  - the same events.
- `<flight>\logs\lights\lights_topside_<UTC>.jsonl` should exist and grow.
  Check its size twice, ten seconds apart.
- The port is the `lights_port` setting (default 9420); the host is the
  vehicle address.
- After the flight: **BlueOS logs → light logs** downloads
  `/usr/blueos/userdata/light_monitor/lights_*` into `<flight>\logs\lights\pi\`.

## 7. After the flight: is the data complete?

Run `PY lights_summary.py <repo root> <file>` on the Pi's
`logs\lights\pi\lights_<UTC>.jsonl` and on the topside copy. It replays the
file through the extension's own parser and model. Expect:

- a time span covering the flight;
- 0 unreadable lines (one at the very start is fine) and 0 lost lines;
- no controller restarts, unless the light box lost power;
- silences over 3 s only where the tether or the USB dropped;
- each light's answered ≈ asked;
- power-on counts unchanged, unless a light lost power.

Then list the events the extension raised.

If the flight's tlog was downloaded (BlueOS logs tab), check that it holds
`NAMED_VALUE_FLOAT` messages named `LT…` from component 191. pymavlink is in
`PY`. That would confirm the MAVLink copy end to end.

The controller's raw text is the `line` field of each record, if you need to
look at it.

**The files.** On the Pi, in `/usr/blueos/userdata/light_monitor/` (BlueOS
File Browser → userdata):
- `lights_<UTC start>.jsonl` holds every controller line with the Pi's arrival
  time `rx`, the extension's events (`"x": "event"`) and the vehicle's power
  once a second (`"x": "vehicle"`).
- `lights_<UTC start>_1hz.csv` holds one row per second, one column per
  quantity.
- A new pair starts when the extension starts, at UTC midnight, and every
  200 MB.

The topside `lights_topside_<UTC>.jsonl` holds the same records. A
`"x": "gap"` record marks a tether drop that outlasted the extension's buffer
of 20,000 records (about 15 minutes).

## 8. Normal behaviour, not faults

- `set` lines always say `"ok": 0`. A light acknowledges a write with a bare
  CR LF, which the firmware reads as no answer.
- A light that stops answering stalls the controller about 2 s per question
  (4 s for `STAT?`). The pilot's changes wait meanwhile and the line rate
  drops.
- `RESYNC_LEVELS` is off, so a missed command lasts until the next change
  (mismatch events).
- `lighting/code/TELEMETRY.md` describes an older format (`"k"`, `"q"`,
  `"c"`). The firmware writes `"type"`, `"query"` and `"cmd"`, and the
  extension reads both. `lighting/code/tests` has 13 stale failures; they are
  not today's concern.
- The first `STAT?` temperature reads 0. T2 and T3 are the real ones.
- Lutris has no power monitor, so per-light watts and amps are modelled
  ("≈"), not measured.

## Report back

Give the user a short note:
- what passed;
- what failed, with the numbers and file names;
- what you changed (ideally nothing);
- where the recordings are.

## Appendix: scripts

All four were run before this was written, against the extension's
simulator or an 11/8 `.BIN`. The one exception is the live read in
`lights_params.py`, which needs the vehicle.

`lights_state.py`: one look at everything (section 2).

```python
import json, sys, time, urllib.request

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.2:9420"
s = json.load(urllib.request.urlopen(f"http://{host}/api/v1/state", timeout=5))
L, C, X = s["link"], s["controller"], s["extension"]
print("extension", X.get("version"), "mode", X.get("mode"),
      f"| Pi clock {s['now'] - time.time():+.1f} s from this laptop's")
print("link     ", L["source"], L["port"], "|", L.get("note"), "| connected", L["connected"])
print("lines/s  ", L["lines_per_s"], "| bad", L["bad_lines"], "| lost", L["lost_lines"],
      "| legacy", L["legacy_firmware"], "| last line", L["last_line_age_s"], "s ago")
print("firmware ", C["fw"], "|", C["machine"], "| port", C["port_mode"], "| uptime", C["uptime_s"],
      "s | restarts", C["restarts"])
print("pwm      ", C["pwm_us"], "us | signal", C["signal"], "| told", C["commanded_pct"],
      "% | rejected", C["pwm_rejected"])
print("ardusub  ", s["power"]["ardusub"])
for lt in s["lights"]:
    print(f"light {lt['id']} {lt['short']}: {lt['state']:<13} level {lt['level_pct']} told "
          f"{lt['commanded_pct']} | answered {lt['answered']}/{lt['asked']} | power-on "
          f"{lt['power_cycles']} (+{lt['restarts_this_session']}) | T2 {lt['temp2_c']} "
          f"RH {lt['humidity_pct']} | flags {lt['flags']}")
print("mavlink  ", {k: s["mavlink"].get(k) for k in ("enabled", "sent", "errors", "last_error", "publishing")})
print("recording", {k: s["recording"].get(k) for k in ("file", "records", "bytes", "disk_free_gb", "error")})
for e in s["events"][-12:]:
    print("event    ", e["lvl"], e["text"])
```

`lights_watch.py`: prints a line whenever the button chain moves (section 4).

```python
import json, sys, time, urllib.request

host = sys.argv[1] if len(sys.argv) > 1 else "192.168.2.2:9420"
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 120
t0, last = time.time(), None
while time.time() - t0 < secs:
    s = json.load(urllib.request.urlopen(f"http://{host}/api/v1/state", timeout=5))
    c = s["controller"]
    now = (s["power"]["ardusub"].get("lights_pct"), c["pwm_us"], c["commanded_pct"],
           [lt["level_pct"] for lt in s["lights"]])
    if now != last:
        print(f"{time.time() - t0:6.1f} s  ArduSub {now[0]}%  PWM {now[1]} us  "
              f"told {now[2]}%  lights {now[3]}", flush=True)
        last = now
    time.sleep(0.25)
```

`lights_params.py`: the light button slots and related parameters (section
3). Pass a `.BIN` path to read a downloaded log instead.

```python
import sys
from rov_flight_ops import blueos

LIGHT_FUNCS = {31: "lights1_cycle", 32: "lights1_brighter", 33: "lights1_dimmer",
               34: "lights2_cycle", 35: "lights2_brighter", 36: "lights2_dimmer"}

if len(sys.argv) > 1:                       # a .BIN already on disk
    p = blueos.parse_parameters(open(sys.argv[1], "rb").read())
    print("read from", sys.argv[1])
else:                                       # the newest log on the Pi (reads only)
    p, src = blueos.read_parameters_now("192.168.2.2")
    print("read from", src or "nothing -- is BlueOS reachable?")
slots = {}
for k, v in p.items():
    if k.startswith("BTN") and k.endswith("FUNCTION") and v is not None and int(v) in LIGHT_FUNCS:
        slots.setdefault(LIGHT_FUNCS[int(v)], []).append(k)
for func in sorted(slots):
    names = sorted(slots[func], key=lambda k: (int(k[3:].split("_")[0]), k))
    regular = [n for n in names if "_SFUNCTION" not in n]
    print(f"{func:<17} {', '.join(names)}" + ("   <-- more than one slot" if len(regular) > 1 else ""))
for k in ("JS_LIGHTS_STEPS", "SERVO15_FUNCTION", "SERVO15_MIN", "SERVO15_MAX", "RC9_MIN",
          "RC9_MAX", "LOG_DISARMED"):
    print(f"{k:<17} {p.get(k)}")
```

`lights_summary.py`: post-flight summary of one recording (section 7).

```python
"""Summarise a light recording (Pi lights_*.jsonl or topside lights_topside_*.jsonl).

usage: PY lights_summary.py <repo root> <recording.jsonl>
"""
import collections, json, sys
from datetime import datetime, timezone

sys.path.insert(0, sys.argv[1] + "/blueos_extensions/light_monitor")
from light_monitor.model import Model                # noqa: E402
from light_monitor.protocol import parse_line        # noqa: E402

model, kinds, gaps = Model(clock=lambda: 0.0), collections.Counter(), []
first = last = None
live_events, extension_starts = [], 0
for raw in open(sys.argv[2], encoding="utf-8"):
    try:
        r = json.loads(raw)
    except ValueError:
        kinds["broken json"] += 1
        continue
    x = r.get("x")
    if x == "event":
        live_events.append((r.get("rx"), r.get("lvl"), r.get("text")))
    elif x == "session":
        extension_starts += 1
    elif x == "gap":
        kinds["topside gap"] += 1
    if "line" not in r:
        continue
    t = r["rx"]
    if last is not None and t - last > 3.0:
        gaps.append((last, round(t - last, 1)))
    first, last = first or t, t
    rec = parse_line(r["line"])
    model.note_line(t, rec is not None)
    if rec is None:
        kinds["unreadable"] += 1
        continue
    kinds[rec["k"]] += 1
    model.ingest(rec, t)

utc = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S") + "Z"
if first is None:
    sys.exit("no controller lines in this file")
s = model.snapshot(now=last)
print(f"{utc(first)} to {utc(last)} ({(last - first) / 60:.1f} min), extension starts: {extension_starts}")
print("lines by type:", dict(kinds))
print("controller:", s["controller"]["fw"], "| restarts", s["controller"]["restarts"],
      "| lost lines", s["link"]["lost_lines"], "| last stats", s["controller"]["stats"])
print("silences over 3 s:", [(utc(t), d) for t, d in gaps[:20]], "(+%d more)" % max(0, len(gaps) - 20))
for lt in s["lights"]:
    print(f"light {lt['id']} {lt['short']}: answered {lt['answered']}/{lt['asked']}, power-on "
          f"{lt['power_cycles_session_start']} -> {lt['power_cycles']} (restarts "
          f"{lt['restarts_this_session']}), lifetime max temp {lt['max_temp_c']}, flags {lt['flags']}, "
          f"ends {lt['state']} at {lt['level_pct']}% (told {lt['commanded_pct']}%)")
print(f"events the extension raised live ({len(live_events)}):")
for t, lvl, text in live_events:
    print(" ", utc(t) if t else "?", lvl, text)
```
