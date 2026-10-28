"""
Put the web demo on Fly.io: assemble the build folder and deploy it.

    python web_demo/deploy.py                  # to the app named in fly.toml
    python web_demo/deploy.py --app NAME
    python web_demo/deploy.py --stage DIR      # assemble it into DIR, deploy nothing

Run from ``rov_flight_ops/``, with Fly's command-line tool installed
(``flyctl``, https://fly.io/docs/flyctl/install/) and this computer logged in
to the Fly account that pays for the demo (``fly auth login``) -- or, as
GitHub runs it (``.github/workflows/web-demo.yml``), with a Fly token in
``FLY_API_TOKEN`` instead of the login. The first run creates the app. Every run builds the container on Fly's own builders --
nothing is built here, so Docker is not needed -- and replaces the running
machine with one made from the new build. A build takes several minutes.

What is built: this folder's Dockerfile and fly.toml at the root, the
repository's LICENSE, and the parts of the project the container needs under
``rov_flight_ops/`` -- the same layout as the GitHub repository, so the
Dockerfile builds from either.

The demo was on Hugging Face first, which paused it as abusive because its
content policy counts VNC as platform abuse: see "The web demo" in the README.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
REPO = PROJECT.parent

#: What of the project the container needs.
INCLUDE = ("rov_flight_ops", "assets", "vehicle_parameters", "web_demo",
           "pyproject.toml", "requirements.txt", "constraints.txt")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache")
#: Read by bash or Docker in the container, where a CR breaks them.
LF_ONLY = ("*.sh", "Dockerfile")


def stage(into: Path) -> Path:
    """Assemble the build folder in `into` (emptied first)."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    shutil.copy2(HERE / "Dockerfile", into / "Dockerfile")
    shutil.copy2(HERE / "fly.toml", into / "fly.toml")
    if (REPO / "LICENSE").is_file():
        shutil.copy2(REPO / "LICENSE", into / "LICENSE")
    project = into / "rov_flight_ops"
    for name in INCLUDE:
        src = PROJECT / name
        if src.is_dir():
            shutil.copytree(src, project / name, ignore=SKIP)
        else:
            project.mkdir(exist_ok=True)
            shutil.copy2(src, project / name)
    for pattern in LF_ONLY:
        for path in into.rglob(pattern):
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
    return into


def commit() -> str:
    """The checked-out commit, and whether the tree has changes beyond it."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=PROJECT, capture_output=True,
                              text=True).stdout.strip()
    head = git("rev-parse", "--short", "HEAD") or "unknown commit"
    return head + (" with local changes" if git("status", "--porcelain", ".") else "")


def flyctl() -> str | None:
    """Fly's command-line tool: on the PATH, or where its installer puts it."""
    found = shutil.which("fly") or shutil.which("flyctl")
    if found:
        return found
    exe = "flyctl.exe" if sys.platform == "win32" else "flyctl"
    installed = Path.home() / ".fly" / "bin" / exe
    return str(installed) if installed.is_file() else None


def app_in_fly_toml() -> str:
    """The app fly.toml names (read by hand: tomllib needs Python 3.11)."""
    text = (HERE / "fly.toml").read_text(encoding="utf-8")
    return re.search(r'^app\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)


def main(argv: list[str] | None = None) -> int:
    default_app = app_in_fly_toml()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--app", default=default_app,
                    help=f"the Fly app to deploy to (default {default_app})")
    ap.add_argument("--org", default="personal",
                    help="the Fly organisation a new app is made in "
                         "(default personal: the account's own)")
    ap.add_argument("--stage", type=Path, metavar="DIR",
                    help="assemble the build folder into DIR and stop")
    args = ap.parse_args(argv)

    if args.stage:
        print(f"Assembled the build folder in {stage(args.stage)}")
        return 0

    fly = flyctl()
    if fly is None:
        print("Needs Fly's command-line tool, flyctl: "
              "https://fly.io/docs/flyctl/install/", file=sys.stderr)
        return 2
    # On GitHub (.github/workflows/web-demo.yml) a token stands in for a login,
    # and a token for an organisation cannot say whose it is.
    if not os.environ.get("FLY_API_TOKEN"):
        who = subprocess.run([fly, "auth", "whoami"], capture_output=True,
                             text=True)
        if who.returncode != 0:
            print("Not logged in to Fly. Run  fly auth login  first -- or, on "
                  "GitHub, give the repository its FLY_API_TOKEN secret.",
                  file=sys.stderr)
            return 2
        print(f"Logged in to Fly as {who.stdout.strip()}.")
    if subprocess.run([fly, "status", "--app", args.app],
                      capture_output=True).returncode != 0:
        print(f"Creating the Fly app {args.app} in {args.org}.", flush=True)
        if subprocess.run([fly, "apps", "create", args.app,
                           "--org", args.org]).returncode != 0:
            print(f"Fly would not create {args.app}; if the name is taken, "
                  f"choose another with --app (and in fly.toml).", file=sys.stderr)
            return 1
    made = commit()
    with tempfile.TemporaryDirectory(prefix="rov_fly_") as tmp:
        folder = stage(Path(tmp) / "build")
        # One machine: it is one shared desktop, and a second would be a
        # second, separate demo that Fly bills for as well.
        done = subprocess.run([fly, "deploy", str(folder), "--app", args.app,
                               "--remote-only", "--ha=false"])
    if done.returncode != 0:
        print(f"The deploy of {made} failed; Fly's output is above.",
              file=sys.stderr)
        return 1
    ipv4_only(fly, args.app)
    print(f"Deployed {made}. The demo is at https://{args.app}.fly.dev")
    return 0


def ipv4_only(fly: str, app: str) -> None:
    """Give the app no public IPv6 address, so every visitor comes by IPv4.

    Fly gives a new app an IPv6 address beside its shared IPv4 one. From the
    first network the demo was opened on (a home connection, 28 October
    2026), every TLS handshake to Fly over IPv6 -- fly.io's own site
    included -- stalled for five seconds and failed, while IPv4 never did:
    the page loaded on a lucky connection and its WebSocket then failed.
    Browsers prefer IPv6 and do not fall back once a connection is open, so
    any visitor on such a network would see the demo fail to connect.
    Without the address, the name has no IPv6 record. ``fly ips allocate-v6``
    puts one back.
    """
    listed = subprocess.run([fly, "ips", "list", "--app", app, "--json"],
                            capture_output=True, text=True)
    try:
        ips = json.loads(listed.stdout or "[]")
    except ValueError:
        print(f"Could not read the app's addresses: {listed.stderr.strip()}",
              file=sys.stderr)
        return
    for ip in ips:
        fields = {k.lower(): v for k, v in ip.items()}
        if fields.get("type") == "v6":
            print(f"Releasing the public IPv6 address {fields.get('address')}.",
                  flush=True)
            subprocess.run([fly, "ips", "release", str(fields.get("address")),
                            "--app", app])


if __name__ == "__main__":
    sys.exit(main())
