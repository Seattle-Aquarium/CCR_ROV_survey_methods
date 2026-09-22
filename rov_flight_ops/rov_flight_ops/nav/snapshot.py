"""
Save a diagnostic snapshot: one file that is worth opening a week later.

What goes in is chosen by one question — could somebody who was not on the
boat work out what happened? So: the observations at the moment, a bounded
window of what the session recorded either side, the configuration that was
in force with its own age and provenance, and a manifest saying which program
and which vehicle stack produced it.

**Nothing here is collected specially.** The session log is already being
written a line at a time, the parameters are already read on their own thread,
and the environment lines already exist for the diagnostics bundle. A snapshot
that started its own collectors would change the thing it was measuring, and
would take the longest on the laptop that was already struggling.

**Parameters carry their age.** A value read twenty minutes ago is evidence
about twenty minutes ago. Rereading a file is not a fresh measurement of what
the vehicle is doing now, and a snapshot that presented one as the other would
be worse than one with no parameters in it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import changed as CH
from . import diagnose as DG
from . import session as SESS
from . import trust as TR

#: Schema version. Bumped when the shape changes, so a reader can tell.
VERSION = 1

#: Default filename stem; the timestamp keeps successive ones apart.
STEM = "nav_snapshot"


def _plain(value):
    """JSON-able, without pretending anything is simpler than it is."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, tuple)):
        return list(value)
    if hasattr(value, "to_json"):
        try:
            return value.to_json()
        except Exception:
            pass
    if hasattr(value, "name") and hasattr(value, "value"):   # an Enum
        return value.name
    return str(value)


def build(s, now: float, *, profile_key: str = "",
          origin_confirmed: bool | None = None,
          events: list[dict] | None = None,
          at_mono: float | None = None,
          params_age_s: float | None = None,
          site: dict | None = None,
          plan_summary: dict | None = None,
          jumps: list | None = None,
          extra: dict | None = None) -> dict:
    """The bundle, as a dictionary. Pure: nothing is read or written here."""
    report = DG.diagnose(s, now, profile_key=profile_key,
                         origin_confirmed=origin_confirmed)
    state, note = TR.track_state(s, now, profile_key=profile_key)
    moment = at_mono if at_mono is not None else now

    window = CH.around(events or [], moment)

    params = dict(s.params or {})
    bundle = {
        "schema": VERSION,
        "saved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "saved_mono": round(moment, 3),
        "profile": profile_key,
        "site": site or {},

        "verdict": {
            "state": state,
            "note": note,
            "severity": report.severity,
            "headline": report.headline,
            "findings": [
                {"key": f.key, "severity": f.severity,
                 "observed": f.observed, "suspected": f.suspected,
                 "actions": [a.label for a in f.actions]}
                for f in report.findings],
            "caveat": ("observations are measurements; anything under "
                       "'suspected' is a possibility offered for "
                       "investigation and is not a diagnosis"),
        },

        "position": {
            "fix": _plain(s.rov_fix) if s.rov_fix is not None else None,
            "aiding_mode": TR.aiding_mode(s)[0],
            "acoustic": TR.acoustic_line(s, now),
            "origin_confirmed": origin_confirmed,
            "jumps": [_plain(j) for j in (jumps or [])],
        },

        "configuration": {
            "parameters": params,
            "parameter_count": len(params),
            "parameters_age_s": params_age_s,
            "provenance": (
                "read from the autopilot's own dataflash log header; nothing "
                "was sent to the vehicle to obtain them. The age is how long "
                "ago that read happened — a value read minutes ago is "
                "evidence about minutes ago, not a measurement of the "
                "vehicle's current settings."),
        },

        "window": {
            "before_s": CH.BEFORE_S,
            "after_s": CH.AFTER_S,
            "summary": window.summary(),
            "truncated": window.truncated,
            "note": window.note,
            "events": [
                {"offset_s": round(a.offset_s, 3), "kind": a.kind,
                 "group": a.group, "row": a.row}
                for a in window.rows],
        },

        "streams": {
            name: _plain(h) for name, h in (s.messages or {}).items()},
        "link": _plain(s.link),
        "plan": plan_summary or {},
        "manifest": manifest(),
    }
    if extra:
        bundle["extra"] = extra
    return bundle


def manifest() -> dict:
    """Which program, which machine, which commit. Best effort, never raises."""
    out: dict = {"program": "rov_flight_ops"}
    try:
        from .. import diagnostics

        out["environment"] = list(diagnostics.environment_lines())
        out["commit"] = diagnostics.git_commit()
    except Exception as ex:
        out["environment_error"] = str(ex)
    return out


def save(bundle: dict, folder: Path | str, *, stem: str = STEM) -> Path | None:
    """Write the bundle beside the flight. Returns the path, or None.

    Never raises: a snapshot is something an operator reaches for when
    something has already gone wrong, and the last thing that moment needs is
    a second failure.
    """
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        when = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        path = folder / f"{stem}_{when}.json"
        text = json.dumps(bundle, indent=1, default=_plain, sort_keys=False)
        path.write_text(text, encoding="utf-8")
        return path
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "a diagnostic snapshot could not be written to %s", folder,
            exc_info=True)
        return None


def from_session_file(s, now: float, session_path: Path | str | None,
                      **kw) -> dict:
    """`build`, with the events read off the session log on disk."""
    events = []
    if session_path is not None:
        events = SESS.read_events(Path(session_path))
    return build(s, now, events=events, **kw)
