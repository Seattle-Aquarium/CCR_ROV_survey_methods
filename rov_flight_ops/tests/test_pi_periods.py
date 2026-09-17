"""
The BlueOS logs page's file types and its worked-out time periods.

Two things are pinned down here.

**A log that is still being written can still be downloaded.** The autopilot
appends to its dataflash log for as long as the ROV has power, and the ROV has
to have power for any of this to work -- so a BIN is always bigger by the time
it has finished coming down than the listing said it would be. Insisting the
two match refused every BIN outright in the field on 16 September 2026. What
must *not* change is the other half of that check: a copy that arrives short is
a truncated download and is still a failure.

**"This flight", "Today" and "Previous day" have to show their working.** Each
is worked out from the files on the vehicle rather than from a calendar, and
"Previous day" is used to delete. A period that quietly picked the wrong day
would be found out after the files had gone.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_pifiles import BIN_DIR, START, dataflash, vehicle  # noqa: F401

from rov_flight_ops import pifiles as PF

DAY = 86400.0


# --------------------------------------------------------------------------
#  the file types
# --------------------------------------------------------------------------


def test_the_video_type_is_called_video():
    """It is the same stream the mcap also carries, but on this page it is a
    file type to tick, and "mcap video" read as part of the mcap row."""
    assert PF.BY_KEY["video"].label == "video"
    # Its folder on disk is unchanged, so flight folders already filed stay
    # where they are.
    assert PF.BY_KEY["video"].dest == "logs/mcap_video"


def test_the_types_are_offered_in_the_order_a_survey_day_uses_them():
    assert [c.key for c in PF.CATEGORIES] == ["mcap", "bin", "c3", "video", "tlog"]


# --------------------------------------------------------------------------
#  a file that grows while it is copied
# --------------------------------------------------------------------------


def _grow(v, path: str, extra: int) -> None:
    """Append to a file on the fake vehicle, as the autopilot would."""
    v.fs[path]["data"] = v.fs[path]["data"] + bytes(extra)
    v.fs[path]["modified"] = v.fs[path]["modified"] + 1


def test_a_bin_that_grew_while_it_copied_is_kept(vehicle, tmp_path):  # noqa: F811
    inv = PF.search(vehicle.host, ["bin"])
    target = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    listed = target.size

    # Between the listing and the download the autopilot writes some more --
    # which is what happens every time, because the ROV is still powered.
    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 4_695)

    rep = PF.download([target], tmp_path, inv.host, inv.token)
    assert not rep.failed, rep.summary()
    assert [f.rel for f in rep.done] == ["00000081.BIN"]

    on_disk = (tmp_path / "logs/BIN/00000081.BIN").stat().st_size
    assert on_disk == listed + 4_695
    assert any("grew while it was copied" in w for w in rep.warnings)


def test_the_growth_is_recorded_rather_than_papered_over(vehicle, tmp_path):  # noqa: F811
    inv = PF.search(vehicle.host, ["bin"])
    target = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 1_000)
    PF.download([target], tmp_path, inv.host, inv.token)

    entry = PF.load_manifest(tmp_path)[target.path]
    assert entry["was_growing"] is True
    assert entry["size"] == target.size + 1_000        # what actually arrived
    assert entry["listed_size"] == target.size         # what was promised
    assert entry["sha256"]

    # The vehicle goes on writing after the copy finished, which is what it
    # does for as long as the ROV has power. Listed again, its copy is bigger
    # than the local one -- which is neither a mismatch to be alarmed by nor
    # a verified copy.
    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 1_000)
    fresh = PF.search(vehicle.host, ["bin"])
    now = next(f for f in fresh.files["bin"] if f.rel == "00000081.BIN")
    assert PF.copy_state(now, tmp_path) == "growing"


def test_a_finished_log_downloaded_again_verifies_normally(vehicle, tmp_path):  # noqa: F811
    """The day after, the vehicle has stopped writing it and the copy is whole."""
    inv = PF.search(vehicle.host, ["bin"])
    target = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 800)
    PF.download([target], tmp_path, inv.host, inv.token)

    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 500)     # still flying
    fresh = PF.search(vehicle.host, ["bin"])
    now = next(f for f in fresh.files["bin"] if f.rel == "00000081.BIN")
    assert PF.copy_state(now, tmp_path) == "growing"

    # Powered down since; the log is final, and the copy of it is whole.
    rep = PF.download([now], tmp_path, inv.host, inv.token)
    assert not rep.failed, rep.summary()
    again = PF.search(vehicle.host, ["bin"])
    settled = next(f for f in again.files["bin"] if f.rel == "00000081.BIN")
    assert PF.copy_state(settled, tmp_path) == "verified"


def test_a_short_download_is_still_a_failure(vehicle, tmp_path):  # noqa: F811
    """The check exists to catch a truncated copy, and still does."""
    inv = PF.search(vehicle.host, ["bin"])
    target = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    # The vehicle hands over less than it said it had.
    vehicle.fs[f"{BIN_DIR}/00000081.BIN"]["data"] = bytes(target.size // 2)

    rep = PF.download([target], tmp_path, inv.host, inv.token)
    assert [f.rel for f, _why in rep.failed] == ["00000081.BIN"]
    assert not (tmp_path / "logs/BIN/00000081.BIN").exists()


def test_a_file_replaced_by_a_smaller_one_is_not_mistaken_for_growth(vehicle,  # noqa: F811
                                                                     tmp_path):
    inv = PF.search(vehicle.host, ["bin"])
    target = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    long_enough = target.size + 100

    # More bytes arrive than were listed, but the vehicle no longer holds
    # them: the file has been rotated away under the same name.
    vehicle.fs[f"{BIN_DIR}/00000081.BIN"]["data"] = bytes(long_enough)
    listing_after = {"size": 10}

    original = PF.list_dir

    def shrunk(host, token, path):
        items, why = original(host, token, path)
        for i in items or []:
            if i["path"] == target.path:
                i["size"] = listing_after["size"]
        return items, why

    PF.list_dir = shrunk
    try:
        rep = PF.download([target], tmp_path, inv.host, inv.token)
    finally:
        PF.list_dir = original
    assert rep.failed, "a file that shrank must not pass as one that grew"


def test_a_growing_copy_is_not_skipped_next_time(vehicle, tmp_path):  # noqa: F811
    """Download skips what is already verified or the same size. A copy that
    stops short of the vehicle's is neither, so it comes down again."""
    inv = PF.search(vehicle.host, ["bin"])
    target = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 800)
    PF.download([target], tmp_path, inv.host, inv.token)

    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 400)
    fresh = PF.search(vehicle.host, ["bin"])
    now = next(f for f in fresh.files["bin"] if f.rel == "00000081.BIN")
    rep = PF.download([now], tmp_path, inv.host, inv.token)
    assert [f.rel for f in rep.done] == ["00000081.BIN"]
    assert not rep.skipped


