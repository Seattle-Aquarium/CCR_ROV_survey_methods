"""
Put the web demo on Hugging Face: assemble the Space repository and upload it.

    python web_demo/deploy.py                    # to zhrandell/rov-flight-ops-simulator
    python web_demo/deploy.py --space OWNER/NAME
    python web_demo/deploy.py --stage DIR        # assemble it into DIR, upload nothing

Run from ``rov_flight_ops/``, with ``huggingface_hub`` installed
(``pip install huggingface_hub``) and this computer logged in to Hugging Face
with a token that can write to the Space's owner (``hf auth login``). The
owner needs a paid plan -- PRO for a person, Team or Enterprise for an
organisation -- or Hugging Face refuses to create a Docker Space. The
Space builds its container from what is uploaded; the build takes several
minutes and can be watched on the Space's page.

What is uploaded: this folder's Dockerfile and README.md (the Space's card) at
the root, the repository's LICENSE, and the parts of the project the
container needs under ``rov_flight_ops/`` -- the same layout as the GitHub
repository, so the Dockerfile builds from either. Files under
``rov_flight_ops/`` on the Space that are no longer part of the project are
deleted by the upload.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: On a personal account because the Seattle-Aquarium organisation has no
#: plan that allows Docker Spaces (see "The web demo" in the README).
SPACE = "zhrandell/rov-flight-ops-simulator"

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
    """Assemble the Space repository in `into` (emptied first)."""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    shutil.copy2(HERE / "Dockerfile", into / "Dockerfile")
    shutil.copy2(HERE / "README.md", into / "README.md")
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


def app_url(space: str) -> str:
    owner, name = space.split("/", 1)
    host = f"{owner}-{name}".lower().replace("_", "-").replace(".", "-")
    return f"https://{host}.hf.space"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--space", default=SPACE, help=f"OWNER/NAME (default {SPACE})")
    ap.add_argument("--stage", type=Path, metavar="DIR",
                    help="assemble the Space repository into DIR and stop")
    ap.add_argument("--private", action="store_true",
                    help="create the Space private (only when it does not exist yet)")
    args = ap.parse_args(argv)

    if args.stage:
        print(f"Assembled the Space repository in {stage(args.stage)}")
        return 0

    try:
        from huggingface_hub import HfApi
        from huggingface_hub.errors import HfHubHTTPError
    except ImportError:
        print("Needs huggingface_hub:  python -m pip install huggingface_hub",
              file=sys.stderr)
        return 2
    api = HfApi()
    who = api.whoami()
    print(f"Logged in to Hugging Face as {who.get('name')}.")
    try:
        api.create_repo(args.space, repo_type="space", space_sdk="docker",
                        space_hardware="cpu-basic", private=args.private,
                        exist_ok=True)
    except HfHubHTTPError as ex:
        # 402: Docker Spaces need a paid plan on the account or organisation.
        if ex.response is None or ex.response.status_code != 402:
            raise
        print(f"Hugging Face would not create {args.space}:\n"
              f"{ex.server_message or ex}", file=sys.stderr)
        return 1
    made = commit()
    with tempfile.TemporaryDirectory(prefix="rov_space_") as tmp:
        folder = stage(Path(tmp) / "space")
        api.upload_folder(repo_id=args.space, repo_type="space", folder_path=folder,
                          commit_message=f"Deploy rov_flight_ops ({made})",
                          delete_patterns=["rov_flight_ops/**"])
    print(f"Uploaded {made} to https://huggingface.co/spaces/{args.space}\n"
          f"It is building now; once it is running the demo is at "
          f"{app_url(args.space)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
