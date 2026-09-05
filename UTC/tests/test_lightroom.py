"""Tests for the Lightroom RAW-develop batch.

Two halves, both hermetic -- no Lightroom, no GPR files, no display:

* the crop arithmetic, which is the one number the feature turns on;
* the catalog poller, against a SQLite fixture carrying the subset of
  Lightroom's schema the poller actually joins on.

The fixture is built from the schema observed in Lightroom Classic 14.5.1. If
Adobe changes it these tests keep passing while the real thing breaks, so they
guard against *our* regressions, not against Adobe's. The live script covers
the real catalog.

Runnable directly (``python tests/test_lightroom.py``) or under pytest.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utc.lightroom import catalog as cat  # noqa: E402
from utc.lightroom import gpr, install, preflight  # noqa: E402
from utc.lightroom.spec import (  # noqa: E402
    CROP_H,
    CROP_W,
    CropImpossible,
    CropRect,
    RawReport,
    crop_fractions,
)

#: The HERO12 Black frame every ROV GPR arrives at.
HERO12 = (5568, 4872)


# ---------------------------------------------------------------- crop maths

def test_hero12_lands_on_the_protocol_size():
    r = crop_fractions(*HERO12)
    assert r.size_in(*HERO12) == (CROP_W, CROP_H), r


def test_crop_is_centred():
    r = crop_fractions(*HERO12)
    assert abs((1.0 - r.right) - r.left) <= 2e-6, r
    assert abs((1.0 - r.bottom) - r.top) <= 2e-6, r


def test_fractions_survive_lightrooms_six_decimals():
    """A rectangle that only works at full float precision is a bug waiting."""
    r = crop_fractions(*HERO12)
    for v in (r.left, r.top, r.right, r.bottom):
        assert round(v, 6) == v, v


def test_many_sources_still_land_exactly():
    """Whatever the source frame, the delivered size is the delivered size."""
    for src in ((5568, 4872), (5568, 4176), (4700, 4100),
                (6000, 5000), (4606, 4030), (8000, 6000)):
        r = crop_fractions(*src)
        assert r.size_in(*src) == (CROP_W, CROP_H), (src, r)


def test_source_too_small_is_refused_not_fudged():
    for src in ((4605, 4030), (4606, 4029), (1000, 1000)):
        try:
            crop_fractions(*src)
        except CropImpossible:
            continue
        raise AssertionError(f"{src} should not have produced a crop")


def test_settings_carry_the_crop_flag():
    s = crop_fractions(*HERO12).as_settings()
    assert s["HasCrop"] is True
    assert s["CropAngle"] == 0.0
    assert s["CropLeft"] < s["CropRight"] and s["CropTop"] < s["CropBottom"]


def test_rounding_is_half_up_not_bankers():
    """Python rounds 0.5 to even; Lightroom does not. 2.5 px must be 3."""
    r = CropRect(0.0, 0.0, 0.00025, 0.00035)
    assert r.size_in(10000, 10000) == (3, 4)


# ------------------------------------------------------------ catalog poller

_SCHEMA = """
create table AgLibraryRootFolder (id_local integer primary key, absolutePath text);
create table AgLibraryFolder (id_local integer primary key, pathFromRoot text, rootFolder integer);
create table AgLibraryFile (id_local integer primary key, idx_filename text, extension text, folder integer);
create table Adobe_images (id_local integer primary key, rootFile integer);
create table Adobe_imageDevelopSettings (
    id_local integer primary key, image integer,
    croppedWidth, croppedHeight,
    removeChromaticAberration real, hasBigData integer, text text);
