# The lights: where each number comes from

What tab 4 *Lights* and the Navigation row show, what each figure is, and what
this program writes. Written 13 October 2026, before anything has run on
Lutris; the assumptions are listed at the end and in the
[extension's README](../../blueos_extensions/light_monitor/README.md).

## The chain

```
lights --RS-485--> controller (QT Py / Pico) --USB--> BlueOS Light Monitor (Pi)
                                                       |-- /api/v1/state ----> this program
                                                       |-- /api/v1/records --> this program's copy
                                                       '-- MAVLink ---------> tlog / mcap / Cockpit / QGC
```

The controller's format is [`ccr.lights/1`](../../lighting/code/TELEMETRY.md).
The extension turns it into one state document (`light_monitor.state/1`);
this program draws that document and nothing else, so it cannot disagree with
the extension's own page.

## Three kinds of number

| kind | shown as | examples | where it comes from |
|---|---|---|---|
| **answered** | plain | level, temperatures, humidity, power-on count, run time, fault flags, serial | the light's own answer to `LOUT?`, `STAT?`, `INFO?`, … |
| **measured** | plain | vehicle V / A / W; light supply V / A / W (only with a monitor) | ArduSub's `SYS_STATUS`; the INA226/INA238 in the light box |
| **expected** | `≈` | each light's W and A; the four lights' total | 98 W at full output × DeepSea's dimming curve at the level the light reports — a model |

Per-light current cannot be measured with the hardware as built: one supply
feeds all four lights, and Lutris has no monitor fitted. "Volts per light" is
the shared supply's voltage.

## What the states mean

| state | shown | rule (extension `model.py`) |
|---|---|---|
| on / off | green / Seafoam ring | answering; reported level > 0 / = 0 |
| not answering | red | 3 questions in a row unanswered |
| stale | red | no answer for 15 s |
| mismatch | red | reported level ≠ commanded, 3 answers in a row |
| standby | red | `STBY?` = 1 |
| fault | red | `STAT?` flags ≠ 0 |
| restarted | power-ons in red, with "(+n this session)" | `STAT?` power-on count rose |

## What this program writes

| file | what |
|---|---|
| `<flight>/logs/lights/lights_topside_<UTC>.jsonl` | the extension's records, copied as they are made, from the moment a flight folder is chosen; a `"x": "gap"` record where records were no longer available to copy |
| `<flight>/logs/lights/pi/lights_<UTC>.jsonl`, `…_1hz.csv` | the extension's own recording, downloaded on *BlueOS logs → light logs* |

Read-only toward the vehicle: GETs to the extension and File Browser. The
extension's *Release port* is on its own page, not here.

## In the mcap and tlog

The extension's NAMED_VALUE_FLOATs (system 1, component 191) were dropped by
this program's mcap extraction, which kept one sender per message type. They
are now kept from every sender for NAMED_VALUE_FLOAT and STATUSTEXT
(`mcap_extract.MULTI_SOURCE`, in both rov_flight_ops and rov_imagery_processing),
so they appear in the telemetry CSV as `NVF.LT4_LVL`, `NVF.LT4_PC` and so on.

`mcap_to_csv`'s transect CSVs are unchanged: their 45 columns are a contract
the VIAME and percent-cover joins rely on, and adding light columns is a
decision for the team. Its readers still keep one sender per type; the change
above, ported, is what adding columns would need.

## Assumptions

* The extension is on the vehicle's address at port 9420 (setting `lights_port`).
* Light *n* is RS-485 address *n*, at 1 front left, 2 rear left, 3 rear right,
  4 front right.
* `SYS_STATUS` on Lutris measures the supply that also feeds the light box's
  boost converter (the converter drops out near 8.6 V at its input — Aaron
  Marburg, CCR_development#50).
