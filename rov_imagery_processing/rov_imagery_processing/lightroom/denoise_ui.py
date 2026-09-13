"""
Denoise one photo, then synchronise it onto the rest.

AI Denoise has no SDK entry point, and as of Lightroom Classic 14.5 it has no
batch entry point either: the Enhance dialog is gone, ``Photo > Enhance...``
only shows a notice saying the feature "moved to the Detail panel", and that
panel acts on the one photo being edited. Driving it once per photo would be
slow and fragile.

So this does what an operator does. Develop one photo properly -- tick Denoise
in the Detail panel and let it compute -- then select the whole folder and
press **Sync**, which carries the develop settings across in one operation.
One panel interaction and one dialog, whatever the size of the batch.

Three things keep it honest:

* **Every step is verified against the catalog, not the screen.** Denoise
  writes a per-photo record, and the caller supplies a `wait_denoised` that
  watches for it. A click that appeared to work but did nothing is caught.
* **Every run leaves evidence.** Control trees are dumped to the run
  directory, so a panel that has moved again names itself.
* **Silence is never success.** Anything unreachable raises rather than
  letting the batch export un-denoised frames and call it done.

The crop is applied through the SDK before any of this and re-checked after,
because Sync carries a crop too and the delivered pixel size is not
negotiable.
"""

from __future__ import annotations

import time
from pathlib import Path

#: How long to wait for the Detail panel's Denoise control to appear.
_PANEL_TIMEOUT = 60.0

#: How long one photo's Denoise may take before we call it stuck. Measured on
#: this hardware: five frames finish inside a minute, so a long wait here only
#: ever delayed the report of something that was never going to happen.
_ONE_PHOTO_TIMEOUT = 180.0

#: How long the whole synchronised batch may take.
_SYNC_TIMEOUT = 60 * 60

_NOTICE_HINTS = ("moved to the detail panel", "have moved")


class DenoiseUnavailable(RuntimeError):
    """The Denoise controls could not be driven, with a readable reason."""


def _pywinauto():
    try:
        import pywinauto
        import pywinauto.keyboard  # noqa: F401
    except Exception as ex:
        raise DenoiseUnavailable(
            f"the pywinauto package is not usable ({ex}). "
            f"Run:  python -m pip install pywinauto") from ex
    return pywinauto


def _texts(ctrl) -> str:
    try:
        info = ctrl.element_info
        return f"{info.name or ''} {info.control_type or ''}".lower()
    except Exception:
        return ""


def _window_for(pwa, pid: int):
    for w in pwa.Desktop(backend="uia").windows():
        try:
            if w.element_info.process_id == pid and w.is_visible():
                return w
        except Exception:
            continue
    return None


def _controls(pwa, pid: int, kind: str | None = None):
    """Every control of `kind` currently on screen for this process."""
    out = []
    for w in pwa.Desktop(backend="uia").windows():
        try:
            if w.element_info.process_id != pid:
                continue
            out.extend(w.descendants(control_type=kind) if kind
                       else w.descendants())
        except Exception:
            continue
    return out


def _named(controls, *needles: str, exact: bool = False):
    for c in controls:
        try:
            nm = (c.element_info.name or "").strip().lower()
        except Exception:
            continue
        if not nm:
            continue
        if exact:
            if nm in needles:
                return c
        elif all(n in nm for n in needles):
            return c
    return None


def dump(pwa, pid: int, out: Path, limit: int = 600) -> None:
    """Write down what is on screen -- what a changed UI is diagnosed from."""
    rows = []
    try:
        for i, c in enumerate(_controls(pwa, pid)):
            if i >= limit:
                rows.append(f"... more than {limit} controls, truncated")
                break
            try:
                info = c.element_info
                nm = (info.name or "").strip()
                if nm:
                    rows.append(f"{info.control_type:<14} {nm[:90]!r}")
            except Exception:
                continue
    except Exception as ex:
        rows.append(f"<could not walk: {ex}>")
    try:
        Path(out).write_text(chr(10).join(rows), encoding="utf-8",
                             errors="replace")
    except OSError:
        pass


