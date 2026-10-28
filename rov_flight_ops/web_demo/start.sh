#!/usr/bin/env bash
# The web demo's container: a virtual display, the program on it in demo mode,
# and noVNC serving that display on port 7860.
#
# One display, so one desktop: everyone who opens the page sees and drives the
# same window. The program and the VNC server are each started again if they
# ever exit, so a visitor who manages to close the window gets it back.
set -u

SCREEN="${SCREEN:-1500x950x24}"
DISPLAY="${DISPLAY:-:1}"
export DISPLAY

Xvfb "$DISPLAY" -screen 0 "$SCREEN" -dpi 96 -nolisten tcp &

# Until the display's socket is there; the program cannot start without it.
socket="/tmp/.X11-unix/X${DISPLAY#:}"
for _ in $(seq 100); do
    [ -S "$socket" ] && break
    sleep 0.1
done

# "Starting", for whoever connects before the program's window is up. Opened
# first, so the program's window opens over it.
python web_demo/splash.py &

(
    while true; do
        python -m rov_flight_ops --demo
        echo "start.sh: ROV Flight Operations exited ($?); starting it again" >&2
        sleep 3
    done
) &

(
    while true; do
        x11vnc -display "$DISPLAY" -rfbport 5900 -localhost -noipv6 \
            -shared -forever -nopw -xkb -quiet \
            -desktop "ROV Flight Operations (simulator)"
        echo "start.sh: x11vnc exited ($?); starting it again" >&2
        sleep 1
    done
) &

# The page and the websocket on one port. 127.0.0.1, not localhost: websockify
# tries only the first address a name resolves to, and x11vnc is not on ::1.
exec websockify --web /opt/novnc --file-only --heartbeat 30 \
    7860 127.0.0.1:5900