"""

# Copied from a real denoised row in a 14.5.1 catalog: the Enhance filter and
# the localisation key that carries the word the poller globs for.
_DENOISED_TEXT = ('s = { FilterList = { Filters = { { Name = "Enhance", '
                  'Title = "$$$/CRaw/Filter/Title/Denoise=Denoise" } } } }')
# The trap: this mentions noise reduction but is not a Denoise. A case-blind
# LIKE would match it on "...anceNoise..." -- the poller uses GLOB for exactly
# this reason.
_PLAIN_TEXT = 's = { LuminanceNoiseReductionContrast = 0, Exposure2012 = 0 }'


def _fixture(db: Path, folder: Path, rows) -> None:
    """rows are (name, crop_w or None, crop_h or None, ca, denoised)."""
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    root = folder.resolve().as_posix() + "/"
    con.execute("insert into AgLibraryRootFolder values (1, ?)", (root,))
    con.execute("insert into AgLibraryFolder values (1, '', 1)")
    for i, (name, cw, ch, ca, den) in enumerate(rows, start=1):
        con.execute("insert into AgLibraryFile values (?,?,'GPR',1)", (i, name))
        con.execute("insert into Adobe_images values (?,?)", (i, i))
        con.execute(
            "insert into Adobe_imageDevelopSettings values (?,?,?,?,?,?,?)",
            (i, i,
             cw if cw is not None else "uncropped",
             ch,
             1.0 if ca else 0.0,
             1 if den else 0,
             _DENOISED_TEXT if den else _PLAIN_TEXT))
    con.commit()
    con.close()


def _with_fixture(rows):
    td = tempfile.TemporaryDirectory()
    d = Path(td.name)
    folder = d / "GPR"
    folder.mkdir()
    db = d / "scratch.lrcat"
    _fixture(db, folder, rows)
    return td, db, folder


def test_poller_counts_crop_and_denoise():
    td, db, folder = _with_fixture([
        ("a.GPR", CROP_W, CROP_H, True, True),
        ("b.GPR", CROP_W, CROP_H, True, False),
        ("c.GPR", None, None, False, False),
    ])
    with td:
        p = cat.CatalogPoller(db, folder, crop_w=CROP_W, crop_h=CROP_H)
        got = p.poll()
        assert (got.total, got.cropped, got.denoised) == (3, 2, 1), got
        assert not got.unknown


def test_a_wrong_sized_crop_does_not_count_as_cropped():
    """4606x4033 is what the hand-drawn rectangle produced. It is not done."""
    td, db, folder = _with_fixture([("a.GPR", CROP_W, 4033, True, False)])
    with td:
        p = cat.CatalogPoller(db, folder, crop_w=CROP_W, crop_h=CROP_H)
        assert p.poll().cropped == 0


def test_denoise_needs_both_the_marker_and_the_blob():
    """hasBigData alone is masks; it is not evidence of a Denoise."""
    td, db, folder = _with_fixture([("a.GPR", None, None, False, False)])
    with td:
        con = sqlite3.connect(db)
        con.execute("update Adobe_imageDevelopSettings set hasBigData = 1")
        con.commit()
        con.close()
        p = cat.CatalogPoller(db, folder, crop_w=CROP_W, crop_h=CROP_H)
        assert p.poll().denoised == 0


def test_a_missing_catalog_reports_unknown_not_a_crash():
    p = cat.CatalogPoller(Path("nowhere") / "gone.lrcat", Path("nowhere"),
                          crop_w=CROP_W, crop_h=CROP_H)
    got = p.poll()
    assert got.unknown and got.total == 0


def test_a_changed_schema_reports_unknown_not_a_crash():
    """Adobe renaming a column must not crash a forty-minute run."""
    td, db, folder = _with_fixture([("a.GPR", CROP_W, CROP_H, True, True)])
    with td:
        con = sqlite3.connect(db)
        con.execute("alter table Adobe_imageDevelopSettings rename to gone")
        con.commit()
        con.close()
        p = cat.CatalogPoller(db, folder, crop_w=CROP_W, crop_h=CROP_H)
        assert p.poll().unknown


def test_other_folders_in_the_catalog_are_not_counted():
    td, db, folder = _with_fixture([("a.GPR", CROP_W, CROP_H, True, True)])
    with td:
        p = cat.CatalogPoller(db, folder.parent / "elsewhere",
                              crop_w=CROP_W, crop_h=CROP_H)
        assert p.poll().total == 0


# ------------------------------------------------------------ where output goes

def test_tif_folder_is_a_sibling_of_gpr_never_a_child():
    """Nested inside GPR, exports would be re-read by the next run."""
    gpr_dir = Path("flight") / "photos" / "transects" / "T1" / "GPR"
    out = preflight.tif_dir_for(gpr_dir)
    assert out == gpr_dir.parent / "TIF"
    assert out.parent == gpr_dir.parent
    assert gpr_dir not in out.parents


# -------------------------------------------------------------- plugin install

def _isolated_modules(td: Path):
    """Point install.modules_dir() at a temp folder for the duration."""
    import os
    before = os.environ.get("APPDATA")
    os.environ["APPDATA"] = str(td)
    return before


def _restore_appdata(before):
    import os
    if before is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = before


def test_plugin_install_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            first = install.install_plugin()
            names = sorted(p.name for p in first.iterdir())
            assert "Info.lua" in names and "Job.lua" in names
            assert install.plugin_is_current()
            again = install.install_plugin()
            assert again == first
            assert sorted(p.name for p in again.iterdir()) == names
        finally:
            _restore_appdata(before)


def test_plugin_install_repairs_an_emptied_folder():
    """The real failure: Windows refused to remove the folder, leaving it
    empty and stamped. Lightroom loads that as a broken plugin."""
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            dest = install.install_plugin()
            for f in dest.iterdir():
                if f.name != install._STAMP:
                    f.unlink()
            assert install.install_plugin(force=True)
            assert (dest / "Info.lua").is_file()
            assert (dest / "Job.lua").is_file()
        finally:
            _restore_appdata(before)


def test_an_interrupted_install_is_not_trusted():
    """No stamp means out of date, so the next run repairs it."""
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            dest = install.install_plugin()
            (dest / install._STAMP).unlink()
            assert not install.plugin_is_current()
        finally:
            _restore_appdata(before)


# ------------------------------------------------------- finding GPR folders

def test_gpr_folders_are_offered_in_transect_order():
    """An import scatters raws across one folder per transect, so the card
    offers what is there. T2 must not sort before T10 alphabetically."""
    with tempfile.TemporaryDirectory() as td:
        flight = Path(td) / "2026_08_25_Site"
        for name in ("T10", "T2", "T1"):
            d = flight / "photos" / "transects" / name / "GPR"
            d.mkdir(parents=True)
            (d / "a.GPR").write_bytes(b"x")
        staging = flight / "photos" / "GPR"
        staging.mkdir(parents=True)
        (staging / "b.GPR").write_bytes(b"x")

        got = gpr.find_folders(flight)
        assert [p.parent.name for p in got] == ["T1", "T2", "T10", "photos"], got


def test_empty_gpr_folders_are_not_offered():
    """A scaffolded flight has an empty photos/GPR; offering it is a dead end."""
    with tempfile.TemporaryDirectory() as td:
        flight = Path(td) / "flight"
        (flight / "photos" / "GPR").mkdir(parents=True)
        assert gpr.find_folders(flight) == []


# ------------------------------------------------------ Lightroom knows of it

# Lightroom stores these as Lua source inside a Lua string, so every backslash
# in a path appears four times. Built the way the real file is written.
_PREFS_HEAD = 'prefs = {\r\n\tAgSomethingElse = true,\r\n'
_PREFS_TAIL = '\tAgZebra = false,\r\n}\r\n'


def _prefs_text(installed=(), disabled_paths=(), disabled_ids=()):
    def block(key, entries):
        out = '\t' + key + ' = "t = {\\\r\n'
        for e in entries:
            out += '\\"' + str(e).replace("\\", "\\\\\\\\") + '\\",\\\r\n'
        return out + '}\\\r\n",\r\n'
    return (_PREFS_HEAD
            + block("AgSdkPluginLoader_disabledPluginIDs", disabled_ids)
            + block("AgSdkPluginLoader_disabledPluginPaths", disabled_paths)
            + block("AgSdkPluginLoader_installedPluginPaths", installed)
            + _PREFS_TAIL)


def _write_prefs(appdata: Path, text: str) -> None:
    d = appdata / "Adobe" / "Lightroom" / "Preferences"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Lightroom Classic CC 7 Preferences.agprefs").write_bytes(
        text.encode("utf-8"))


def test_a_plugin_on_disk_is_not_a_registered_plugin():
    """The bug that cost a three-minute silent failure: installing the plug-in
    does nothing until Lightroom is told about it."""
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            _write_prefs(Path(td), _prefs_text())
            assert install.plugin_registration() == "absent"
        finally:
            _restore_appdata(before)


def test_registration_is_seen_through_lightrooms_escaping():
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            _write_prefs(Path(td), _prefs_text(installed=[install.installed_plugin()]))
            assert install.plugin_registration() == "registered"
        finally:
            _restore_appdata(before)


def test_a_switched_off_plugin_is_not_registered():
    """Disabled in the Plug-in Manager looks exactly like working, right up
    until the batch waits forever."""
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            _write_prefs(Path(td), _prefs_text(
                installed=[install.installed_plugin()],
                disabled_ids=[install.TOOLKIT_ID]))
            assert install.plugin_registration() == "disabled"
        finally:
            _restore_appdata(before)


def test_a_multi_entry_list_is_read_whole():
    """Every entry is written \\"path\\", so terminating the block on the first
    '",' silently truncates it after one item -- which reads as a short list
    rather than as a parse failure."""
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            paths = [Path(r"C:\one\a.lrplugin"),
                     Path(r"C:\two\b.lrplugin"),
                     install.installed_plugin()]
            _write_prefs(Path(td), _prefs_text(installed=paths))
            block = install._pref_block(
                (Path(td) / "Adobe" / "Lightroom" / "Preferences"
                 / "Lightroom Classic CC 7 Preferences.agprefs"
                 ).read_text(encoding="utf-8"),
                "AgSdkPluginLoader_installedPluginPaths")
            assert block.count("lrplugin") == 3, block
            # and the last entry is still found
            assert install.plugin_registration() == "registered"
        finally:
            _restore_appdata(before)


def test_utc_scratch_catalogs_are_not_offered_as_the_users_own():
    """Lightroom reopens its most recent catalog, and after a run that is a
    scratch one UTC deletes -- so it must never be handed back as 'yours'."""
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            mine = Path(td) / "Mine.lrcat"
            mine.write_bytes(b"x")
            scratch = install.utc_root() / "runs" / "r" / "UTC_scratch.lrcat"
            scratch.parent.mkdir(parents=True, exist_ok=True)
            scratch.write_bytes(b"x")
            text = _prefs_text() + '\trecentLibraries20 = "recentLibraries = {\r\n'
            for p in (scratch, mine):
                text += '\t\\"' + str(p).replace("\\", "\\\\\\\\") + '\\",\\\r\n'
            text += '}\\\r\n",\r\n'
            _write_prefs(Path(td), text)
            assert install.last_real_catalog() == mine
        finally:
            _restore_appdata(before)


def test_the_scratch_catalog_husk_survives_cleanup():
    """Deleting it leaves Lightroom's 'reopen last catalog' pointing at
    nothing, so the operator's own next launch fails."""
    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "run"
        _fake_run(run)
        install.clean_run_dir(run)
        assert (run / "UTC_scratch.lrcat").is_file()
        assert not (run / "UTC_scratch Previews.lrdata").exists()
        assert not (run / "UTC_scratch.lrcat-data").exists()


