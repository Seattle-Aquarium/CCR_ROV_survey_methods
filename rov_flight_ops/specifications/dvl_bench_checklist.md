# DVL capture: bench checklist

Once per vehicle (Nereo, Lutris), before a capture is relied on. Everything
here was built against Water Linked's documentation, their demo DVL and a
simulator; none of it has met our DVLs yet. Tick, and note what you saw.

Vehicle: ____________   DVL software (DVL tab → events, or the capture's
`.json` → `about.version`): ____________   Date: ____________

## Connection

- [ ] Choose a flight folder. The DVL tab, section 1, says *Recording
      capture … into …\logs\dvl* and lists the files.
- [ ] The DVL address fills itself in from the BlueOS DVL extension
      (`(the BlueOS DVL extension)` after it). Address: ______________.
      If it does not, type the address you use in the browser and note why.
- [ ] All three lamps go solid: **TCP stream**, **web stream**, **web API**.
- [ ] Section 2: *clients on the DVL's JSON output* reads **2** (this program
      and the extension). If **1**, the extension is not connected to the DVL.
- [ ] Section 4: *periodic cycling* reads **off**; *range mode* names a
      running configuration.
- [ ] Section 4: *DVL clock vs laptop* shows an offset. NTP synced? ____.
      Offset: ______ ms.

## Did it reach the autopilot?

- [ ] Section 2's `→` rows: do they show rates, or *never sent by this
      vehicle*? ____________. (Rates mean mavlink2rest sees the extension's
      messages and the comparison works live; *never sent* means it has to
      be done afterwards against the mcap — see the diagnosis guide.)
- [ ] The extension's message type (`sends …`): ____________.

## The beams

- [ ] In water, with bottom lock, all four tiles are green and their
      distances agree with the altitude to within a few centimetres on a
      flat bottom.
- [ ] **Cover transducer 1** — on the A50's face, the one nearest the cable
      on the right as you look at the face (Water Linked's drawing). The tile
      that goes red should read **T1 · id 0**. Which went red? ________
- [ ] Repeat for transducer 3 (opposite the cable, on the left as you look
      at the face): **T3 · id 2** should go red. ________
- [ ] With the DVL mounted on the vehicle as it flies, do the positions on
      the tab (forward-port and so on) match where the transducers actually
      point? If not, note the DVL's mounting rotation offset: ______.

## The files

- [ ] Close the program. In `logs\dvl`, the capture's `.json` says
      `"state": "closed"` and lists sixteen files.
- [ ] `_velocity.csv` opens in a spreadsheet; `velocity_valid` is 1 with
      bottom lock and 0 lifted out of range.
- [ ] `_events.txt` has the lift as an invalid stretch, with its length.

## The experiments (from the diagnosis guide)

- [ ] **Snapshots and the DVL's CPU.** Five minutes each at Off, 5 /s,
      10 /s. `cpu_load` ______ / ______ / ______; report cadence unchanged?
      ____
- [ ] **Tether pull.** Extension and capture both connected, pull the tether
      for 60 s, reconnect. Download the mcap: is there a gap in
      `mavlink/255/0/DISTANCE_SENSOR` during the pull? ____ (A gap means a
      topside client can stall the DVL — report it before relying on
      captures over a flaky tether.)
- [ ] **Periodic cycling on for two minutes**, then off again. The events
      file says `periodic cycling is ON`, and `dvl_quiet` gaps appear about
      every 10 s. ____
- [ ] **Diagnostic log**, disarmed, 15 s, with a description. Saved file
      name and size: ________________. Did the capture show any change in
      the DVL's output while it recorded? ____

## Afterwards

Copy what you found into "Not yet verified" in
[dvl_artifacts_and_provenance.md](dvl_artifacts_and_provenance.md) — as
verified, or as what was seen instead.
