# CCR ROV Survey Methods  

## 🌊 Overview  

This repository provides an overview of the **Seattle Aquarium’s Coastal Climate Resilience (CCR) team’s ROV survey workflow** — from hardware configuration to data extraction and visualization. It includes code and documentation for processing and organizing ROV telemetry data collected during benthic habitat surveys.  

Our goal is to create an **open-source reference** that allows other research groups to understand, replicate, and adapt our methods for their own underwater survey applications.  

---

## ⚙️ Hardware  

### 🤖 ROV Platform  
- [**BlueROV2**](https://bluerobotics.com/store/rov/bluerov2/) (Blue Robotics) — Heavy Configuration with 150 m tether  
- **Navigator Flight Controller** (with Raspberry Pi 4, 8 GB Model B)  
- **Modifications:** Custom “kelp guards” fabricated from heavy plastic to minimize entanglement with kelp stipes during surveys  

### ⚡ Power and Tether Management  
- [**Outland Technology Power Supply (OTPS-1kW)**](https://bluerobotics.com/store/comm-control-power/powersupplies-batteries/otps1kw/)  
- [**Outland Technology Tether Reel (RL-750-2)**](https://www.outlandtech.com/resources/rovtetherresources)  

### 💡 Lighting  
- 4 × [**Kraken Solar Flare Mini 18,000**](https://krakensports.ca/product/solar-flare-mini-18000/) video lights  
- 4 × [**Ultralight AD-1420-IK**](https://ulcs.com/product/ad-1420-ik-base-adapter/?srsltid=AfmBOoqhjfKVo0aI3aMI6ZeeyEz2tsy--UoIa0qY0KNQkLEsrkHGSEGb) universal ball adapters mounted to ROV frame  
- Custom light mounting hardware  

### 📡 Additional Sensors  
- **Water Linked DVL A50** — Doppler Velocity Log for sub-meter scale positioning  
  - Firmware: v1.0.8 (updated 2025-07-24)  

### 🎥 Cameras  
- [**GoPro Hero 12 / 13 Black**](https://gopro.com/en/us/shop/cameras/buy/hero13black/CHDHX-131-master.html)  
- [**Protective Housing**](https://gopro.com/en/us/shop/mounts-accessories/protective-housing-plus-waterproof-case/ADDIV-001.html)  
- [**GoPro Labs**](https://gopro.com/en/bn/info/gopro-labs) firmware for time synchronization and advanced scripting  

**Camera configuration (for still-image transects):**  

| Setting | Value |
|----------|--------|
| Mode | Photo |
| Interval | 3 s |
| Lens | Wide |
| Output | RAW (.GPR) |
| EV Comp | 0 |
| White Balance | Native |
| ISO Min–Max | 100–200 |
| Sharpness | Low |
| Color | Flat |
| Shutter Speed | 1/300 s (`!MEXPX=300` via GoPro Labs) |

**Time synchronization:** [GoPro Precision Time](https://gopro.github.io/labs/control/precisiontime/)  

---

## 🗺️ Positioning and Navigation  

- [**Water Linked Underwater GPS G2 Standard Kit**](https://www.waterlinked.com/shop/underwater-gps-g2-standard-kit-132)  
  - Components: G2 Topside, Locator U1, Antenna  
  - Firmware: v3.3.4 (updated 2025-08-15)  

- [**Advanced Navigation Satellite Compass**](https://landing.advancednavigation.com/inertial-navigation-systems/satellite-compass/gnss-compass/)  
  - Firmware: v2.47 (updated 2025-08-08)  

- **NMEA network integration** for synchronized positioning across all sensors  

---

## 💻 Command Console  

- Custom **Pelican case** housing the surface control system  
- Ruggedized **laptop** for mission control (BlueOS / QGroundControl)  
- **19-inch sunlight-readable monitor** ([MS190W1610NT](https://www.lcdpart.com/products/ms190w1610nt-19-inch-sunlight-readable-open-frame-monitor-1200-nits))  
- **Ethernet switch** for network connectivity between ROV, GPS, DVL, and camera control systems  

---

## 🧠 Firmware and Software  

### BlueOS Configuration  
- **ArduSub:** v4.5.3 (updated 2025-07-24)  
- [**DVL Extension**](https://github.com/bluerobotics/BlueOS-Water-Linked-DVL): v1.0.8  
- [**UGPS Extension**](https://github.com/waterlinked/blueos-ugps-extension): v1.0.7  
  - Modified configuration with `EXTRA_ARGS=--ignore_gps` for improved sensor fusion  
- [**Surftrack Fixit**](https://github.com/clydemcqueen/surftrak_fixit): v1.0.0-beta.2  
- [**Water Linked External UGPS Extension**](https://github.com/clydemcqueen/wl_ugps_external_extension)  
  - Provides external (vessel) position and heading data to the Water Linked UGPS system  

---

## 🧮 Telemetry Processing  


### Code 
* `tlog_csv_no_EKF.py`: This script processes telemetry `.tlog` files from BlueOS when there is no fusion between the GPS and Doppler Velocity Log (DVL). It extracts relevant fields (e.g., time, date, GPS latitude/longitude, DVLx, DVLy (`LOCAL_POSITION_NED`), altitude, depth, heading) and averages values per second. Additionally, it calculates DVL-based latitude and longitude (`DVLlat`, `DVLlon`) from DVLx and DVLy movements and estimates the width (m) and area (m²) captured by GoPro images based on the ROV's altitude. If the survey start and end times are known, they can be specified when running the script; otherwise, the entire `.tlog` file will be processed.

* `tlog_to_csv_EKF.py`: This script processes `.tlog` files when GPS and DVL data are fused via an Extended Kalman Filter (EKF), producing more accurate tracks than using GPS or DVL alone. Instead of calculating `DVLlat`/`DVLlon`, this script incorporates the fused position data (`GLOBAL_POSITION_INT`) for improved accuracy.
<p align="center">
  <img src="figures/survey_params.png" width="600", height="200" /> 
</p>

* `transect_map.py`: This script generates a Leaflet map displaying the ROV tracks as measured by different navigation sources: GPS (black), DVL (blue), and EKF (red), which can then be incorporated into broader maps, as depicted below for the Urban Kelp Research Project with the Port of Seattle 
<p align="center">
  <img src="figures/GPS_EKF_tracks.jpg" width="300", height="300" /> 
</p>

<p align="center">
  <img src="figures/Port_2425_map.png" width="450", height="600"/>
</p>

---
## General information; workflows ready to implement
The following repos contain general information about our work, and specialized repos for ROV telemetry analyses, processing and analyses of ROV-derived benthic abundance and distribution data.

```mermaid
graph TD

A["<a href='https://github.com/Seattle-Aquarium/Coastal_Climate_Resilience' target='_blank' style='font-size: 16px; font-weight: bold;'>Coastal_Climate_Resilience</a><br><font color='darkgray'>the main landing pad for the CCR research program</font>"]

A --> E["<a href='https://github.com/Seattle-Aquarium/CCR_analytical_resources' target='_blank' style='font-size: 16px; font-weight: bold;'>CCR_ROV_telemetry_processing</a><br><font color='darkgray'>analytical tools for working with ROV telemetry data</font>"]

A --> F["<a href='https://github.com/Seattle-Aquarium/CCR_benthic_analyses' target='_blank' style='font-size: 16px; font-weight: bold;'>CCR_benthic_analyses</a><br><font color='darkgray'>code to work with ROV-derived benthic community data</font>"]


```



## Help wanted! 
The following repos involve active areas of open-source software development, AI/ML implementation, and computer vision challenges; areas where we could use assistance are 🔶 highlighted in orange 🔶

```mermaid
graph TD

B["<a href='https://github.com/Seattle-Aquarium/CCR_development' target='_blank' style='font-size: 16px; font-weight: bold;'>CCR_development</a><br><font color='darkgray'>main hub for organizing active Issues under development </font>"]

B --> C["<a href='https://github.com/Seattle-Aquarium/CCR_image_processing' target='_blank' style='font-size: 16px; font-weight: bold;'>CCR_image_processing</a><br><font color='darkgray'>help wanted to implement AI/ML solution to expendite image processing</font>"]

B --> D["<a href='https://github.com/Seattle-Aquarium/CCR_kelp_feature_detection' target='_blank' style='font-size: 16px; font-weight: bold;'>CCR_kelp_feature_detection</a><br><font color='darkgray'>active research re: photogrammetry in kelp forests</font>"]

style B stroke:#FF8600,stroke-width:4px
style C stroke:#FF8600,stroke-width:4px
```

