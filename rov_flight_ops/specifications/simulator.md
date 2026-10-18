# The ROV flight simulator

A switch in the title banner — left of Diagnostics, there whether the banner is
open or folded — that turns the whole program into a simulated flight: the
vehicle flying survey lanes off Pier 59, the laptop monitored, the DVL
streaming, the UGPS solving, the Pi full of the morning's files. It exists so
the program can be learned away from the pressure of a real dive, and so the
way each kind of failure looks can be seen before it happens for real.

Code: `rov_flight_ops/sim/` (the simulator), `gui/simbanner.py` (its switch),
`bands.py` (the Live monitoring colours, used with or without it).

## What switching it on does

| Tab | In simulation |
|---|---|
| Banner | The switch lights up in the simulator's purple. The Vehicle connected and Logging lamps, the versions (BlueOS 1.4.6, ArduSub 4.5.7, Cockpit 1.19.0-beta.2) and the recorder line read exactly as on a real flight. Under the switch (beside it, folded): the fault clock and **Show what's wrong**. |
| Monitoring | A simulated flight recorder, recording, with the last ten minutes already charted. Every Live monitoring reading is simulated, including the ones L342-D cannot publish (marked *◇ simulated*). The go / no-go board judges the simulated vehicle with the program's own rules, so each fault on Navigation and DVL shows on it too. Check the network shows what a healthy station's check looks like. |
| Parameters | The simulated vehicle's parameters are Nereo's reference file with the 15 October navigation set on top and four changed on purpose: `JS_GAIN_DEFAULT` 0.5 and `JS_LIGHTS_STEPS` 4 (key parameters), `PSC_VELXY_P` 6 and `NTF_LED_BRIGHT` 1 (others). The parameters the autopilot rewrites by itself are given flight-like values, and are not listed as differences. The simulated ROV is armed, so *Set to reference* stays disabled; were it disarmed, a set would be refused — the simulator sends nothing. |
| Navigation | The program's own collector reading a simulated mavlink2rest: the vehicle flying four 40 m lanes in the slip north of Pier 59, in the acoustic profile with POSITION_DELTA from the DVL (as on 15 October 2026), the EKF origin confirmed, guidance following the lane being flown, and the UGPS receiver row reading a simulated G2. |
| DVL | The program's own capture reading a simulated Water Linked A50 on localhost — the real TCP stream, web stream, web API and acoustic views — live only, writing nothing. Section 1's delivery figures read the simulated extension's MAVLink. |
| Transects | The lanes already flown, as transects T1, T2, … typed for this flight; Preview draws the simulated dive profile. |
| BlueOS logs | Search lists a simulated Pi. Download describes what it would copy and copies nothing; Delete is refused because the simulated ROV is armed — exactly what the real vehicle says mid-flight. |
| Flight summary, Analyze transects | Not simulated: they read recordings already on disk, which the simulator does not make. Each says so. |

## The fault clock

One clock runs the whole program: **30 s with a fault, then 15 s with
everything green, then the next fault.** During each faulty half-minute every
live tab has one thing wrong — Navigation one, DVL one, and on Monitoring one
reading in *every* group, so whichever group is open has something to find.
Each tab (and each Monitoring group) works through its own list below, one
entry per cycle, and starts again at the end.

The banner says a fault is active and how long is left. **Show what's wrong**
names it for the tab (and group) on screen, and writes the explanation to the
log pane; it hides again when the next fault starts.

A fault lives on its own tab: the DVL losing bottom lock on the DVL tab does
not also lose it on the Navigation tab. The one exception is the Pi Ethernet
outage — the failure the 15 October 2026 dive actually had — which reaches
everything that talks to the Pi, because that is how it shows up in the field.

Faults wind down in their last few seconds so that readings judged on a mean
(temperatures, rates) are back to green by the time the green interval starts.

## Where the numbers come from

* **The laptop**: the medians and spreads of the 27 L342-D laptop-monitor logs
  (September–October 2026); see `monitoring_bands.md` for what they showed.
* **The vehicle**: the 15 October 2026 OTS flight — its EKF origin
  (47.607567, −122.34387), its navigation parameters, the UGPS topside's
  position on the pier, the ~14.5 m depth there, survey speed 0.35 m/s.
