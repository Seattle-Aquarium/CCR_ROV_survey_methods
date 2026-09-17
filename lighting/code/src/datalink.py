"""
Creates a second USB serial port for telemetry.

The usb-device-cdc module can be configured to create a second
virtual serial port for telemetry.  This separates the read-only telemetry
streaam from the primary Micropython REPL and prevents us from accidentally
interrupting the REPL and stopping the program.

The Micropython port will typically appear as /dev/ttyACM0
The telemetry port will typically appear as   /dev/ttyACM1

The second port is written without waiting (`timeout=0`), so a reader that
stops reading costs telemetry lines -- counted -- instead of stalling the
light control loop.

Needs MicroPython 1.23 or newer (`machine.USBDevice`) and the micropython-lib
package, installed once with the controller on the bench::

    mpremote mip install usb-device-cdc

Without either, `start()` notes why and `port` stays None; main.py then sends
telemetry on the console instead, each line prefixed "@L " so a reader can
pick it out of the console text.

**The safety valve.** The box on Lutris is sealed, and a controller whose USB
stopped working would need opening to recover. So `start()` is guarded:

* a file `datalink.off` on the controller turns the port off entirely;
* `datalink.try` is written before the port is set up and removed as soon as
  setting it up returns. If a boot finds it still there, the previous attempt
  never returned (a crash or hang mid-way), so this boot leaves the port off
  and renames the marker `datalink.failed`, which keeps it off until someone
  deletes that file with mpremote.

A soft reset (which mpremote does) removes the second port and the device
re-enumerates; boot.py runs again on a soft reset and brings it back. The
BlueOS extension reconnects on its own. `mpremote run` may fail on the first
try for the same reason, and succeed on the second -- that is MicroPython's
documented behaviour, not a fault.
"""

import os

port = None          # the usb.device.cdc.CDCInterface, once started
error = ""           # why there is no port, in words

OFF_FILE = "datalink.off"
TRY_FILE = "datalink.try"
FAILED_FILE = "datalink.failed"

ERROR_FILE = "datalink_error.txt"


def _exists(name):
    try:
        os.stat(name)
        return True
    except OSError:
        return False


def _write(name, text):
    try:
        with open(name, "w") as f:
            f.write(text)
    except OSError:
        pass

def _write_error(text):
    _write(ERROR_FILE,text)


def _remove(name):
    try:
        os.remove(name)
    except OSError:
        pass


def start():
    """Bring up the telemetry port. Never raises."""
    global port, error

    if port is not None:
        return port

    if _exists(OFF_FILE):
        error = "turned off (datalink.off is present)"
        _write_error(error)
        return None

    if _exists(FAILED_FILE):
        error = "left off: an earlier attempt never finished (delete datalink.failed to retry)"
        _write_error(error)
        return None

    if _exists(TRY_FILE):
        try:
            os.rename(TRY_FILE, FAILED_FILE)
        except OSError:
            pass
        error = "left off: the previous attempt never finished"
        _write_error(error)
        return None
    try:
        import usb.device
        from usb.device.cdc import CDCInterface
    except ImportError as ex:
        error = "usb-device-cdc not installed (mpremote mip install usb-device-cdc): " + str(ex)
        _write_error(error)
        return None
    
    _write(TRY_FILE, "setting up the telemetry port\n")

    try:
        cdc = CDCInterface()
        cdc.init(timeout=0)                      # never block the light loop
        usb.device.get().init(cdc, builtin_driver=True)
        port = cdc
        error = ""
        _remove(ERROR_FILE)
    except Exception as ex:                      # noqa: BLE001 - anything at all
        port = None
        error = "could not start: " + repr(ex)
        _write_error(error)
        _restore_console()

    _remove(TRY_FILE)

    return port


def _restore_console():
    """Put the console back as the only USB interface, after a failed start."""
    try:
        import machine
        dev = machine.USBDevice()
        dev.active(False)
        dev.builtin_driver = dev.BUILTIN_DEFAULT
        dev.active(True)
    except Exception:
        pass


def is_open():
    """True when the host has the telemetry port open."""
    try:
        return port is not None and port.is_open()
    except Exception:
        return False
