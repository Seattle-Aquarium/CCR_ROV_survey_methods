# BlueOS extensions

Extensions this program runs on the ROVs' Raspberry Pis. Each folder is one
extension: its source, its `Dockerfile` (with the labels BlueOS reads), its
tests and its README. Images are built for the Pi by GitHub Actions and pushed
to Docker Hub, which is where BlueOS installs extensions from — no Docker is
needed on a laptop.

| folder | what it does |
|---|---|
| [`light_monitor/`](light_monitor/) | the four DeepSea lights on Lutris and their controller: live page, recording on the Pi, summary into MAVLink (tlog / mcap), and the API rov_flight_ops' Lights tab reads |

They live here rather than in repositories of their own because each is tied
to code in this one — the Light Monitor reads the telemetry format the light
controller's firmware (`lighting/code`) writes, and rov_flight_ops reads the
Light Monitor — so a change to one side is reviewed with the other.
