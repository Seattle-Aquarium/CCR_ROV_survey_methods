"""
Main application for the Lutris Lighting System.
Controls 4 SeaLite LED lights via PWM input from BlueROV servo output,
and reports what the lights say about themselves.

Telemetry (ccr.lights/1, see ../TELEMETRY.md) goes out on a second USB serial
port when one could be set up (datalink.py), otherwise on the console with an
"@L " prefix. The BlueOS Light Monitor extension reads it, records it, and
forwards a summary into MAVLink -- so it lands in the tlog on BlueOS 1.4 and
the mcap on 1.5.

Steering is unchanged from the PWM-only firmware: the pilot's servo output is
measured, filtered and turned into a LOUT command. What is new is that on the
control loop's quiet turns the controller asks one light one question (see
poller.py), and reads a power monitor if there is one.
"""

import gc
import os
import sys
import time

from machine import I2C, Pin

# Add lib to path - works for both mpremote mount (/remote) and deployed (/)
if "/remote" in os.getcwd():
    sys.path.insert(0, "/remote/lib")
else:
    sys.path.insert(0, "/lib")

import board
import telemetry
from ina2xx import find as find_power_monitors
from lightbus import LightBus
from poller import Poller
from pydspl_seasense.sealite import Sealite
from uart_wrapper import UARTWrapper
from pwm_reader import PWMReader

FIRMWARE = "lutris-lights 2.0.0-telemetry"