def _dismiss_notices(pwa, pid: int, notes: list[str]) -> None:
    """Clear Lightroom's 'Enhance has moved' notice, which blocks everything."""
    try:
        for w in pwa.Desktop(backend="uia").windows():
            try:
                if w.element_info.process_id != pid:
                    continue
                blob = _texts(w)
                for t in w.descendants(control_type="Text"):
                    blob += " " + (t.element_info.name or "").lower()
            except Exception:
                continue
            if not any(h in blob for h in _NOTICE_HINTS):
                continue
            ok = _named(w.descendants(control_type="Button"), "ok", exact=True)
            try:
                if ok is not None:
                    ok.click_input()
                else:
                    w.set_focus()
                    pwa.keyboard.send_keys("{ENTER}")
                notes.append("dismissed the 'Enhance has moved' notice")
                time.sleep(1.0)
            except Exception:
                pass
    except Exception:
        pass


# --------------------------------------------------------------------------
#  One photo
# --------------------------------------------------------------------------

def _toggle_state(box):
    try:
        return box.get_toggle_state()
    except Exception:
        return None


def _expand_detail(pwa, pid: int, notes: list[str]) -> None:
    """Open the Detail panel if it is collapsed.

    A collapsed panel still has its controls in the accessibility tree, so the
    Denoise checkbox can be found, clicked, and quietly do nothing.
    """
    box = _named(_controls(pwa, pid, "CheckBox"), "denoise")
    try:
        if box is not None and box.is_visible() and box.rectangle().height() > 0:
            return
    except Exception:
        pass
    header = _named(_controls(pwa, pid, "Text"), "detail", exact=True)
    if header is None:
        return
    try:
        header.click_input()
        notes.append("expanded the Detail panel")
        time.sleep(1.5)
    except Exception:
        pass