* **The DVL**: that dive's capture with the vehicle a metre off the bottom —
  9.2 reports a second at 1.1 m (3.8 at 14 m); RSSI −38.6, −35.3, −35.0,
  −37.4 dBm per beam falling ~35 dB a decade of range; noise floors −97.2,
  −97.0, −100.0, −95.4 dBm; figure of merit ~0.0002; ~109 ms processing; its
  clock 871 days behind (software 2.7.2, never NTP-synced); 19–20 °C.
* **MAVLink rates**: VISION_POSITION_DELTA at ~7/s and DISTANCE_SENSOR at the
  DVL's report rate, as mavlink2rest counted them that day.
* **The Pi's files**: names, folders and sizes from the Port of Seattle
  `pi_downloads.jsonl` manifests.

## Guarantees

* **It only starts with no vehicle.** Not while the Vehicle connected lamp is
  lit, the Navigation tab is reading a live vehicle, or a DVL is streaming;
  and only after one read-only question to the vehicle's address goes
  unanswered.
* **It switches itself off if a vehicle appears.** While it runs it asks the
  vehicle's address every 5 s (one small GET) and watches the real recorder;
  the first answer turns it off and says why.
* **Nothing reaches a vehicle.** Simulated services listen on 127.0.0.1;
  the simulated vehicle's MAVLink host is `simulated-rov.invalid`, which cannot
  resolve; its connection refuses `send` and `request_message` whatever the
  write lock says; Delete on BlueOS logs is refused.
* **Nothing lands in a real flight folder.** The program's flight folder is the
  simulator's own, `%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\simulator\flights\…`,
  emptied each time it starts; Browse… is refused while it runs. The DVL
  capture runs as a live view; the recorder writes nothing.
* **Switching it off puts everything back**: the flight folder chosen before,
  the real recorder (which kept watching underneath), the transects typed, the
  survey plan drawn, the profile chosen, the banner's versions.

`tests/test_simulator.py` holds each of these.

## The faults

### Monitoring → CPU

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | the laptop is thermal throttling | The processor has reached its temperature limit and Windows is holding its clock down. cpu_frequency_mhz falls toward 1 GHz and the ceiling drops, while cpu_usage_pct climbs: the same work, done slower. In the field: shade the laptop, open the case vents, close anything that is not flying the ROV. |
| 2 | a process is pinning the CPU | Something is using every core -- an update, an antivirus scan, a stuck renderer. cpu_usage_pct and the busiest core sit near 100 % and the package draws more power and runs hotter. Cockpit's video will start to drop frames. Find it in Task Manager and end it. |
| 3 | the CPU is power-limited | Windows is capping the processor because the power supply cannot keep up -- a small car charger or a weak inverter on the boat. The ceiling drops to about half and the clock with it, while the package power sits flat and low. Use the full-size charger. |

### Monitoring → Memory

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | memory is leaking | A program keeps allocating memory and never gives it back. RAM in use and committed memory climb steadily toward the limit and the memory still available falls below 2 GB. Restart the program that is growing -- usually the video client -- between transects. |
| 2 | the laptop is paging hard | Memory has been pushed out to disk and is being read back: pages_per_s stays in the thousands and the page file fills. Everything on the laptop becomes sluggish, video included. Close what is not needed. |
| 3 | the commit limit was reached | Committed memory hit the limit and Windows grew the page file to make room: watch memory_commit_limit_gb step up. The next allocation that does not fit makes a program crash. Restart whatever has grown before it does. |

### Monitoring → GPU

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | the video decoder is saturated | The graphics chip's video decoder is nearly fully booked -- a second camera stream, or a codec without hardware support. gpu_video_decode_pct jumps from about 7 % to near 90 % and the whole GPU is busy. Expect stutter and lag in Cockpit's video. |
| 2 | graphics memory is leaking | Shared graphics memory climbs past 3 GB and does not come back -- a leak in the video client, which grows until the client crashes. Restart Cockpit between transects. |
| 3 | the graphics chip is hot | The GPU, which shares the CPU's die, is near its temperature limit and drawing more power than video needs. A hot case in the sun, or something rendering heavily in the background. |

