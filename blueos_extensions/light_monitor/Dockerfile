# BlueOS Light Monitor -- the Lutris DeepSea lights, live, recorded and in MAVLink.
#
# Built for the Pi by .github/workflows/blueos-light-monitor.yml (no Docker is
# needed on a laptop) and pushed to Docker Hub, which is where BlueOS installs
# extensions from. Install it on the vehicle from BlueOS -> Extensions ->
# Installed -> "+" -- see README.md.
#
# python:3.11-slim is published for linux/arm/v7 (Pi 3 and the 32-bit BlueOS
# images) and linux/arm64/v8 (Pi 4/5 on 64-bit), so one tag serves both.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    LIGHT_MONITOR_PORT=9420 \
    LIGHT_MONITOR_DATA=/data \
    LIGHT_MONITOR_MODE=auto \
    MAVLINK2REST_URL=http://127.0.0.1:6040

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY light_monitor /app/light_monitor

# --- What BlueOS reads ----------------------------------------------------
#
# permissions -- passed to `docker create`:
#   Privileged + /dev:/dev  the light controller's USB serial ports, including
#                           when it is unplugged and comes back. BlueOS's own
#                           documented pattern for serial devices.
#   NetworkMode host        mavlink2rest at 127.0.0.1:6040 on BlueOS 1.4 and
#                           1.5 alike, and this page on a fixed port (9420)
#                           that rov_flight_ops can find.
#   the data bind           the recording, on the Pi, where BlueOS's File
#                           Browser shows it (userdata/light_monitor) and where
#                           it survives updates of the extension.
LABEL version="0.1.0"
LABEL permissions='{\
  "HostConfig": {\
    "Privileged": true,\
    "NetworkMode": "host",\
    "Binds": [\
      "/dev:/dev",\
      "/usr/blueos/userdata/light_monitor:/data"\
    ]\
  }\
}'
LABEL type="device-integration"
LABEL tags='["lights", "lighting", "telemetry", "logging", "deepsea"]'
LABEL authors='[{"name": "Zachary Randell", "email": ""}]'
LABEL company='{"about": "Coastal Climate Resilience program", "name": "Seattle Aquarium", "email": ""}'
LABEL readme="https://raw.githubusercontent.com/Seattle-Aquarium/CCR_ROV_survey_methods/main/blueos_extensions/light_monitor/README.md"
LABEL links='{\
  "source": "https://github.com/Seattle-Aquarium/CCR_ROV_survey_methods/tree/main/blueos_extensions/light_monitor",\
  "support": "https://github.com/Seattle-Aquarium/CCR_development/issues"\
}'
LABEL requirements="core >= 1.1"

CMD ["python", "-m", "light_monitor"]
