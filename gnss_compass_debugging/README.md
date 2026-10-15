# GNSS Compass → wl_ugps_external → UGPS G2: debugging handoff

**Written 2026-10-14 by Claude (desk research, no hardware attached) for a
fresh Claude session on the ROV laptop on the morning of 2026-10-15.**
Zach Randell will be there to answer questions, log in to web pages and
approve changes.

If you are that Claude: read this whole file before you touch anything. Then
work through [§7, the morning plan](#7-the-morning-plan). The two tools in this
folder were written and tested for this job:

| File | What it does | Writes to devices? |
|---|---|---|
| [`probe_topside.py`](probe_topside.py) | One-shot snapshot of the laptop network, the compass, BlueOS, the extension (status + container log) and the G2 API. Prints *findings* and saves raw files. | **No.** GET requests and an optional ping sweep only |
| [`nmea_check.py`](nmea_check.py) | Says whether a stream of NMEA would kill the extension's listener. `selftest`, `listen`, `file` modes. | **No.** Binds a UDP port and listens |

Both are standard-library Python, so either the system Python or
`%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\venv\Scripts\python.exe` will run them.

---

## 0. TL;DR

* **Symptom.** The Advanced Navigation GNSS Compass firmware was updated a few
  weeks ago (probably **2.30 → 2.47**; 2.47 is the newest public release).
  Since then:
  * the compass's own web page shows live pitch/roll and "three green boxes"
    but **no latitude/longitude**;
  * **nothing reaches the G2** through Clyde McQueen's `wl_ugps_external`
    BlueOS extension;
  * the desktop app (GNSS Compass Manager / INS Manager) **does** show a
    correct lat/lon, so the receiver and antennas are fine.
* **Strongest lead (proven offline).** The extension's UDP listener has **no
  error handling**. Any NMEA sentence that pynmea2 1.19 cannot parse, and any
  GGA/HDT with an empty number in it, kills the listener thread **for good**.
  The extension's web page keeps running and simply shows GGA/HDT red until
  the container restarts. With the compass's **factory-default Ethernet NMEA
  set**, two default sentences do exactly that:
  * `$GPHEV` (heave, 1 Hz);
  * `$GPHBT` (heartbeat, every **60 s**).

  `$GPTHS`, TSS1, LF-only line endings, a bad checksum and binary (ANPP) bytes
  have the same effect. See [§4](#4-the-test-results).
* **The likely story.** Updating the firmware through the web UI requires the
  compass to be in **DHCP mode**. A factory reset also puts it back on DHCP,
  wipes the static IP and returns the NMEA output to defaults. Connecting the
  desktop app needs an **ANPP** data port. Any of these can change the very
  settings this pipeline depends on:
  * a static 192.168.2.x address;
  * a UDP Client data port aimed at `192.168.2.2:6200`;
  * only GGA + HDT (+ PASHR) enabled on that port.

  The 2.30 → 2.47 changelog itself lists **no NMEA changes**.
* **The probable fix**, after you confirm the cause:
  1. Put the compass back on a static 192.168.2.x address.
  2. Set one data port to UDP Client → `192.168.2.2:6200`, NMEA output, with
     **only GGA and HDT** (PASHR optional) enabled and everything else off.
  3. **Restart the extension.** Its listener is dead, and a correct compass
     config will look like a failure until you do.
  4. Watch for **at least 3 minutes**: `$GPHBT` arrives only once a minute.
* **The web-page lat/lon problem may be a separate issue.** It could be a
  stale browser cache after the firmware change, a JS error, or the filter not
  being fully "valid". Diagnose it on its own track ([§7.7](#77-the-compass-web-page-latlon-problem-separate-track)).
  The G2 doesn't need that page to work.

---

## 1. Ground rules for this session

1. **Read-only first.** Run the probe and read the pages before changing
   anything.
2. **Every change gets Zach's OK first.** That covers the compass, the G2, the
   BlueOS extension and the Windows network settings. Say exactly what will
   change from what to what. Record the current values (screenshot or text)
   before the change, so it can be undone.
3. **You do not type passwords.** Zach logs in to the compass web UI himself
   (manuscript default `admin` / `password`, possibly changed). He also handles
   any SSH session. Once he is logged in, you can read pages in the browser
   pane.
4. **Don'ts unless Zach explicitly asks:**
   * no compass factory reset;
   * no firmware updates or downgrades of any device;
   * no G2 IP or network changes;
   * no changes to the Windows bridge, adapter or firewall settings;
   * no reboot of the vehicle.
5. **Vehicle stays disarmed on deck.** No ArduSub parameter writes. In
   rov_flight_ops, leave *Allow writes to this vehicle* unticked.
6. **Choosing a flight folder in rov_flight_ops writes nav logs into it.**
   Don't point it at a real flight folder just to look around.
7. **Don't push to `clydemcqueen/wl_ugps_external_extension`** and don't open
   issues or PRs there. A patch is a follow-up for Zach to decide on
   ([§9](#9-follow-ups-after-the-fix)).
8. **Report observations as observations.** "No GGA within 4 s" is a
   measurement. "The compass config is wrong" is a conclusion, and it needs
   the evidence named next to it.

---

## 2. The system

```
  vessel roof                    Pelican case / command console                  ROV
 ┌──────────────┐  PoE  ┌────────┐   ┌──────────────┐   ┌──────────────────────┐  tether  ┌──────────────────────┐
 │ AN GNSS      │──────▶│ PoE    │──▶│ Ethernet     │──▶│ laptop Ethernet port │          │ Raspberry Pi, BlueOS │
 │ Compass      │       │ switch │   │ switch       │   │   ║ Windows network  │  USB     │ 192.168.2.2          │
 │ (GC2-POE)    │       └────────┘   │              │   │   ║ bridge           │─FXTI────▶│  wl_ugps_external    │
 └──────────────┘                    │              │   │ laptop USB (FXTI)    │ Fathom-X │   UDP 6200 (NMEA in) │
                                     │   ┌────────┐ │   └──────────────────────┘          │   HTTP :<mapped port>│
                                     │   │ UGPS G2│◀┼──────────── HTTP PUT ───────────────│  Water Linked UGPS   │
                                     │   │ .2.94  │ │                                     │   extension → ArduSub│
                                     └───┴────────┴─┘                                     └──────────────────────┘
```

Because the Pi sits *behind the laptop's bridge*, every packet from the
compass to `192.168.2.2` crosses the laptop's Ethernet adapter. A capture on
the laptop can see it.

### Who expects what

| Hop | Contract | Source |
|---|---|---|
| Compass → extension | **Unicast UDP to 192.168.2.2:6200**, ASCII NMEA 0183, sentences separated by **CRLF**, ≤ 1024 bytes per datagram, GGA *and* HDT both present | extension `README.md`, `app/listen_thread.py` |
| Extension listener | `recvfrom(1024)` → `packet.decode().split('\r\n')` → `pynmea2.parse()` on **every** sentence. The loop catches only `socket.timeout`/`socket.error`, so anything else ends the thread | `app/listen_thread.py` L46–87 (tag `v1.1.0-beta.1`) |
| Extension → G2 | Every 1/`SEND_RATE` s (default 2 Hz), **only if GGA and HDT both arrived in the last 4 s**: `PUT {UGPS_HOST}/api/v1/external/master` with `{lat, lon, orientation, fix_quality, hdop, numsats, cog:0, sog:0}` | `app/send_thread.py`, `app/topside_position.py` |
| G2 | Uses that position *"only if GPS mode is set to use external GPS"* (`/api/v1/config/generic` → `"gps": "external"`; heading needs `"compass": "external"`). `orientation` must be 0–359.999. The G2 **still needs its own GPS fix** to sync its clock (up to 10 min) | G2 OpenAPI spec (demo, fw 3.3.4); extension README caveat |
| Extension `/status` | `gga_status`/`hdt_status` = "a sentence was received in the last 4 s", **not** fix quality. `latitude`/`longitude` start at **0** and keep the last value. `inject_status` = a PUT returned 200 in the last 4 s | `app/main.py` |

### Addresses

| Device | Address | Notes |
|---|---|---|
| Laptop bridge | usually `192.168.2.1` | bridge of the FXTI USB adapter + the built-in Ethernet port |
| BlueOS (Pi) | `192.168.2.2` | extension UDP 6200; its HTTP port is mapped by BlueOS (discover via `/helper/latest/web_services`) |
| UGPS G2 | `192.168.2.94` | web GUI and API, no auth |
| GNSS Compass | `http://gnss-compass.local/` (mDNS) | static IP before the update is **unknown**; ask Zach. Default mode is DHCP |
| DVL | `192.168.194.95` | not involved |

### Software versions on record

| Item | Version | Where recorded |
|---|---|---|
| GNSS Compass device firmware | **2.3** (= 2.30, 2023-11-10), "update available" → now presumably **2.47** (2024-04-05, newest public) | `CCR_development/files/ROV_firmware_software_versions.md` |
| GNSS receiver firmware | unknown; newest is 20230830, which pairs with device fw ≥ 2.47 | — |
| WL UGPS External | **v1.1.0-beta.1** (pynmea2 ~=1.19.0, Python 3.9) | `rov_flight_ops/NAVIGATION.md` compatibility table, 2026-09-18 |
| Water Linked UGPS ext. | v1.0.7 | same |
| BlueOS / ArduSub | 1.5.0-beta.39 / 4.5.7 | same |
| G2 firmware | 3.3.1 on record (3.3.4 available) | versions doc |

**Update the versions doc** once you have read the real values ([§8](#8-done-means)).

---

## 3. What the documentation says that matters here

From the **GNSS Compass Reference Manual v2.6** (May 2024; the online manual
is v2.9.1, 2026-05-05, with the same NMEA table) and the firmware changelog:

* **Ethernet NMEA defaults (Table 22).** On the GC2-POE, NMEA is *enabled by
  default* on Ethernet, all sentences with the `GP` talker:
  * at **8 Hz**: GGA, RMC, HDT;
  * at **1 Hz**: GLL, GNS, GSA, GSV, HDG, HDM, **HEV**, ROT, VBW, VLW, VTG,
    ZDA, PASHR, `PFEC,GPatt`, `PFEC,GPhve`;
  * every 60 s: **HBT**;
  * TSS1 is off by default.

  "NMEA 0183 broadcast packets are sent via multicast to 239.192.0.4 … cannot
  be disabled". That multicast doesn't reach the extension, which only gets
  unicast to 6200.
* **Data ports.** There are four, each **TCP Server, TCP Client or UDP
  Client**, each with input/output functions:
  * NMEA output, or ANPP, TSS1, Simrad 1000/3000;
  * output rates are **per port**;
  * the defaults are TCP 16718–16721;
  * each port should serve one remote device at a time.
* **Desktop app.** GNSS Compass Manager / INS Manager needs a data port set to
  **ANPP input + output** (normally TCP 16718). The **GNSS receiver** firmware
  can only be updated through that app, over TCP 16718.
* **Firmware via the web UI.** *"To upgrade the Ethernet compass via the web
  UI, it must be using a dynamic IP from a DHCP server."*
* **Factory reset** *"resets all … settings back to their factory defaults …
  will re-enable the DHCP Client and lose any static IP address settings."*
  2.30 added that a factory restore also resets the GNSS receiver config.
* **Main view gating.** *"Current Time, Position and Heading … data will only
  be valid if GNSS Compass has a HEALTHY status, there is a 3D GNSS fix or
  better, and the Heading is initialised."* The three status indicators are
  **Status** (HEALTHY), **GNSS Fix** (3D FIX) and **Heading** (HEADING
  LOCKED).
* **Position page.** It shows north/east metres from a reference point, not
  lat/lon. Lat/lon appears on the main/map view.
* **Changelog 2.30 → 2.47.**
  * 2.47 adds support for GNSS receiver fw 20230830 and better heading
    tolerance to ionospheric activity.
  * *Known limitation:* with receiver fw 20230830 the reported **position
    standard deviation is higher than expected**.
  * Receiver fw 20230830 has **no SBAS**.
  * Nothing about the NMEA format, data ports or the web UI.
* **The manual's own example GGA**
  (`$GPGGA,031644.460,3352.3501851,S,15112.2355488,E,6,00,1.4,…`) uses fix
  quality **6** and **0** satellites. The extension passes such a GGA
  through, and the G2 schema accepts it.

---

## 4. The test results

`nmea_check.py selftest` reproduces this table. It was cross-checked
sentence-by-sentence against the real pynmea2 1.19.0 with the extension's
code transcribed: 0 mismatches.

| Input on UDP 6200 | Extension |
|---|---|
| `GPGGA` / `GNGGA` (normal fix, or quality 6 with 0 sats) | used |
| `GPHDT` / `HEHDT` / `GNHDT` | used |
| GLL, GNS, GSA, GSV, HDG, HDM, RMC, ROT, VBW, VLW, VTG, ZDA, PASHR, `PFEC,GPatt`, `PFEC,GPhve` | parsed, ignored |
| **`$GPHEV`** (factory default, 1 Hz) | **listener thread dies**: `SentenceTypeError` |
| **`$GPHBT`** (factory default, every 60 s) | **dies**: `SentenceTypeError` |
| `$GPTHS` (NMEA-4 true heading, instead of HDT) | **dies** |
| TSS1 (`:00FFFF -0012 …`, not NMEA-framed) | **dies**: `ParseError` |
| GGA with no fix (`,,,,,0,00,,,M,,M,,`) | **dies**: `ValueError` |
| GGA with empty HDOP, or empty altitude | **dies** |
| HDT with an empty heading | **dies** |
| bad checksum | **dies**: `ChecksumError` |
| several sentences in one datagram with **LF-only** endings | **dies**: `ParseError` |
| binary bytes (an ANPP port pointed at 6200) | **dies**: `UnicodeDecodeError` |
| datagram > 1024 B | cut at 1024; harmless or fatal depending on where the cut falls |

**What a dead listener looks like:**

* In `/status`: `gga_status` and `hdt_status` go false 4 s after the death
  and stay false, while the web page keeps responding.
* In the **container log**: an uncaught-exception traceback through
  `listen_thread.py` that names the offending sentence (for example
  `SentenceTypeError: ('Unknown sentence type GPHEV,', '$GPHEV,0.02,A*11')`).
  The `Receiving XXX messages` lines just before it list every sentence type
  that arrived first.
* **After a restart**, GGA/HDT can go green for a moment and then die again
  when the next bad sentence arrives: within a second for HEV, up to 60 s for
  HBT.

---

## 5. Hypotheses, ranked

Each one comes with the fastest test that separates it from the others.

| # | Hypothesis | For | Fastest test |
|---|---|---|---|
| **H1** | The compass's **NMEA data port toward 192.168.2.2:6200 is gone or changed** (reset, repurposed to ANPP for the desktop app, or rate set to 0) | The update path forces DHCP; the desktop app needs an ANPP port; the changelog shows no NMEA format change | Compass web UI → **Configuration → Data Ports** |
| **H2** | The port exists but **sends a sentence that kills the listener** (factory-default rates bring back HEV/HBT; or TSS1, which the [GNSS_compass_logging 1-pager](https://github.com/Seattle-Aquarium/CCR_development/blob/main/completed_1-pagers/GNSS_compass_logging.md) proposed enabling) | Proven mechanism (§4) | **Extension container log** shows a `listen_thread` traceback. `probe_topside.py` prints it |
| **H3** | **Compass IP changed** (left in DHCP after the update, or factory reset), so its UDP can't reach 192.168.2.2 | The update procedure requires DHCP | Probe §2: compass address link-local (169.254.x.x) or outside 192.168.2.0/24. Compass **Ethernet Settings** page |
| **H4** | The compass's **navigation solution is flagged invalid**, so the web page hides lat/lon *and* GGA goes out empty or with quality 0, which also kills the listener (H2 mechanism) | Would explain **both** symptoms with one cause. The desktop app shows lat/lon from the ANPP state packet even when validity flags are false | Compass **View → Status** (filter-initialised flags) and **Device Information** (device vs receiver fw). A raw GGA capture (§7.5) |
| **H5** | **Browser-side only**: a cached old-firmware web app, a JS error, or a map library that needs the internet | Explains the web-page symptom only | Hard reload / InPrivate / another browser; browser console |
| **H6** | **G2 side**: `gps`/`compass` not `external`, or the PUT is rejected (e.g. `orientation` = 360.0 → 400) | Independent of the compass but blocks the end result | Probe §4 (`/api/v1/config/generic`); `Got HTTP Error` lines in the extension log |
| **H7** | **Extension/BlueOS side**: the extension is stopped, crash-looping, or has a changed `UGPS_HOST`; the 6200 mapping is missing | Not obviously linked to the firmware, but cheap to rule out | Probe §3 (service list, installed record, container status) |
| **H8** | **Topside path**: bridge not forwarding, PoE or switch port | Web UI and desktop app working argues against it, but they talk to the *laptop*, not the Pi | Probe §1/§2b. Capture on the laptop's Ethernet adapter (§7.5) |

H1–H3 often come **together** after a DHCP-mode update or a reset. Expect
to fix more than one, and use the probe to check each.

---

## 6. Questions to ask Zach first

Two minutes of answers can save an hour.

1. Which firmware did you update from and to? Did you **also** update the
   **GNSS receiver** firmware with the desktop app?
2. During or after the update, was the compass **switched to DHCP**, **factory
   reset**, or were any **Data Ports** changed (for example, to connect the
   desktop app)?
3. What was the compass's **IP address before**? Any photos or notes of the
   old Data Ports page?
4. What exactly does the **WL UGPS External** page in BlueOS show now? Red
   "Waiting for GGA/HDT"? Does it flash green briefly after the extension
   restarts?
5. When was the **last good dive** with the compass feeding the G2? Was
   anything else updated since then: BlueOS, the G2 firmware, extensions,
   Windows on the laptop?
6. Did anyone enable **TSS1** or other outputs on the compass, for example
   for the pitch/roll logging idea?

---

## 7. The morning plan

### 7.1 Get the files and run the probe

```powershell
cd <repo>                       # CCR_ROV_survey_methods on the ROV laptop
git pull                        # if this folder came via GitHub
python gnss_compass_debugging\probe_topside.py --sweep
```

(If `python` isn't found, use
`%LOCALAPPDATA%\CCR_ROV\rov_flight_ops\venv\Scripts\python.exe`.)

Run this with the full system powered: compass on PoE, G2 on, tether
connected, BlueOS booted, network bridge up. It writes
`gnss_compass_debugging/probe_output/<time>/`:

* `report.txt`;
* `extension_container.log`;
* `g2_api.json`;
* `extension_status_samples.json`;
* `ipconfig_all.txt`, `arp_*.txt` and the other raw files.

The last block, **FINDINGS**, lists the observations.

The probe can't see the compass's configuration (it's behind a login). That's
the next step.

### 7.2 Read the extension's state

* **Traceback in `listen_thread`** → H2 or H4 confirmed. The last line names
  the sentence. Go to 7.3 to find which port sends it, then 7.6.
* **No traceback, `gga_status`/`hdt_status` false, no `Receiving …` lines
  since the last start** → nothing is arriving: H1, H3 or H8. Go to 7.3, and
  to 7.5 if the config looks right.
* **GGA and HDT true, inject false** → H6/H7. Read the `Got HTTP Error` /
  `Got exception` lines and probe §4.
* **Everything true** → the topside pipe works. Check the G2's own GUI and
  `position/master`, and whether the acoustic side is the real complaint.

You can also read the log in the browser: BlueOS → *Extensions* → WL UGPS
External → logs. Or in the BlueOS *Terminal*:
`red-pill`, then `sudo docker ps | grep -i ugps` and
`sudo docker logs --tail 300 <name>`.

### 7.3 Read the compass configuration

Zach logs in. You read and screenshot. In the browser pane, open
`http://gnss-compass.local/` (or the IP from the probe); Zach may need to
approve the site. Record, without changing anything:

1. **View → Device Information**: serial number, device firmware, hardware
   version, GNSS receiver firmware.
2. **View → Status**: every flag, especially *Navigation Filter
   Initialised*, *Heading Initialised*, *UTC Time Initialised*, GNSS fix type
   and any failure bits.
3. **Configuration → Ethernet Settings**: DHCP or static; address, mask,
   gateway.
4. **Configuration → Data Ports**: for **each** of the 4 ports, record mode
   (TCP Server/Client, UDP Client), destination IP:port, input and output
   functions, and the per-message NMEA rates (especially HEV, HBT, TSS1, THS,
   GGA, HDT).
5. The **main view**: what exactly is blank, and what do the three indicators
   say in words?

Then match the evidence to H1–H4.

### 7.4 Read the G2

The probe already pulled the G2 API, but check that:

* `/api/v1/config/generic` → `gps` and `compass` are `"external"`;
* `/api/v1/about/status` → `gps` ≥ 1 (the G2's own clock-sync fix);
* `/api/v1/status_report/` has no non-`ok` groups.

The G2 web GUI's *System Status* page also shows its own view.

### 7.5 Optional: capture the raw NMEA

Do this if the config looks right but the extension still doesn't get valid
data, or to prove what the compass actually emits.

**Option A, no admin rights (preferred).** With Zach's OK, set a **spare**
compass data port to UDP Client → *the laptop's* 192.168.2.x address, port
**6201**, NMEA output, *same message rates* as the 6200 port. This leaves the
real port untouched. Then run:

```powershell
python gnss_compass_debugging\nmea_check.py listen --port 6201 --seconds 180 --save gnss_compass_debugging\captures\nmea.jsonl
```

Windows may ask to let Python through the firewall. Zach decides.

The output lists every sentence type:

* its rate;
* whether the extension would use it, ignore it, or **die on it**;
* the datagram sizes;
* whether line endings are LF-only.

Listen for **≥ 70 s** so HBT has a chance to appear. Remove the spare port
afterwards, or leave it if Zach wants it.

**Option B, needs an elevated shell, which Zach opens.** Use Windows'
built-in `pktmon` on the laptop's Ethernet adapter. Traffic for the Pi
crosses it:

```
pktmon filter remove
pktmon filter add NMEA -t UDP -p 6200
pktmon start --capture --pkt-size 0 --file-name %TEMP%\nmea.etl
:: wait ~90 s
pktmon stop
pktmon etl2pcap %TEMP%\nmea.etl --out %TEMP%\nmea.pcapng
```

Open the pcapng in Wireshark if it's installed, or extract the UDP payloads
with a short script and feed them to `nmea_check.py file`.

### 7.6 Apply the fix

Get **Zach's OK for each step** and record the current values first.

1. **Compass → Ethernet Settings**: set a static address on `192.168.2.x/24`.
   Use the old address if Zach knows it. Otherwise pick an address the sweep
   showed unused, avoiding `.1`, `.2` and `.94`. The web session will drop
   when the IP changes; reconnect at the new address or
   `gnss-compass.local`.
   *(Note for the record: future web-UI firmware updates need DHCP again.)*
2. **Compass → Data Ports**: configure **one** port as follows.
   * **UDP Client**, destination `192.168.2.2`, port `6200`, output function
     **NMEA**.
   * Message rates:
     * `GPGGA` 1–5 Hz;
     * `GPHDT` 2–8 Hz;
     * `PASHR` 1 Hz is optional (it's harmless, and the extension logs it
       when `LOG_NMEA` is on);
     * **everything else 0/off**: HEV, HBT, GLL, GNS, GSA, GSV, HDG, HDM,
       RMC, ROT, VBW, VLW, VTG, ZDA, PFEC, TSS1.

   Keep a separate port as ANPP TCP Server 16718 if the desktop app is
   wanted.
3. **Restart the extension.** Use BlueOS → *Extensions* → WL UGPS External →
   restart (or disable/enable). The kraken API has
   `POST /kraken/v2.0/extension/<identifier>/restart`; check the live API
   at `http://192.168.2.2/kraken/v2.0/docs` before using it.
4. **G2.** Only if the probe showed it wasn't already set, and only with
   Zach's OK: `gps` = external, `compass` = external, in the G2 GUI settings.
5. **Verify, for at least 3 minutes:**
   * `probe_topside.py` (no `--sweep` needed) shows GGA, HDT and inject true
     in all samples, and no new traceback;
   * the G2's `/api/v1/position/master` lat/lon is within a few metres of the
     extension's GGA, and its `orientation` ≈ HDT. The probe prints this
     distance;
   * the G2 GUI shows the vessel at the right place and heading;
   * in rov_flight_ops → *2 Navigation*, the vessel row reads fresh
     GGA/HDT. This is optional; only do it with a scratch flight folder.

If the compass can't be made to drop a fatal sentence, the fallback for a
field day is to set the G2 back to `onboard` GPS/compass. That's noisier, as
the manuscript explains. Say so to Zach rather than doing it silently.

### 7.7 The compass web page lat/lon problem (separate track)

In order of cost:

1. **Hard reload** (Ctrl+F5), an **InPrivate** window, or another browser.
   After a firmware change the browser may still run the *old* web app from
   cache against the *new* firmware.
2. Open the page in the browser pane and read the **console messages** and
   **failed network requests**. Look for a JS exception or a CDN or map-tile
   request that fails without internet. If you find a failed request,
   compare with the laptop on a phone hotspot.
3. **View → Status.** If *Navigation Filter Initialised* is false while GNSS
   shows 3D FIX, that's H4. It's also why GGA might go out empty.
4. Compare device firmware with GNSS receiver firmware. 2.47 pairs with
   receiver fw 20230830, which has a known "position SD higher than expected"
   limitation.
5. If the status is fully healthy and the page is still blank, gather Device
   Information, screenshots and console errors for
   **support@advancednavigation.com**. Don't factory reset as a guess.

---

## 8. Done means

* The extension shows GGA, HDT and injection green for ≥ 3 min, with no
  traceback in its log since the last start.
* The G2 `position/master` matches the compass (≤ a few m) and its
  orientation matches HDT. The G2 is set to external GPS + compass.
* The root cause is written down with its evidence. Add a short **"Outcome
  (2026-10-15)"** section at the bottom of this file covering:
  * what was found;
  * what changed, from what to what;
  * the final compass Ethernet and Data Ports settings, as text, not only
    screenshots;
  * firmware versions read from Device Information.
* `CCR_development/files/ROV_firmware_software_versions.md` updated with the
  real compass device and receiver firmware, the G2 firmware and the
  extension versions, if that repo is on the laptop. Otherwise list them for
  Zach.
* If the web-page lat/lon issue remains, it's written up separately with what
  was ruled out.

---

## 9. Follow-ups after the fix

Propose these to Zach. Don't do them unasked.

* **Harden the extension.** The upstream fix is small, in
  `app/listen_thread.py`:
  * wrap each `pynmea2.parse` + `recv_*` call in
    `try/except (pynmea2.ParseError, ValueError, IndexError, AttributeError)`,
    log once per sentence type and `continue`;
  * also catch `Exception` around `recv_packet` in `run()` so the thread can
    never die;
  * `recvfrom(65535)`;
  * `packet.decode('ascii', 'replace').splitlines()` instead of
    `split('\r\n')`;
  * skip GGA with fix quality 0 or empty fields instead of raising.

  `nmea_check.py selftest` is a ready-made list of test inputs. This would be
  a PR to Clyde's repo or a Seattle Aquarium fork, which is Zach's call.
* **Write the compass configuration into the field protocol** (manuscript
  supplementary, network configuration section, and the field checklist):
  static IP, the 6200 data port, the exact message set. Note that firmware
  updates need DHCP and can reset these settings.
* The extension's `/status` would be more useful if it reported the listener
  thread being alive and the last parse error. Same PR.

---

## 10. Reference

**Endpoints (all GET unless stated).** Replace `<p>` with the extension port
from `/helper/latest/web_services`.

```
BlueOS services      http://192.168.2.2/helper/latest/web_services
Extension status     http://192.168.2.2:<p>/status
Installed exts       http://192.168.2.2/kraken/v1.0/installed_extensions
Containers           http://192.168.2.2/kraken/v1.0/list_containers
Container log        http://192.168.2.2/kraken/v1.0/log?container_name=<name>&timeout=3   (streams)
Kraken API docs      http://192.168.2.2/kraken/v2.0/docs
G2 about/config      http://192.168.2.94/api/v1/about   /api/v1/config/generic   /api/v1/about/status
G2 vessel position   http://192.168.2.94/api/v1/position/master
G2 status groups     http://192.168.2.94/api/v1/status_report/
G2 OpenAPI spec      http://192.168.2.94/swagger/swagger.json   (public demo: https://demo.waterlinked.com/swagger/swagger.json)
G2 external input    PUT http://192.168.2.94/api/v1/external/master   (what the extension sends; don't send by hand without Zach's OK)
Compass web UI       http://gnss-compass.local/  (login required)
```

**Sources.**
* The extension: <https://github.com/clydemcqueen/wl_ugps_external_extension>
  (deployed tag `v1.1.0-beta.1`; `main` @ c061c95 has only a UI addition
  since). Testing tools: <https://github.com/clydemcqueen/wl_ugps_external>.
* GNSS Compass Reference Manual v2.6:
  <https://advancednavigation.com/wp-content/uploads/2024/05/GNSS_Compass_Reference_Manual_v2.6.pdf>
  (§2.1 changelog, §8.6 NMEA, §9.1 web UI, §9.2.8.3 receiver fw, §10.1
  Ethernet).
* Online docs: <https://docs.advancednavigation.com/gnss-compass/Introduction.htm>,
  the [firmware changelog](https://docs.advancednavigation.com/gnss-compass/Firmware%20Changelog.htm)
  and the [NMEA 0183 page](https://docs.advancednavigation.com/gnss-Compass/Operation/NMEA%200183.htm).
* G2 API: the OpenAPI spec above, schema `SetMasterExternalPayload`.
* In this repo: `rov_flight_ops/NAVIGATION.md` (compatibility table, what
  `/status` does and does not mean) and `rov_flight_ops/rov_flight_ops/nav/extensions.py`
  (the end-to-end trace). Manuscript supplementary: network configuration and
  topside GNSS compass integration.