def _tick_denoise(pwa, pid: int, notes: list[str]) -> None:
    """Turn Denoise on, and prove it went on.

    Lightroom draws its own controls; a checkbox that reports itself to the
    accessibility layer still may not respond to a click at its centre, and a
    click that lands on the label rather than the box does nothing at all. The
    previous version clicked once and assumed -- which meant a batch waiting
    ten minutes for a computation that had never been asked for. So: try each
    way of pressing it in turn, and read the state back after every attempt.
    """
    deadline = time.monotonic() + _PANEL_TIMEOUT
    box = None
    while time.monotonic() < deadline and box is None:
        box = _named(_controls(pwa, pid, "CheckBox"), "denoise")
        if box is None:
            time.sleep(1.0)
    if box is None:
        raise DenoiseUnavailable(
            "the Detail panel's Denoise control could not be found. In "
            "Lightroom 14.5 it lives in Develop > Detail; if it has moved "
            "again, the dumps in this run's folder show what is there now.")

    if _toggle_state(box) == 1:
        notes.append("Denoise was already on for the first photo")
        return

    _expand_detail(pwa, pid, notes)
    box = _named(_controls(pwa, pid, "CheckBox"), "denoise") or box

    def by_pattern():
        box.toggle()

    def by_click():
        box.click_input()

    def by_glyph():
        # The tick box itself sits at the left of the row; the rest of the
        # width is label, which is not a hit target.
        r = box.rectangle()
        box.click_input(coords=(6, r.height() // 2), absolute=False)

    def by_space():
        box.set_focus()
        pwa.keyboard.send_keys("{SPACE}")

    for name, attempt in (("toggle pattern", by_pattern),
                          ("click", by_click),
                          ("click on the tick box", by_glyph),
                          ("space key", by_space)):
        try:
            attempt()
        except Exception:
            continue
        time.sleep(2.0)
        fresh = _named(_controls(pwa, pid, "CheckBox"), "denoise") or box
        if _toggle_state(fresh) == 1:
            notes.append(f"Denoise turned on via {name}")
            time.sleep(1.5)
            return

    raise DenoiseUnavailable(
        "the Denoise checkbox in Develop > Detail would not turn on. It was "
        "found and pressed four different ways and still reads as off, so "
        "nothing was denoised. Turning Denoise on by hand for the first photo "
        "and re-running would work around it.")


# --------------------------------------------------------------------------
#  The rest, by Sync
# --------------------------------------------------------------------------

def _sync_to_all(pwa, pid: int, notes: list[str],
                 log_dir: Path | None) -> None:
    """Select everything and push the first photo's settings onto it."""
    win = _window_for(pwa, pid)
    if win is not None:
        try:
            win.set_focus()
        except Exception:
            pass

    # Select the whole filmstrip. The developed photo stays the active one,
    # which is what Sync copies *from*.
    try:
        pwa.keyboard.send_keys("^a")
        time.sleep(1.5)
    except Exception:
        pass

    button = _named(_controls(pwa, pid, "Button"), "sync")
    if button is None:
        if log_dir:
            dump(pwa, pid, Path(log_dir) / "no_sync_button.txt")
        raise DenoiseUnavailable(
            "the Sync button could not be found in the Develop module")
    try:
        button.click_input()
    except Exception as ex:
        raise DenoiseUnavailable(
            f"the Sync button would not click ({ex})") from ex
    notes.append("pressed Sync with the whole folder selected")
    time.sleep(2.5)

    # The Synchronize Settings dialog. Check everything: the source photo
    # carries exactly the crop, chromatic aberration and Denoise we want, and
    # nothing else about it has been touched.
    deadline = time.monotonic() + 30.0
    check_all = None
    while time.monotonic() < deadline and check_all is None:
        check_all = _named(_controls(pwa, pid, "Button"), "check all")
        if check_all is None:
            time.sleep(0.5)
    if check_all is not None:
        try:
            check_all.click_input()
            notes.append("ticked every setting in Synchronize Settings")
            time.sleep(1.0)
        except Exception:
            pass
    else:
        notes.append("no 'Check All' button found; used the dialog's "
                     "remembered selection")

    if log_dir:
        dump(pwa, pid, Path(log_dir) / "sync_dialog.txt")

    confirm = _named(_controls(pwa, pid, "Button"), "synchronize")
    if confirm is None:
        raise DenoiseUnavailable(
            "the Synchronize button could not be found; see sync_dialog.txt "
            "in this run's folder")
    try:
        confirm.click_input()
    except Exception as ex:
        raise DenoiseUnavailable(
            f"the Synchronize button would not click ({ex})") from ex
    notes.append("confirmed Synchronize")
    time.sleep(2.0)


# --------------------------------------------------------------------------
#  The sequence
# --------------------------------------------------------------------------

def denoise_all(pid: int, *, total: int, wait_denoised,
                amount: int = 50, log_dir: Path | None = None,
                cancelled=None) -> list[str]:
    """Denoise the first photo, then synchronise onto the whole folder.

    `wait_denoised(n, timeout)` must block until at least `n` photos report
    denoised in the catalog, returning False on timeout. Screen state is never
    taken as proof; the catalog is.
    """
    notes: list[str] = []
    pwa = _pywinauto()

    win = _window_for(pwa, pid)
    if win is None:
        raise DenoiseUnavailable("Lightroom's window could not be found")
    try:
        win.set_focus()
    except Exception as ex:
        raise DenoiseUnavailable(
            f"Lightroom's window would not come to the front ({ex}). "
            f"The screen may be locked.") from ex
    time.sleep(1.0)

    _dismiss_notices(pwa, pid, notes)

    # Develop the first photo. 'd' switches module; the plugin has already
    # made the first imported photo the active one.
    try:
        pwa.keyboard.send_keys("d")
        time.sleep(4.0)
    except Exception:
        pass
    _dismiss_notices(pwa, pid, notes)

    if log_dir:
        dump(pwa, pid, Path(log_dir) / "develop_before_denoise.txt")

    _tick_denoise(pwa, pid, notes)

    # Denoise is a real computation. Wait for the catalog to record it rather
    # than assuming the click was enough.
    if not wait_denoised(1, _ONE_PHOTO_TIMEOUT):
        if log_dir:
            dump(pwa, pid, Path(log_dir) / "develop_after_denoise.txt")
        raise DenoiseUnavailable(
            f"the first photo was still not denoised after "
            f"{int(_ONE_PHOTO_TIMEOUT / 60)} minutes")
    notes.append("first photo denoised")

    if cancelled is not None and cancelled():
        return notes
    if total <= 1:
        return notes

    _sync_to_all(pwa, pid, notes, log_dir)

    if not wait_denoised(total, _SYNC_TIMEOUT):
        raise DenoiseUnavailable(
            "Synchronize did not carry Denoise onto every photo. The catalog "
            "still shows some frames un-denoised, so the export was stopped "
            "rather than writing frames that only look processed.")
    notes.append(f"Denoise synchronised onto all {total} photos")
    return notes