def test_downloading_a_growing_file_still_writes_nothing_to_the_vehicle(
        vehicle, tmp_path):  # noqa: F811
    inv = PF.search(vehicle.host, ["bin"])
    target = next(f for f in inv.files["bin"] if f.rel == "00000081.BIN")
    _grow(vehicle, f"{BIN_DIR}/00000081.BIN", 300)
    vehicle.seen.clear()
    PF.download([target], tmp_path, inv.host, inv.token)
    assert {m for m, _ in vehicle.seen} == {"GET"}


# --------------------------------------------------------------------------
#  telling one flight from the next
# --------------------------------------------------------------------------


def _log(category: str, rel: str, start: float, end: float,
         size: int = 1000) -> PF.PiFile:
    return PF.PiFile(category=category, path=f"/x/{rel}", rel=rel, size=size,
                     modified=end, start=start, end=end)


def test_recordings_close_together_are_one_flight():
    """Within a flight the vehicle may disarm, reboot, even power-cycle, and
    each of those starts a new mcap. None of them takes five minutes."""
    base = 1_789_152_000.0
    files = [
        _log("mcap", "a.mcap", base, base + 600),
        _log("bin", "1.BIN", base + 5, base + 620),          # same flight
        _log("mcap", "b.mcap", base + 660, base + 1200),     # 40 s later
    ]
    assert PF.flight_spans(files) == [(base, base + 1200)]


