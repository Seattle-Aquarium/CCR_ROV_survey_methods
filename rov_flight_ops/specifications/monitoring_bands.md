# Live monitoring: normal, unusual and bad

Every strip in Monitoring → **4. Live monitoring** is coloured by how its
reading compares with a healthy flight:

| level | colour | mark | meaning |
|---|---|---|---|
| normal | green | ● | what September–October 2026 flights looked like when nothing was wrong |
| unusual | amber | ▲ | worth a look; not yet a problem for the flight |
| bad | red | ✖ | act on it |

The level is drawn three ways at once — the line's colour, the stripe down the
strip's left edge, and the mark and word under the current value — so it never
rests on colour alone (sunlight on a rugged screen and colour-vision
deficiency both take colour first). Dashed lines are the thresholds, drawn
when the data comes near them. Point at a reading's name for the reason.

The judgements live in `rov_flight_ops/bands.py`; this table is generated
from it. They apply to real flights and to the ROV flight simulator alike.

## Where the numbers come from

**Normal is measured.** Each band was checked against the 27
`laptop_monitor_*.csv` logs recorded on L342-D between 11 September and
15 October 2026 — six Port of Seattle survey days and the OTS test flights,
39,842 rows. The *normal on real flights* column is the share of those rows
the band calls normal. Most readings are normal 97–100 % of the time; where a
reading is not, the logs say why:

* `disk_free_gb` / `disk_free_pct` are amber on about half of all rows,
  because L342-D really was short of disk all September (10–20 GB free of
  237 GB; it was full on 1 October).
* `rov_packet_loss_pct` is amber or red on about 9 % of rows: the vehicle was
  unreachable for up to 229 s on 11 September and dropped in and out at
  EBM_C on 18 September.
* `system_uptime_s` is amber on about half: the laptop ran up to 28 days
  between restarts.

**Unusual and bad are mostly judgement**, because the logs hold few failures
of most kinds. They come from what the hardware is documented to do — Intel's
100 °C throttle point, Windows' memory manager, the Raspberry Pi's 80 °C soft
limit, the Fathom-X's negotiated rates, Electron's habit of leaking handles —
and from the failures the logs do contain (the Pi above 70 °C for ten minutes
at EBM_W on 17 September; the CPU's frequency ceiling falling to 21 % at Grain
Elevator on 15 September).

## How a reading is judged

* **Smoothed where it is noisy.** A CPU at 100 % for one second is a busy
  second, not a fault, so most rates are judged on their mean over a few
  seconds (*judged on*). The line itself is drawn raw.
* **Counters by how they move.** A cumulative count — bytes, discarded
  packets, carrier drops — is judged by whether it climbs when it should not,
  or stops when it should be climbing. A count that goes *down* is always
  bad: its owner restarted.
* **Some only in context.** A slow clock on an idle laptop is the power plan
  doing its job, so `cpu_frequency_mhz` is judged only while the CPU is ≥ 50 %
  busy; little video arriving is a fault only while Cockpit is running.
* **A blank is not judged.** No value, no colour.

## The bands

### CPU

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `cpu_usage_pct` | ▲ ≥ 80 %  ✖ ≥ 95 % | 5 s mean | 97.5 % | Cockpit's video and the recorders together run at 30-50 % on L342-D (95th percentile 76 %). Held above 80 % the window starts to lag; above 95 % video frames are dropped and the 1 Hz row slips. |
| `cpu_max_core_usage_pct` | ▲ ≥ 92 %  ✖ ≥ 99 % | 10 s mean | 98.8 % | One core pinned for ten seconds is a single-threaded job that cannot go faster -- usually a stuck renderer -- even while the average looks fine. |
| `cpu_frequency_mhz` | ▲ ≤ 1600  ✖ ≤ 1000 (while busy) | 5 s mean, in context | 99.7 % | Judged only while the CPU is busy (≥ 50 %): an idle laptop clocks down on purpose. Busy and under 1.6 GHz is a laptop being held back; under 1 GHz is thermal or power throttling (the lowest seen on a survey was 981 MHz). |
| `cpu_max_freq_ceiling_pct` | ▲ ≤ 80 %  ✖ ≤ 50 % | 3 s mean | 99.7 % | The highest clock Windows will currently allow. L342-D's power plan holds it at 89 %; a drop is firmware or Windows capping the processor for heat or power. It fell to 21 % for 17 s at Grain Elevator on 15 September. |
| `cpu_package_temp_c` | ▲ ≥ 80 C  ✖ ≥ 90 C | 5 s mean | not published on L342-D | Tiger Lake throttles at 100 °C. A laptop running video sits at 60-75 °C; 80 °C is a hot case, 90 °C is minutes from throttling. |
| `cpu_max_core_temp_c` | ▲ ≥ 85 C  ✖ ≥ 95 C | 5 s mean | not published on L342-D | The hottest core, against the same 100 °C limit. 95 °C means the clock is about to be pulled down. |
| `cpu_package_power_w` | ▲ ≥ 28 W  ✖ ≥ 45 W | 10 s mean | not published on L342-D | Above 28 W the package is past its long-term limit and running on turbo budget; held near 45 W it is shedding heat faster than a closed rugged case can. |

