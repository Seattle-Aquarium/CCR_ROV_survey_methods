"""
Starting the batch, by clicking the menu item that starts it.

Lightroom will not run a plugin because it is installed, registered and
enabled. It initialises one lazily, the first time something asks for an entry
point the plugin declares -- so a plugin has to be *invoked*, and the only
ways in are the entry points Lightroom itself puts in its menus. Ours is
``File > Plug-in Extras > Run UTC RAW batch``, and this module clicks it.

That makes one menu click part of every run, including runs with AI Denoise
turned off: a top-level menu, a submenu and a fixed title, all of them things
Lightroom exposes properly to the accessibility layer. It is still automation,
though, so a batch cannot run on a locked screen even without Denoise.
"""

from __future__ import annotations

import time
from pathlib import Path

#: The title declared in Info.lua's LrExportMenuItems.
MENU_ITEM = "Run UTC RAW batch"

#: Lightroom needs to finish opening the catalog before its menus respond.
_READY_TIMEOUT = 240.0

#: How long to keep looking for the submenu entry once File is open.
_MENU_TIMEOUT = 20.0


class CannotStart(RuntimeError):
    """The menu item could not be reached, with a reason worth reading."""


def _pywinauto():
    try:
        import pywinauto
        import pywinauto.keyboard  # noqa: F401
    except Exception as ex:
        raise CannotStart(
            f"the pywinauto package is not usable ({ex}). "
            f"Run:  python -m pip install pywinauto") from ex
    return pywinauto


def _main_window(pwa, pid: int, timeout: float):
    """Lightroom's window, once it has one and the catalog is open."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            for w in pwa.Desktop(backend="uia").windows():
                try:
                    info = w.element_info
                    if info.process_id != pid:
                        continue
                    name = info.name or ""
                    # The title carries the catalog name once it is open; a
                    # bare splash window has nothing useful in it yet.
                    if "lightroom" in name.lower() and w.is_visible():
                        return w
                except Exception:
                    continue
        except Exception as ex:
            last = ex
        time.sleep(1.0)
    raise CannotStart(
        f"Lightroom did not present a window within {int(timeout)}s"
        + (f" ({last})" if last else ""))


def _menu_items(pwa, pid: int):
    """Every menu item currently on screen for this process."""
    out = []
    for w in pwa.Desktop(backend="uia").windows():
        try:
            if w.element_info.process_id != pid:
                continue
            for c in w.descendants(control_type="MenuItem"):
                try:
                    nm = (c.element_info.name or "").strip()
                except Exception:
                    continue
                if nm:
                    out.append((nm, c))
        except Exception:
            continue
    return out


def _find(items, *needles: str):
    for nm, ctrl in items:
        low = nm.lower()
        if all(n in low for n in needles):
            return ctrl
    return None


def start_batch(pid: int, *, log_dir: Path | None = None) -> list[str]:
    """Click File > Plug-in Extras > Run UTC RAW batch in a running Lightroom.

    Returns notes worth showing the operator. Raises `CannotStart` rather than
    returning quietly: a batch that was never started is otherwise
    indistinguishable from a very slow one.
    """
    notes: list[str] = []
    pwa = _pywinauto()

    win = _main_window(pwa, pid, _READY_TIMEOUT)
    try:
        win.set_focus()
    except Exception as ex:
        raise CannotStart(
            f"Lightroom's window would not come to the front ({ex}). "
            f"The screen may be locked.") from ex
    time.sleep(1.0)

    # Library menu items live under File > Plug-in Extras, and Lightroom
    # populates that submenu per module. Lightroom reopens in whichever module
    # it was last used in -- often Develop -- so switch to the Library grid
    # first with G.
    try:
        pwa.keyboard.send_keys("g")
        time.sleep(2.5)
    except Exception:
        pass

    file_menu = _find(_menu_items(pwa, pid), "file")
    if file_menu is None:
        raise CannotStart("Lightroom's File menu could not be found")
    file_menu.click_input()
    time.sleep(1.5)

    deadline = time.monotonic() + _MENU_TIMEOUT
    extras = None
    while time.monotonic() < deadline and extras is None:
        extras = _find(_menu_items(pwa, pid), "plug-in extras")
        if extras is None:
            time.sleep(0.5)
    if extras is None:
        _escape(pwa)
        raise CannotStart(
            "the 'Plug-in Extras' submenu never appeared. The plug-in may not "
            "be enabled in Lightroom's Plug-in Manager.")

    # Hover rather than click: clicking a submenu parent can close the menu.
    try:
        extras.click_input()
    except Exception:
        pass
    time.sleep(1.5)

    deadline = time.monotonic() + _MENU_TIMEOUT
    item = None
    while time.monotonic() < deadline and item is None:
        item = _find(_menu_items(pwa, pid), MENU_ITEM.lower())
        if item is None:
            item = _find(_menu_items(pwa, pid), "utc", "batch")
        if item is None:
            time.sleep(0.5)
    if item is None:
        if log_dir:
            _dump(pwa, pid, Path(log_dir) / "menu_items.txt")
        _escape(pwa)
        raise CannotStart(
            f"'{MENU_ITEM}' was not in Plug-in Extras. The installed plug-in "
            f"may be an older version than this build of UTC expects.")

    item.click_input()
    notes.append(f"started the batch from File > Plug-in Extras > {MENU_ITEM}")
    return notes


def click_path(pid: int, *needles: str, timeout: float = _MENU_TIMEOUT,
               log_dir: Path | None = None) -> bool:
    """Walk Lightroom's menus, clicking each level in turn.

    `click_path(pid, "photo", "enhance")` opens the Photo menu and clicks the
    Enhance entry. Menus are the one part of Lightroom's interface that is
    properly exposed to the accessibility layer, which makes this far steadier
    than sending a keyboard shortcut and hoping the right window had focus.
    """
    pwa = _pywinauto()
    for i, needle in enumerate(needles):
        deadline = time.monotonic() + (timeout if i else 5.0)
        ctrl = None
        while time.monotonic() < deadline and ctrl is None:
            ctrl = _find(_menu_items(pwa, pid), needle.lower())
            if ctrl is None:
                time.sleep(0.4)
        if ctrl is None:
            # Record what was on offer instead. A menu whose wording has
            # drifted is otherwise indistinguishable from one that never
            # opened.
            if log_dir:
                _dump(pwa, pid, Path(log_dir) / f"menu_{needle}_missing.txt")
            _escape(pwa)
            return False
        # A disabled entry still answers to a click and does nothing, which
        # reads downstream as "the dialog never opened". Enhance is disabled
        # whenever no photos are selected.
        try:
            if ctrl.is_enabled() is False:
                if log_dir:
                    (Path(log_dir) / f"menu_{needle}_disabled.txt").write_text(
                        f"{needle}: present but disabled", encoding="utf-8")
                _escape(pwa)
                return False
        except Exception:
            pass
        try:
            ctrl.click_input()
        except Exception:
            _escape(pwa)
            return False
        time.sleep(1.5)
    return True


def _escape(pwa) -> None:
    try:
        pwa.keyboard.send_keys("{ESC}{ESC}")
    except Exception:
        pass


def _dump(pwa, pid: int, out: Path) -> None:
    """Record what was on the menu, for when the title has drifted."""
    try:
        names = sorted({nm for nm, _ in _menu_items(pwa, pid)})
        out.write_text("\n".join(names), encoding="utf-8", errors="replace")
    except Exception:
        pass