def test_recordings_far_apart_are_different_flights():
    base = 1_789_152_000.0
    files = [
        _log("mcap", "a.mcap", base, base + 600),
        _log("mcap", "b.mcap", base + 600 + 200, base + 1600),   # 200 s: same
        _log("mcap", "c.mcap", base + 1600 + 900, base + 3000),  # 900 s: next
    ]
    spans = PF.flight_spans(files)
    assert len(spans) == 2
    assert spans[0] == (base, base + 1600)
    assert PF.latest_flight(files) == spans[-1]


def test_imagery_does_not_decide_where_a_flight_ends():
    """A photo has a moment, not a span, and a folder of them cannot say."""
    base = 1_789_152_000.0
    files = [_log("c3", "img.jpg", base, base), _log("mcap", "a.mcap", base, base + 60)]
    assert PF.flight_spans(files) == [(base, base + 60)]


def test_a_recording_with_no_readable_time_is_not_guessed_at():
    f = PF.PiFile(category="mcap", path="/x/a.mcap", rel="a.mcap", size=10)
    assert PF.flight_spans([f]) == []
    assert PF.latest_flight([f]) is None


# --------------------------------------------------------------------------
#  the periods themselves
# --------------------------------------------------------------------------


def _inventory(files: list[PF.PiFile], skew: float | None = 0.0) -> PF.Inventory:
    inv = PF.Inventory(host="1.2.3.4", token="t", vehicle="Nereo", skew=skew,
                       listed_at=time.time())
    for cat in PF.CATEGORIES:
        inv.files[cat.key] = [f for f in files if f.category == cat.key]
    return inv


def _at(day_offset: int, hour: int, now: float) -> float:
    here = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    return (here + timedelta(days=day_offset, hours=hour)).timestamp()


def test_this_flight_takes_the_last_stretch_of_recording():
    now = time.time()
    early = _at(0, 9, now)
    late = _at(0, 14, now)
    files = [
        _log("mcap", "morning.mcap", early, early + 1200),
        _log("bin", "morning.BIN", early, early + 1300),
        _log("mcap", "afternoon.mcap", late, late + 1200),
        _log("bin", "afternoon.BIN", late, late + 1250),
    ]
    inv = _inventory(files)
    c = PF.choose(inv, ["mcap", "bin"], PF.PERIOD_THIS_FLIGHT, now=now)
    assert sorted(f.rel for f in c.files) == ["afternoon.BIN", "afternoon.mcap"]
    assert "this flight" in c.window_note
    assert "the last of 2" in c.window_note


def test_this_flight_sweeps_in_the_imagery_that_belongs_to_it():
    now = time.time()
    late = _at(0, 14, now)
    files = [
        _log("mcap", "old.mcap", _at(-3, 11, now), _at(-3, 11, now) + 600),
        _log("c3", "old.jpg", _at(-3, 11, now) + 100, _at(-3, 11, now) + 100),
        _log("mcap", "now.mcap", late, late + 1200),
        _log("c3", "now.jpg", late + 300, late + 300),
    ]
    inv = _inventory(files)
    c = PF.choose(inv, ["mcap", "c3"], PF.PERIOD_THIS_FLIGHT, now=now)
    assert sorted(f.rel for f in c.files) == ["now.jpg", "now.mcap"]


def test_this_flight_says_so_when_it_cannot_tell():
    inv = _inventory([PF.PiFile(category="mcap", path="/x/a", rel="a", size=1)])
    c = PF.choose(inv, ["mcap"], PF.PERIOD_THIS_FLIGHT)
    assert not c.files
    assert "cannot be worked out" in c.note