### Memory

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `ram_used_gb` | ▲ ≥ 70 %  ✖ ≥ 85 % of RAM | 5 s mean, derived | 100.0 % | Judged as a share of installed memory. Surveys use 30-45 % of L342-D's 32 GB; past 85 % Windows starts trimming programs and compressing memory. |
| `ram_available_gb` | ▲ ≤ 4 GB  ✖ ≤ 2 GB | 5 s mean | 100.0 % | Below 4 GB free the next large allocation competes for memory; below 2 GB Windows signals low memory and paging begins. |
| `ram_usage_pct` | ▲ ≥ 70 %  ✖ ≥ 85 % | 5 s mean | 100.0 % | The same judgement as ram_used_gb: 30-45 % is a normal survey day; 85 % is memory pressure. |
| `memory_committed_gb` | ▲ ≥ 70 %  ✖ ≥ 90 % of limit | 5 s mean, derived | 100.0 % | Judged against the commit limit. At 100 % allocations fail and programs crash; Windows warns from about 90 %. Surveys peak under 50 %. |
| `memory_commit_limit_gb` | coloured by commit ÷ limit | 5 s mean, derived | 100.0 % | Shown in the colour of how full it is. A step up in the limit itself means Windows grew the page file because commit was running out. |
| `pagefile_usage_pct` | ▲ ≥ 10 %  ✖ ≥ 25 % | each sample | 100.0 % | Surveys use 0-6 % of the page file. A quarter of it in use means memory has been pushed to disk and reading it back is slow. |
| `pages_per_s` | ▲ ≥ 300 pages/s  ✖ ≥ 1500 pages/s | 10 s mean | 96.9 % | Hard page faults. Single spikes into the thousands are normal (a program starting); held above 300/s for ten seconds is paging, above 1,500/s the disk is being thrashed. |

### GPU

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `gpu_usage_pct` | ▲ ≥ 75 %  ✖ ≥ 90 % | 5 s mean | 99.5 % | Iris Xe draws Cockpit's video at 35-60 %. Held above 90 % it cannot keep up and frames are dropped or late. |
| `gpu_video_decode_pct` | ▲ ≥ 40 %  ✖ ≥ 75 % | 5 s mean | 100.0 % | One 1080p H.264 stream costs about 7 % of the decoder. A second stream or a codec without hardware support pushes it up; held above 75 % the video stutters. |
| `gpu_memory_used_mb` | ▲ ≥ 1500 MB  ✖ ≥ 3000 MB | 5 s mean | 100.0 % | Surveys use 180-680 MB of shared graphics memory. Climbing past 1.5 GB through a flight is a leak, usually in the video client. |
| `gpu_memory_usage_pct` | ▲ ≥ 10 %  ✖ ≥ 20 % | 5 s mean | 100.0 % | Of the half of RAM Windows lends an integrated GPU. Surveys use 1-4 %. |
| `gpu_temp_c` | ▲ ≥ 80 C  ✖ ≥ 90 C | 5 s mean | not published on L342-D | Integrated graphics share the CPU's die and its 100 °C limit. |
| `gpu_power_w` | ▲ ≥ 12 W  ✖ ≥ 18 W | 10 s mean | not published on L342-D | An integrated GPU decoding one stream draws a few watts; sustained above 12 W it is rendering far more than video. |