def test_missing_preferences_is_unknown_not_a_guess():
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_modules(Path(td))
        try:
            assert install.plugin_registration() == "unknown"
        finally:
            _restore_appdata(before)


def test_the_plugin_does_not_live_in_adobes_folder():
    """Modules is not an auto-load folder, and putting it there implied it
    was. The plug-in belongs somewhere UTC owns."""
    assert "Adobe" not in install.installed_plugin().parts


def test_an_over_long_catalog_path_is_caught_before_the_run():
    """Lightroom puts up a modal Warning and never starts, which reads from
    the outside as 'the plug-in never loaded'."""
    short = Path("C:/Users/x/AppData/Local/UTC/lightroom/runs/r/UTC_scratch.lrcat")
    assert install.catalog_path_problem(short) == ""
    long = Path("C:/" + "d" * 200 + "/UTC_scratch.lrcat")
    why = install.catalog_path_problem(long)
    assert str(install.MAX_CATALOG_PATH) in why, why


# ------------------------------------------------------------------- cleanup

def _isolated_scratch(td: Path):
    """Point install.utc_root() at a temp folder for the duration."""
    import os
    before = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(td)
    return before


def _restore_localappdata(before):
    import os
    if before is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = before


def _fake_run(run_dir: Path) -> None:
    """A run directory with the heavy things Lightroom leaves in one."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plugin.log").write_text("what happened", encoding="utf-8")
    (run_dir / "job.txt").write_text("version=1", encoding="utf-8")
    (run_dir / "UTC_scratch.lrcat").write_bytes(b"x" * 2048)
    for side in ("UTC_scratch.lrcat-data", "UTC_scratch Previews.lrdata"):
        d = run_dir / side
        d.mkdir()
        (d / "blob").write_bytes(b"y" * 4096)


def test_a_successful_run_leaves_only_the_catalog_husk():
    """Two megabytes stay so Lightroom's 'reopen the last catalog' resolves.
    Everything with weight in it goes."""
    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "run"
        _fake_run(run)
        install.clean_run_dir(run)
        left = sorted(p.name for p in run.iterdir())
        assert left == ["UTC_scratch.lrcat"], left


def test_a_failed_run_keeps_the_log_but_not_the_gigabytes():
    """The log is the only account of what Lightroom did. The previews and the
    blob store are one to two GB per run and are worth nothing afterwards."""
    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "run"
        _fake_run(run)
        install.clean_run_dir(run, keep_diagnostics=True)
        assert (run / "plugin.log").read_text(encoding="utf-8") == "what happened"
        assert (run / "job.txt").is_file()
        assert not (run / "UTC_scratch.lrcat-data").exists()
        assert not (run / "UTC_scratch Previews.lrdata").exists()


def test_old_runs_are_pruned_but_recent_ones_kept():
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_scratch(Path(td))
        try:
            runs = install.utc_root() / "runs"
            for i in range(6):
                _fake_run(runs / f"run{i}")
            gone = install.prune_runs(keep=2, days=install.KEEP_RUN_DAYS)
            left = sorted(d.name for d in runs.iterdir())
            assert gone == 4, (gone, left)
            assert len(left) == 2, left
        finally:
            _restore_localappdata(before)


def test_pruning_cannot_reach_the_seed():
    """The seed lives beside the runs, not inside them: a prune that ate it
    would turn every future run into a manual setup step."""
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_scratch(Path(td))
        try:
            seed = install.seed_dir("14.5.1") / install.SEED_NAME
            seed.parent.mkdir(parents=True, exist_ok=True)
            seed.write_bytes(b"seed")
            _fake_run(install.utc_root() / "runs" / "old")
            install.prune_runs(keep=0, days=0)
            assert seed.is_file(), "the seed was deleted by pruning"
        finally:
            _restore_localappdata(before)


def test_scratch_bytes_counts_what_is_held():
    with tempfile.TemporaryDirectory() as td:
        before = _isolated_scratch(Path(td))
        try:
            assert install.scratch_bytes() == 0
            _fake_run(install.utc_root() / "runs" / "one")
            assert install.scratch_bytes() > 4096
        finally:
            _restore_localappdata(before)


def test_a_working_catalog_is_refused_as_a_seed():
    """The seed has to be empty; pointing this at a real catalog would put
    survey frames into somebody's own library."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "not_empty.lrcat"
        con = sqlite3.connect(db)
        con.executescript(_SCHEMA)
        con.execute("insert into Adobe_images values (1, 1)")
        con.commit()
        con.close()
        why = install.describe_seed_problem(db)
        assert "photo" in why.lower(), why


def test_report_summary_mentions_the_destination():
    r = RawReport(source=Path("x") / "GPR", root=Path("x") / "TIF",
                  found=3, imported=3, cropped=3, denoised=3, exported=3)
    assert "TIF" in r.summary()


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception:
                fails += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    print("failures:", fails)
    sys.exit(1 if fails else 0)