### Monitoring → Storage

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | the disk is nearly full | A large copy is filling the disk: free space falls under 10 GB and keeps falling. Downloads from the Pi will fail and the recorders run short. Free space before the next flight -- move old flights off the laptop. |
| 2 | something is hammering the disk | A background job -- a sync client, Windows Update, a video copy -- is writing hundreds of MB a second. The disk is busy past 85 % and requests queue up, so the recorders' writes wait. Pause syncing during flights. |
| 3 | the SSD is overheating | The SSD is above 70 °C, where it slows its own writes to cool down. A laptop in the sun with its vents blocked. |

### Monitoring → Network

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | the tether is congested | Pings to the Pi take hundreds of milliseconds and a quarter are lost; the Pi's replies slow past a second. Control and video both lag. Usually a noisy or overloaded link: something else is using the tether, or the line is wet at a connector. |
| 2 | the video stream has stopped | Cockpit is running but almost nothing is arriving: network_receive_mbps falls from about 12 Mbps to nearly zero. The camera stream has stopped -- the camera, its encoder or its stream on the Pi. Check the camera's own page. |
| 3 | the adapter is discarding packets | network_packets_discarded, normally flat all day, starts climbing: the laptop is throwing away packets it received. A driver or buffer problem on the laptop's side of the tether. |
| 4 | the tether link is saturated | Something is pulling 80+ Mbps down the tether -- a large download off the Pi during the flight. The link is nearly full, latency climbs, and the video competes with the download. Download after surfacing, not during. |

### Monitoring → Cockpit

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | Cockpit is leaking | Cockpit's memory and handle count climb without stopping -- an Electron leak. Past about 4 GB it will crash, usually at the worst moment. Restart it between transects. |
| 2 | a Cockpit process crashed | Cockpit normally runs as four processes; one has died -- usually its GPU process. The window stays open but the video freezes or goes black, and Cockpit's GPU share drops to nothing. Reload Cockpit. |
| 3 | Cockpit's renderer is spinning | Cockpit is using most of the CPU and GPU on its own -- a widget or a renderer stuck in a loop. Reload the view. |

### Monitoring → Power

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | the charger cannot keep up | The laptop is plugged in but still drawing on its battery: an underpowered adapter or a failing inverter. The charge falls slowly while the power source still says AC. Change the supply before the battery runs out. |
| 2 | the laptop is on battery and draining | Mains power has gone -- an unplugged cable or a tripped inverter -- and the laptop is drawing over 40 W from its battery. The charge falls fast toward 20 %, where Windows starts saving power at the expense of the video. |
| 3 | the Pi in the vehicle is overheating | The Raspberry Pi inside the vehicle passes 80 °C and starts slowing its own processor, which starves video and telemetry. It reached 76 °C on a survey in September. Shorter dives, or a cooler enclosure. |
| 4 | the laptop is cooking in the sun | The chassis and battery are hot and the fan is at its maximum: a laptop in direct sun on deck. Lithium batteries stop charging around 45 °C. Shade it. |

### Monitoring → Tether

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | the Pi's Ethernet port dropped | The Pi's own end of the tether lost its link: pi_eth_carrier_drops steps up by one, the Pi's byte count stops, and layer 2 briefly cannot find the vehicle. The Fathom-X pair still hears each other, which places the fault on the Pi's cable or port -- the 15 October 2026 finding. |
| 2 | the Fathom-X link has degraded | The two Fathom-X boards renegotiated far lower -- 31 Mbps one way and 66 the other. Under 40 Mbps the video will not fit, and the two directions this far apart usually means a wire disconnected rather than a noisy line. Check the tether connectors. |
| 3 | Windows put the tether adapter to sleep | Windows' power management has flagged the tether adapter for a low-power state -- the step before the link drops. Turn off "Allow the computer to turn off this device to save power" for that adapter; Check the network on this tab says which one. |
| 4 | the Pi's network interface restarted | The Pi's own count of bytes received went back to almost nothing: its Ethernet interface was reset, or BlueOS restarted its networking. Anything the Pi was doing over the tether was interrupted. |