### Storage

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `disk_active_pct` | ▲ ≥ 50 %  ✖ ≥ 85 % | 10 s mean | 100.0 % | Surveys keep the disk 1-3 % busy. Busy past 85 % for ten seconds, writes queue and the recorders' syncs stall. |
| `disk_read_mb_s` | ▲ ≥ 50 MB/s  ✖ ≥ 300 MB/s | 10 s mean | 100.0 % | Flights read almost nothing. Sustained reads are another job -- an antivirus scan, indexing, a sync client -- competing with the recorders. |
| `disk_write_mb_s` | ▲ ≥ 50 MB/s  ✖ ≥ 200 MB/s | 10 s mean | 99.8 % | The recorders write well under 1 MB/s. Sustained heavy writes are something else -- an update, a sync, a video copy -- and fill the disk quickly: 200 MB/s is 12 GB a minute. |
| `disk_queue_length` | ▲ ≥ 2  ✖ ≥ 8 | 5 s mean | 99.9 % | Requests waiting for the disk. Surveys sit at 0; held above 2 the disk is behind, above 8 it has stopped keeping up. |
| `disk_free_gb` | ▲ ≤ 30 GB  ✖ ≤ 10 GB | each sample | 45.7 % | A survey day downloads tens of GB of recordings and video. Under 30 GB a day's downloads may not fit; under 10 GB Windows and the recorders run short (the DVL capture stops its snapshots under 2 GiB). L342-D was down to 10 GB on 18 September. |
| `disk_free_pct` | ▲ ≤ 10 %  ✖ ≤ 5 % | each sample | 45.7 % | The same, as a share: SSDs slow down and wear faster with less than 5-10 % free. L342-D spent most of September between 5 and 10 %, so this strip was amber on most survey days -- rightly. |
| `ssd_temp_c` | ▲ ≥ 60 C  ✖ ≥ 70 C | 10 s mean | not published on L342-D | Consumer SSDs throttle their writes from about 70 °C. |

### Network

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `network_receive_mbps` | ▲ ≥ 40  ✖ ≥ 70 · ✖ ≤ 2 with Cockpit up | 5 s mean, in context | 98.3 % | The camera stream arrives at 6-18 Mbps. Held above 70 Mbps the tether, which carries 80-130 Mbps at best, is nearly full and the video will stall. While Cockpit is running, under 2 Mbps arriving means the video stream has stopped reaching the laptop. |
| `network_send_mbps` | ▲ ≥ 5 Mbps  ✖ ≥ 20 Mbps | 5 s mean | 100.0 % | The laptop sends the vehicle almost nothing (0.1-1 Mbps of MAVLink and requests). Megabits going up the tether are an upload or an update competing with the video coming down. |
| `network_utilization_pct` | ▲ ≥ 50 %  ✖ ≥ 80 % | 5 s mean | 99.8 % | Of the adapter's link speed. Surveys use 11-19 %. |
| `rov_ping_latency_ms` | ▲ ≥ 25 ms  ✖ ≥ 100 ms | 3 s mean | 99.9 % | Round trip to the Pi over the tether: 3-6 ms normally, 19 ms at the 99th percentile. Past 25 ms the link is congested or retrying; past 100 ms control and video both lag. |
| `rov_packet_loss_pct` | ▲ ≥ 5 %  ✖ ≥ 20 % | each sample | 91.1 % | Lost pings to the Pi, over the last 30. One lost (3.3 %) is common and means nothing; two or more is a link problem, and at 20 % the vehicle is dropping in and out. |
| `network_packets_discarded` | ✖ climbing (in 10 s) | change over 10 s | 100.0 % | A count since the adapter started, normally flat all day. Any rise during a flight means the laptop is throwing away packets it received -- a driver or buffer problem. |
| `rov_http_ms` | ▲ ≥ 250 ms  ✖ ≥ 1000 ms | 5 s mean | 96.9 % | How long the Pi takes to answer a small request: 40-160 ms normally. Over a second it is overloaded or the tether is struggling, and the recorder's arm checks start to fail. |

### Cockpit

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `cockpit_cpu_pct` | ▲ ≥ 45 %  ✖ ≥ 70 % | 10 s mean | 100.0 % | Of the whole CPU. Cockpit uses 15-37 % on L342-D; held above 70 % a renderer is spinning. |
| `cockpit_ram_mb` | ▲ ≥ 2500 MB  ✖ ≥ 4000 MB | 5 s mean | 100.0 % | Cockpit holds 0.9-1.6 GB on a survey (most ever 2.1 GB). Electron apps leak; past 4 GB a restart between transects is cheaper than a crash during one. |
| `cockpit_gpu_usage_pct` | ▲ ≥ 60 %  ✖ ≥ 85 % | 5 s mean | 98.6 % | Cockpit's share of the GPU, normally 15-45 %. |
| `cockpit_process_count` | ▲ ≤ 3  ✖ ≤ 2 while running · ▲ ≥ 10 | in context | 99.9 % | Cockpit runs as four processes. One fewer usually means its GPU or renderer process crashed, which freezes or blanks the video while the window stays open. More than ten means extra windows or a respawning renderer. |
| `cockpit_handle_count` | ▲ ≥ 5000  ✖ ≥ 10k | each sample | 100.0 % | Steady at about 2,800. A count that climbs all flight is a leak; past 10,000 the process is in trouble. |
| `cockpit_thread_count` | ▲ ≥ 200  ✖ ≥ 400 | each sample | 100.0 % | Steady at about 110. |