def test_today_is_everything_since_local_midnight():
    now = time.time()
    files = [
        _log("mcap", "yesterday.mcap", _at(-1, 14, now), _at(-1, 14, now) + 600),
        _log("mcap", "this_morning.mcap", _at(0, 9, now), _at(0, 9, now) + 600),
        _log("mcap", "just_now.mcap", _at(0, 14, now), _at(0, 14, now) + 600),
    ]
    c = PF.choose(_inventory(files), ["mcap"], PF.PERIOD_TODAY, now=now)
    assert sorted(f.rel for f in c.files) == ["just_now.mcap", "this_morning.mcap"]
    assert "today" in c.window_note


def test_previous_day_is_the_last_day_flown_not_literally_yesterday():
    """Survey days are not consecutive. After a fortnight ashore, offering to
    clear 'yesterday' would pick nothing and look broken."""
    now = time.time()
    files = [
        _log("mcap", "a_fortnight_ago.mcap", _at(-14, 11, now), _at(-14, 11, now) + 600),
        _log("mcap", "today.mcap", _at(0, 11, now), _at(0, 11, now) + 600),
    ]
    c = PF.choose(_inventory(files), ["mcap"], PF.PERIOD_PREVIOUS_DAY, now=now)
    assert [f.rel for f in c.files] == ["a_fortnight_ago.mcap"]
    assert "previous flying day" in c.window_note


def test_previous_day_never_picks_todays_files():
    """It is used to delete, so this is the one that matters."""
    now = time.time()
    files = [
        _log("mcap", "yesterday.mcap", _at(-1, 11, now), _at(-1, 11, now) + 600),
        _log("mcap", "today.mcap", _at(0, 11, now), _at(0, 11, now) + 600),
    ]
    c = PF.choose(_inventory(files), ["mcap"], PF.PERIOD_PREVIOUS_DAY,
                  destructive=True, now=now)
    assert [f.rel for f in c.files] == ["yesterday.mcap"]


def test_previous_day_says_so_when_there_is_nothing_older():
    now = time.time()
    files = [_log("mcap", "today.mcap", _at(0, 11, now), _at(0, 11, now) + 600)]
    c = PF.choose(_inventory(files), ["mcap"], PF.PERIOD_PREVIOUS_DAY, now=now)
    assert not c.files
    assert "no previous day" in c.note


def test_the_vehicle_clock_is_allowed_for():
    """Every time on a listed file comes off a Pi with no battery-backed
    clock. Asking 'was this today?' against the laptop's midnight without
    correcting for that is asking two clocks the same question."""
    now = time.time()
    skew = 3600.0                        # the vehicle is an hour ahead
    late = _at(0, 23, now) + 1800        # 23:30 here, 00:30 tomorrow there
    files = [_log("mcap", "late.mcap", late + skew, late + skew + 600)]
    inv = _inventory(files, skew=skew)

    c = PF.choose(inv, ["mcap"], PF.PERIOD_TODAY, now=now)
    assert [f.rel for f in c.files] == ["late.mcap"], (
        "the file was recorded today by this laptop's clock")
    assert "vehicle's clock is +3600 s" in c.window_note


def test_the_old_periods_are_unchanged():
    now = time.time()
    files = [_log("mcap", "a.mcap", _at(-5, 11, now), _at(-5, 11, now) + 600)]
    inv = _inventory(files)
    assert len(PF.choose(inv, ["mcap"], PF.PERIOD_ALL).files) == 1
    assert not PF.choose(inv, ["mcap"], PF.PERIOD_MANUAL).files
    assert PF.choose(inv, ["mcap"], "").note.startswith("choose a time period")
    assert PF.choose(inv, [], PF.PERIOD_TODAY).note == "choose at least one file type"


def test_a_type_not_listed_yet_is_reported_for_the_new_periods_too():
    now = time.time()
    files = [_log("mcap", "a.mcap", _at(0, 11, now), _at(0, 11, now) + 600)]
    inv = _inventory(files)
    del inv.files["bin"]
    c = PF.choose(inv, ["mcap", "bin"], PF.PERIOD_TODAY, now=now)
    assert c.not_listed == ["bin"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