class LightingController:
    """
    Main controller for the Lutris lighting system.

    Manages 4 SeaLite LED lights on a shared RS-485 bus, addressed
    individually (1-4) via the SeaSense protocol. Light brightness is
    controlled by a PWM input from the BlueROV.

    Pins come from board.py.  Both the Pico (original wiring, Keenan's PCB) and the
    QT Py RP2040 now on Lutris are recognised automatically at start-up.
    """

    # ArduSub servo PWM range (microseconds)
    PWM_MIN_US = 1105
    PWM_MAX_US = 1900

    # Accept-range for raw pulse samples. Pulses outside this are treated
    # as EMI glitches and dropped by the PWM reader's ISR.
    PWM_VALID_MIN_US = 900
    PWM_VALID_MAX_US = 2100

    # Median filter window over raw samples, to reject single-sample
    # outliers that pass the ISR range gate.
    MEDIAN_WINDOW = 9

    # Signal loss timeout - lights off if no PWM edges for this long
    SIGNAL_TIMEOUT_MS = 1000

    # Minimum brightness change (%) before updating lights (deadband)
    LEVEL_DEADBAND = 5

    # Control loop interval
    LOOP_INTERVAL_MS = 50

    # Pause after each LOUT command so a light's ACK does not collide with
    # the next command on the half-duplex bus (CCR_ROV_survey_methods#43:
    # 100 ms was clean, 50 ms was clean, 10 ms corrupted).
    INTER_LIGHT_MS = 50

    # Status print interval (the human-readable console line)
    STATUS_INTERVAL_MS = 2000

    # Telemetry intervals, all in ms
    CTL_INTERVAL_MS = 500  # PWM and commanded level
    POWER_INTERVAL_MS = 250  # power monitor(s), when fitted
    HELLO_INTERVAL_MS = 60000  # who and what this is, for late readers
    STATS_INTERVAL_MS = 1000  # the controller's own health

    # Ask the lights how they are on quiet turns of the loop. Off = PWM-only
    # behaviour, exactly as before telemetry.
    DO_POLL_LIGHTS = True

    # Re-send a light's level when it reports something other than what it
    # was last told. Keenan Johnson's suggestion (CCR_development#34, 22 May
    # 2026) for lights that miss a command or restart at a stale level.
    # **Off** until the team decides it wants the controller correcting the
    # lights on its own: telemetry should first show how often it happens.
    RESYNC_LEVELS = False
    RESYNC_AFTER_MISMATCHES = 3

    # Power monitor (INA226 / INA238). None = shunt unknown: report voltage
    # only. Set to the fitted shunt's resistance to get current and power.
    INA_SHUNT_OHMS = None
    INA_MAX_CURRENT = 20.0
    # Latch a bus under-voltage between reads. Chosen from the first reading:
    # a monitor reading > 18 V is on the 24 V light rail, otherwise on the
    # vehicle's ~13 V side.
    INA_UV_24V_SIDE = 20.0
    INA_UV_13V_SIDE = 10.0

    # Serial configuration
    BAUDRATE = 9600

    # Light addresses on the shared bus
    LIGHT_ADDRESSES = [1, 2, 3, 4]

    def __init__(self):
        """Initialize the lighting controller."""
        self.board = board.detect()
        self._uart = None
        self._lights = []
        self._monitors = []
        self._monitor_notes = []
        self._pwm_reader = None
        self._current_level = 0
        self._mismatch = {}

        self._init_telemetry()
        self._init_uart()
        self._init_lights()
        self._init_power_monitor()
        self._init_pwm_reader()

        self._bus = LightBus(self._uart, local_echo=self.board["local_echo"])
        self._poller = Poller(self._bus, self.LIGHT_ADDRESSES, self._on_poll)
        self._poller.enabled = self.DO_POLL_LIGHTS

        self._emit_hello()

    # ------------------------------------------------------------------
    #  telemetry
    # ------------------------------------------------------------------

    def _init_telemetry(self):
        port, why = None, ""
        try:
            import datalink

            port, why = datalink.port, datalink.error
        except ImportError:
            why = "datalink.py missing"

        self.tel = telemetry.Sink(port=port)
        self._datalink_note = why

        print(
            "Telemetry:",
            "second USB port" if port is not None else "console (@L lines); " + why,
        )

    def _emit_hello(self):
        try:
            import machine

            reset = machine.reset_cause()
        except Exception:
            reset = None
        self.tel.emit(
            "hello",
            proto=telemetry.PROTOCOL,
            fw=FIRMWARE,
            board=self.board["name"],
            machine=board.machine_name(),
            mpy=".".join(str(x) for x in sys.implementation.version[:3]),
            reset=reset,
            port=self.tel.mode,
            datalink=self._datalink_note,
            lights=self.LIGHT_ADDRESSES,
            power=[m.describe() for _l, m in self._monitors],
            power_notes=self._monitor_notes,
            poll=self._poller.enabled if hasattr(self, "_poller") else self.POLL_LIGHTS,
            resync=self.RESYNC_LEVELS,
            deadband=self.LEVEL_DEADBAND,
        )

    def _on_poll(self, kind, **fields):
        """
        Got data from a light.

        Dump it to telemetry, then process it (if it's an "lout")
        """

        self.tel.emit(kind, **fields)

        if fields.get("cmd") != "lout" or not fields.get("ok"):
            return

        addr = fields.get("id")

        try:
            said = int(str(fields.get("raw", "")).strip().split()[0])
        except (ValueError, IndexError):
            return

        if said == self._current_level:
            self._mismatch[addr] = 0
            return

        n = self._mismatch.get(addr, 0) + 1
        self._mismatch[addr] = n

        if self.RESYNC_LEVELS and n >= self.RESYNC_AFTER_MISMATCHES:
            self.set_light_level(addr, self._current_level, why="resync")
            self._mismatch[addr] = 0

    # ------------------------------------------------------------------
    #  hardware
    # ------------------------------------------------------------------

    def _init_uart(self):
        """Initialize the shared RS-485 UART."""
        b = self.board
        kw = {}
        if b["uart_tx"] is not None and b["uart_rx"] is not None:
            kw = {"tx": Pin(b["uart_tx"]), "rx": Pin(b["uart_rx"])}
        self._uart = UARTWrapper(
            uart_id=b["uart_id"], baudrate=self.BAUDRATE, timeout=2000, **kw
        )
        print(f"UART{b['uart_id']} initialized ({b['name']} pins)")

    def _init_lights(self):
        """Initialize SeaLite objects for each light."""
        for addr in self.LIGHT_ADDRESSES:
            light = Sealite(
                address=addr, max_level=100, expect_response=self.board["local_echo"]
            )
            self._lights.append(light)
        print(
            f"Initialized {len(self._lights)} SeaLite lights (addresses {self.LIGHT_ADDRESSES})"
        )

    def _init_power_monitor(self):
        """Find any INA226/INA238 on I2C. None fitted is normal (Lutris, Oct 2026)."""
        b = self.board
        try:
            i2c = I2C(
                b["i2c_id"], sda=Pin(b["i2c_sda"]), scl=Pin(b["i2c_scl"]), freq=400000
            )
            self._monitors, self._monitor_notes = find_power_monitors(
                i2c, shunt_ohms=self.INA_SHUNT_OHMS, max_current=self.INA_MAX_CURRENT
            )
        except Exception as e:
            self._monitors, self._monitor_notes = [], ["I2C: " + repr(e)]
        for label, mon in self._monitors:
            try:
                v = mon.read()["v"]
                mon.uv_volts = (
                    self.INA_UV_24V_SIDE if v > 18.0 else self.INA_UV_13V_SIDE
                )
                mon.configure()
            except Exception as e:
                self._monitor_notes.append(f"{label}: first read failed: {repr(e)}")
        print(
            "Power monitors:",
            [(label, m.chip, hex(m.addr)) for label, m in self._monitors] or "none",
            self._monitor_notes,
        )

    def _init_pwm_reader(self):
        """Initialize PWM input reader."""
        self._pwm_reader = PWMReader(
            self.board["pwm_pin"],
            valid_min_us=self.PWM_VALID_MIN_US,
            valid_max_us=self.PWM_VALID_MAX_US,
        )
        print(f"PWM input initialized: GPIO{self.board['pwm_pin']}")
        print(
            f"  PWM range: {self.PWM_MIN_US}-{self.PWM_MAX_US} us -> 0-100% brightness"
        )
        print(
            f"  Accept range: {self.PWM_VALID_MIN_US}-{self.PWM_VALID_MAX_US} us, median window: {self.MEDIAN_WINDOW}"
        )

    # ------------------------------------------------------------------
    #  public-facing commands
    # ------------------------------------------------------------------

    def pwm_to_level(self, pulse_width_us):
        """Map PWM pulse width (us) to light level (0-100)."""
        if pulse_width_us <= self.PWM_MIN_US:
            return 0
        if pulse_width_us >= self.PWM_MAX_US:
            return 100
        return int(
            (pulse_width_us - self.PWM_MIN_US)
            * 100
            / (self.PWM_MAX_US - self.PWM_MIN_US)
        )

    def set_light_level(self, addr, level, why="manual"):
        """
        Set brightness level for a specific light.

        Args:
            addr: Light address
            level: Brightness level (0-100)

        Returns:
            True on success, False on failure
        """
        if addr not in self.LIGHT_ADDRESSES:
            print(f"Invalid light address: {addr}")
            return False

        try:
            ok, _raw, _err, rtt = self._bus.set_level(addr, level)
            self.tel.emit("set", id=addr, lvl=level, ok=ok, rtt=rtt)
            return ok
        except ValueError as e:
            print(f"Error setting light {addr}: {e}")
            self.tel.emit(
                "log", lvl="error", msg=f"set light value error {addr}: {repr(e)}"
            )
            return False
        except Exception as e:
            print(f"Error setting light {addr}: {e}")
            self.tel.emit("log", lvl="error", msg=f"set light {addr}: {repr(e)}")
            return False

    def set_all_lights(self, level, why="manual"):
        """Set brightness level for all lights."""
        success = 0
        for addr in self.LIGHT_ADDRESSES:
            if self.set_light_level(addr, level, why=why):
                success += 1
            # Let each light ACK before the next command (PR #43). The pause
            # is arbitrary: it could be tuned to the lights' actual response
            # time, or the code that catches their replies made more robust.
            time.sleep_ms(self.INTER_LIGHT_MS)

        # Ask each light what it now thinks, ahead of the rotation.
        for addr in reversed(self.LIGHT_ADDRESSES):
            self._poller.ask_now(addr, "lout")
        return success

    def all_off(self):
        """Turn off all lights."""
        return self.set_all_lights(0, why="off")

    def get_light_level(self, addr):
        """Read current brightness level from a light."""
        if addr not in self.LIGHT_ADDRESSES:
            return None

        try:
            return self._poller.ask_now(addr, "lout")
        except Exception as e:
            print(f"Error reading light {addr}: {e}")
            return None

    def get_light_temperature(self, addr):
        """Read temperature from a light."""
        if addr not in self.LIGHT_ADDRESSES:
            return None

        try:
            return self._poller.ask_new(addr, "temp")
        except Exception as e:
            print(f"Error reading temp from light {addr}: {e}")
            return None

    def read_power(self):
        """Read every power monitor: {label: {v, shunt_mv, [a, w, t, uv]}}."""
        out = {}
        for label, mon in self._monitors:
            try:
                out[label] = mon.read()
            except Exception as e:
                out[label] = {"error": repr(e)}
        return out or None

    def status(self):
        """Get status of all lights and power consumption."""
        return {
            "lights": [
                {"index": i, "level": self._lights[i].level}
                for i in range(len(self._lights))
            ],
            "power": self.read_power(),
            "telemetry": {
                "port": self.tel.mode,
                "sent": self.tel.sent,
                "dropped": self.tel.dropped,
            },
            "bus": {
                "asked": self._bus.asked,
                "answered": self._bus.answered,
                "failed": self._bus.failed,
            },
        }

    # ------------------------------------------------------------------
    #  the loop
    # ------------------------------------------------------------------

    def _emit_power(self):
        for label, mon in self._monitors:
            try:
                r = mon.read()
                self.tel.emit("pwr", ch=label, chip=mon.chip, addr=mon.addr, **r)
            except Exception as e:
                self.tel.emit(
                    "pwr", ch=label, chip=mon.chip, addr=mon.addr, err=repr(e)
                )

    def run(self):
        """Main control loop. Reads PWM input and updates light brightness.

        Runs until KeyboardInterrupt (Ctrl-C). Turns lights off on exit.
        """
        print("PWM control loop running (Ctrl-C to stop)")

        now = time.ticks_ms()
        last_status_ms = last_ctl_ms = last_power_ms = now
        last_hello_ms = last_stats_ms = now
        pw_samples = []
        pw_filtered = 0
        loop_max_ms = 0

        try:
            while True:
                t_loop = time.ticks_ms()
                signal_age = self._pwm_reader.signal_age_ms
                signal_lost = signal_age < 0 or signal_age > self.SIGNAL_TIMEOUT_MS

                if signal_lost:
                    pw_samples = []
                    pw_filtered = 0
                    target_level = 0
                else:
                    pw_samples.append(self._pwm_reader.pulse_width_us)
                    if len(pw_samples) > self.MEDIAN_WINDOW:
                        pw_samples.pop(0)
                    pw_filtered = sorted(pw_samples)[len(pw_samples) // 2]
                    target_level = self.pwm_to_level(pw_filtered)

                # Only update lights if level changed beyond deadband
                changed = abs(target_level - self._current_level) >= self.LEVEL_DEADBAND
                if changed:
                    self._current_level = target_level
                    self.set_all_lights(target_level, why="pwm")

                now_ms = time.ticks_ms()
                if (
                    changed
                    or time.ticks_diff(now_ms, last_ctl_ms) >= self.CTL_INTERVAL_MS
                ):
                    self.tel.emit(
                        "ctl",
                        pwm=pw_filtered,
                        age=signal_age,
                        sig=0 if signal_lost else 1,
                        cmd=self._current_level,
                        tgt=target_level,
                        rej=self._pwm_reader.rejected,
                    )
                    last_ctl_ms = now_ms

                if (
                    self._monitors
                    and time.ticks_diff(now_ms, last_power_ms) >= self.POWER_INTERVAL_MS
                ):
                    self._emit_power()
                    last_power_ms = now_ms

                # If the light levels weren't changed, ask one light one question.
                if not changed:
                    self._poller.step()

                if time.ticks_diff(now_ms, last_hello_ms) >= self.HELLO_INTERVAL_MS:
                    self._emit_hello()
                    last_hello_ms = now_ms

                if time.ticks_diff(now_ms, last_stats_ms) >= self.STATS_INTERVAL_MS:
                    self.tel.emit(
                        "stats",
                        sent=self.tel.sent,
                        drop=self.tel.dropped,
                        unread=self.tel.unread,
                        asked=self._bus.asked,
                        answered=self._bus.answered,
                        failed=self._bus.failed,
                        loop_max=loop_max_ms,
                        mem=gc.mem_free(),
                        passes=self._poller.passes,
                    )
                    loop_max_ms = 0
                    last_stats_ms = now_ms

                # Periodic status output (human-readable, on the console)
                if time.ticks_diff(now_ms, last_status_ms) >= self.STATUS_INTERVAL_MS:
                    if signal_lost:
                        print(f"PWM: NO SIGNAL | Level: {self._current_level}%")
                    else:
                        print(f"PWM: {pw_filtered} us | Level: {self._current_level}%")
                    last_status_ms = now_ms

                spent = time.ticks_diff(time.ticks_ms(), t_loop)
                loop_max_ms = max(loop_max_ms, spent)
                if spent < self.LOOP_INTERVAL_MS:
                    time.sleep_ms(self.LOOP_INTERVAL_MS - spent)

        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.all_off()
            self._pwm_reader.deinit()
            self.tel.emit(
                "log", lvl="warn", msg="control loop stopped; lights turned off"
            )
            print("Lights off. PWM reader disabled.")


def main():
    """Main entry point. Initializes controller and starts PWM control loop."""

    print("Lutris Lighting System Starting...")
    print("-" * 40)

    controller = LightingController()

    print("-" * 40)
    print("REPL commands (if loop is stopped):")
    print("  c.set_all_lights(50)  # Manual override")
    print("  c.all_off()")
    print("  c.read_power()")
    print("  c.status()")
    print("-" * 40)

    controller.run()

    return controller


if __name__ == "__main__":
    main()