### Power

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `battery_charge_pct` | ▲ ≤ 40 %  ✖ ≤ 20 % | each sample | 100.0 % | On the boat the laptop is meant to be on mains. Under 40 % there is perhaps an hour left on battery; under 20 % Windows will start saving power at the expense of the video. |
| `battery_discharge_w` | ▲ ≥ 35  ✖ ≥ 45 · ✖ ≥ 10 on mains | 10 s mean, in context | 99.0 % | On battery, L342-D draws 20-38 W flying; past 45 W the battery is being emptied in about an hour. Any draw from the battery while plugged in means the charger cannot keep up -- an underpowered adapter or a failing inverter. |
| `battery_temp_c` | ▲ ≥ 40 C  ✖ ≥ 50 C | 10 s mean | not published on L342-D | Lithium cells stop charging around 45 °C and age fast above it; a laptop in the sun on deck gets there. |
| `fan_speed_rpm` | ▲ ≥ 4500 rpm  ✖ ≥ 5800 rpm | 10 s mean | not published on L342-D | A fan held at its maximum is cooling at its limit; the next step is throttling. |
| `motherboard_temp_c` | ▲ ≥ 55 C  ✖ ≥ 70 C | 10 s mean | 100.0 % | The chassis zone. It rises with the sun on the case. (On L342-D this sensor reads a constant 25.1 °C.) |
| `pi_soc_temp_c` | ▲ ≥ 70 C  ✖ ≥ 80 C | 10 s mean | 98.2 % | The Raspberry Pi in the vehicle: 55-65 °C on a survey. It slows its own processor at 80 °C, which starves video and telemetry. It sat above 70 °C for ten minutes at EBM_W on 17 September. |
| `system_uptime_s` | ▲ ≥ 7 days · ▲ < 10 min | each sample | 51.9 % | A laptop that has not restarted in a week carries pending updates and slowly leaking drivers into the field. Worth a restart the evening before; never an emergency. L342-D has run 28 days without one. Up for less than ten minutes in the middle of a day means it restarted -- planned or not. |

### Tether

| reading | thresholds (▲ unusual, ✖ bad) | judged on | normal on real flights | why |
|---|---|---|---|---|
| `nic_carrier` | ✖ FALSE | each sample | 100.0 % | The interface that routes to the vehicle has no link. |
| `phy_carrier` | ✖ FALSE with the vehicle gone | in context | 99.1 % | No link on the cable while the vehicle is also not answering: the tether is unplugged or broken. FALSE on its own is not judged -- on L342-D the watched adapter is often the bridge's other port, and was FALSE two thirds of the time on healthy flights. |
| `nic_low_power` | ✖ TRUE | each sample | 100.0 % | Windows has put the tether adapter into a low-power state, which drops the link. |
| `phy_rx_bytes` | ✖ stops climbing (10 s) | change over 10 s | 100.0 % | Bytes arriving on the cable. It should climb every second the video runs; flat means nothing is arriving at the laptop at all. |
| `rov_arp_ok` | ✖ FALSE | each sample | 99.4 % | Layer 2 cannot find the vehicle's address: it is not on the wire. |
| `pi_eth_rx_bytes` | ✖ stops climbing (30 s) | change over 30 s | 99.5 % | What the Pi has received, read every 5 s. Flat, or reset to a smaller number, is the Pi's own port going quiet or the Pi restarting -- the 15 October outages. |
| `pi_eth_carrier_drops` | ✖ climbing (in 30 s) | change over 30 s | not published on L342-D | How many times the Pi's port has lost its link since boot. A step during a flight is the cable or port physically dropping. |
| `tether_link_mbps` | ▲ ≤ 80 Mbps  ✖ ≤ 40 Mbps | each sample | 100.0 % | The Fathom-X pair's negotiated rate: 104-132 Mbps on surveys. Under 80 the tether is noisy or wet at a connector; under 40 video will not fit. |
| `tether_tx_mbps` | ▲ ≤ 80  ✖ ≤ 40 · ▲ ≥ 20 apart | derived | 100.0 % | The same, one direction. The two directions more than 20 Mbps apart usually means a wire disconnected rather than a noisy line. |
| `tether_rx_mbps` | ▲ ≤ 80  ✖ ≤ 40 · ▲ ≥ 20 apart | derived | 100.0 % | The same, the other direction. As for tether_tx_mbps. |
| `tether_remote_seen` | ✖ FALSE | each sample | 100.0 % | The topside Fathom-X cannot hear the vehicle's at all. |