### Navigation

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | the DVL lost bottom lock | The autopilot's downward range reads zero, so altitude goes invalid -- not zero -- and the DVL rows in the sensor matrix go red. Speed comes only from the estimator now. Flying too high, over a drop-off, or through a cloud of silt. |
| 2 | one UGPS receiver is weak | Receiver 2's signal is 14 dB below the other three, and the row under the map flags it. A fouled or shadowed hydrophone, or a cable partly out of its connector. The solution still works on three, with less margin. |
| 3 | the topside heading (HDT) stopped | The topside GNSS compass is still sending its position but not its heading, so the acoustic solution cannot be oriented: the vessel heading row goes red while its position stays green. |
| 4 | the acoustic position is lost | The UGPS stops producing a valid solution: GPS_RAW_INT's fix type drops to 0, the acoustic row goes red and the estimator falls back to relative aiding -- the position is now dead-reckoned from the DVL and will drift. |
| 5 | the Pi dropped off the network | The Pi's Ethernet went quiet for eight seconds, both ways -- the failure of the 15 October 2026 dive. Every MAVLink read times out, the banner's Vehicle connected lamp goes out, and three seconds in the autopilot's GCS failsafe disarms the vehicle. It reaches every tab: the DVL tab can no longer see the extension's MAVLink, though the DVL itself keeps streaming. Monitoring shows the vehicle unreachable and, once it answers again, disarmed. |
| 6 | the estimator reset and the position jumped | The estimator restarted its horizontal solution and the position jumped about 15 m before converging back. The map breaks the track rather than drawing across the jump, and the jump is listed beside the position. |
| 7 | one UGPS receiver is noisy | Receiver 1's noise floor is 12 dB above the others -- a thruster, a pump or the vessel's own sounder near that hydrophone. The row under the map flags it. |
| 8 | the topside GNSS compass went silent | Neither position nor heading is reaching the UGPS topside: the GNSS compass stopped sending NMEA, or the extension's listener died on a sentence it does not know (the October 2026 firmware-update problem). Both vessel rows go red. |
| 9 | a leak sensor is wet | ArduSub is sending "Leak Detected" every 20 seconds: a moisture sensor in one of the enclosures is wet. The Leak sensors row on the Monitoring board goes red. Surface, power down and open the enclosures before the water reaches the electronics. |
| 10 | the ROV's compass is disturbed | The vehicle's heading swings by twenty degrees either way while it flies straight -- magnetic interference from a piling or a steel hull nearby. Watch the heading arrow on the map. |

### DVL

| # | fault | what it looks like, and what to do |
|---|---|---|
| 1 | beam T3 is not firing | Transducer T3 (forward-port) returns nothing: no range, signal at the noise floor, and its tile goes red. The DVL still solves velocity on the other three, but its figure of merit gets worse. A damaged transducer, a cable fault, or something covering the face. |
| 2 | electrical noise is drowning the beams | The noise floor on all four beams has risen about 40 dB -- electrical noise from thrusters or lights coupling into the transducers. Signal to noise collapses, beams flicker invalid and velocity drops in and out. Look at the spectrum. |
| 3 | the DVL has lost bottom lock | No beam finds the bottom: altitude reads −1, velocity is invalid, and the stock BlueOS extension drops every invalid report -- so its rates under Delivery and stream health fall to zero. These are the gaps that end up in the mcap. |
| 4 | the BlueOS DVL extension stopped | The DVL is fine and streaming, but the BlueOS extension that forwards it to the autopilot has stopped: its MAVLink counts freeze and DISTANCE_SENSOR goes stale. The DVL's own page would look healthy -- this is why the capture reads both sides. |
| 5 | beam T1 is hitting a piling | Beam T1's (aft-starboard) range is far shorter than the others -- it is striking a pier piling rather than the bottom. The altitude estimate drops, the figure of merit worsens and velocity occasionally goes invalid. Move away from the pier. |
| 6 | periodic cycling is switched on | The DVL's periodic cycling is on, so every ten seconds it pauses to check its range settings and reports stop for about a second. The team runs with it off for exactly this reason. |
| 7 | reports are arriving in bursts | The DVL makes its reports on time but they reach the laptop late, several at once -- the network between them is stalling. The gaps are classed as delivery stalls, read from the DVL's own clock. |
| 8 | the DVL is overheating | The DVL's temperature climbs past 50 °C and it raises its own high-temperature warning. It happens when it runs out of the water for long -- on deck, in the sun. Turn the acoustics off on deck. |
| 9 | the DVL's acoustics are switched off | Somebody switched the acoustics off: the DVL stays connected but makes no velocity reports at all. The silence is classed as acoustics-off rather than as a fault in the stream. |
| 10 | reports are going missing | The DVL makes its reports -- its own clock and report counter say so -- but some never arrive. The gaps are classed as missing reports, which places the loss between the DVL and this laptop. |
