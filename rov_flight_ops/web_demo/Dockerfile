# ROV Flight Operations running its ROV flight simulator, in a browser.
#
# The real CustomTkinter program on a virtual X display (Xvfb), shared by
# x11vnc and served to the browser by noVNC through websockify on port 7860 --
# the port a Hugging Face Docker Space exposes. Nothing here is a web rewrite
# of the GUI; it is the same window an operator sees on the field laptop.
#
# Built by Hugging Face from the Space repository that web_demo/deploy.py
# assembles, whose root holds this file and the rov_flight_ops/ project
# folder. The same layout is the GitHub repository's root, so it also builds
# locally from there:
#     docker build -f rov_flight_ops/web_demo/Dockerfile -t rov-flight-ops-demo .
#     docker run --rm -p 7860:7860 rov-flight-ops-demo
FROM python:3.13-slim-trixie

ARG NOVNC_VERSION=1.6.0
ARG NOVNC_SHA256=5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa

# Tk (the slim image builds Python's _tkinter but drops the library), the
# virtual display and VNC server, and fonts: DejaVu for the lamps and marks
# (Segoe UI Symbol's stand-in), Symbola for the plan panel's hand, eye and
# lock, Droid Sans Fallback for its full-width plus. No colour emoji font --
# Tk on X11 cannot draw one and can fail outright trying.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tk xvfb xauth x11vnc \
        fonts-dejavu-core fonts-symbola fonts-droid-fallback fontconfig \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# noVNC, pinned by checksum; websockify comes from PyPI below.
RUN curl -fsSL -o /tmp/novnc.tgz \
        "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
    && echo "${NOVNC_SHA256}  /tmp/novnc.tgz" | sha256sum -c - \
    && mkdir -p /opt/novnc \
    && tar -xzf /tmp/novnc.tgz -C /opt/novnc --strip-components=1 \
    && rm /tmp/novnc.tgz

# The program's dependencies at the versions the field laptops run.
COPY rov_flight_ops/requirements.txt rov_flight_ops/constraints.txt /tmp/req/
RUN pip install --no-cache-dir -r /tmp/req/requirements.txt -c /tmp/req/constraints.txt \
    && pip install --no-cache-dir websockify==0.13.0

# Hugging Face runs the container as uid 1000. Xvfb run by anyone but root
# needs the X socket folder to exist already.
RUN useradd -m -u 1000 user \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix \
    && mkdir -p /opt/novnc/selfcheck && chown user /opt/novnc/selfcheck

COPY rov_flight_ops/web_demo/web/ /opt/novnc/
COPY rov_flight_ops/web_demo/montserrat.conf /etc/fonts/conf.d/50-montserrat.conf

USER user
ENV HOME=/home/user \
    LOCALAPPDATA=/home/user/.local/share \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:1 \
    SCREEN=1500x950x24 \
    ROV_FLIGHT_OPS_DEMO=1 \
    ROV_FLIGHT_OPS_DEMO_RESTART_MIN=60
WORKDIR /home/user/app
COPY --chown=user rov_flight_ops/ /home/user/app/

# Fails the build if any module will not import here, and runs the program
# once on a throwaway display: every tab opened and photographed into
# /opt/novnc/selfcheck/, which the running Space then serves.
RUN xvfb-run -a -s "-screen 0 ${SCREEN} -dpi 96" \
        python web_demo/selfcheck.py /opt/novnc/selfcheck \
    && rm -rf /home/user/.local/share/CCR_ROV

EXPOSE 7860
CMD ["bash", "web_demo/start.sh"]
