"""
Main application for the Lutris Lighting System.
Controls 4 SeaLite LED lights via PWM input from BlueROV servo output.
Monitors power via INA238.
"""

import time
from machine import Pin, I2C

import sys
import os

# Add lib to path - works for both mpremote mount (/remote) and deployed (/)
if '/remote' in os.getcwd():
    sys.path.insert(0, '/remote/lib')
else:
    sys.path.insert(0, '/lib')

from uart_wrapper import UARTWrapper
from ina238 import INA238
from pydspl_seasense.sealite import Sealite


class PWMReader:
    """Reads PWM pulse width using pin edge interrupts.

    Designed for measuring ArduSub servo output signals (~50 Hz, 1100-1900 us).
    Tracks last edge time for signal-loss detection. Pulses outside
    [valid_min_us, valid_max_us] are dropped as EMI glitches; the prior
    good measurement is retained.
    """

    def __init__(self, pin_num, valid_min_us=900, valid_max_us=2100):
        self._valid_min_us = valid_min_us
        self._valid_max_us = valid_max_us
        self._rise_us = 0
        self._pulse_width_us = 0
        self._last_edge_us = 0

        self._pin = Pin(pin_num, Pin.IN, Pin.PULL_DOWN)
        self._pin.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
                       handler=self._irq_handler)

    def _irq_handler(self, pin):
        now = time.ticks_us()
        self._last_edge_us = now
        if pin.value():
            # Rising edge
            self._rise_us = now
        else:
            # Falling edge - measure pulse width
            if self._rise_us:
                width = time.ticks_diff(now, self._rise_us)
                # Reject EMI glitches outside the valid PWM range.
                if self._valid_min_us <= width <= self._valid_max_us:
                    self._pulse_width_us = width

    @property
    def pulse_width_us(self):
        """Pulse width (high time) in microseconds."""
        return self._pulse_width_us

    @property
    def signal_age_ms(self):
        """Milliseconds since last edge. Returns -1 if no edge received yet."""
        if self._last_edge_us == 0:
            return -1
        return time.ticks_diff(time.ticks_us(), self._last_edge_us) // 1000

    def deinit(self):
        """Disable the interrupt."""
        self._pin.irq(handler=None)


class LightingController:
    """
    Main controller for the Lutris lighting system.

    Manages 4 SeaLite LED lights on a shared UART0 bus,
    addressed individually (1-4) via the SeaSense protocol.
    Light brightness is controlled by a PWM input from the BlueROV.
    """

    # Hardware UART pin assignments
    UART_TX_PIN = 0   # GP0
    UART_RX_PIN = 1   # GP1

    # PWM input from BlueROV servo output
    PWM_INPUT_PIN = 16  # GP16

    # ArduSub servo PWM range (microseconds)
    PWM_MIN_US = 1100
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

    # Status print interval
    STATUS_INTERVAL_MS = 2000

    # I2C for INA238
    I2C_ID = 1
    I2C_SDA_PIN = 14  # GP14
    I2C_SCL_PIN = 15  # GP15

    # INA238 configuration
    INA238_ADDRESS = 0x40
    SHUNT_RESISTANCE = 0.1  # Ohms
    MAX_CURRENT = 10.0      # Amps

    # Serial configuration
    BAUDRATE = 9600

    # Light addresses on the shared bus
    LIGHT_ADDRESSES = [1, 2, 3, 4]

    def __init__(self):
        """Initialize the lighting controller."""
        self._uart = None
        self._lights = []
        self._power_monitor = None
        self._pwm_reader = None
        self._current_level = 0

        self._init_uart()
        self._init_lights()
        self._init_power_monitor()
        self._init_pwm_reader()

    def _init_uart(self):
        """Initialize shared UART0 connection."""
        self._uart = UARTWrapper(
            uart_id=0,
            baudrate=self.BAUDRATE,
            tx=Pin(self.UART_TX_PIN),
            rx=Pin(self.UART_RX_PIN),
            timeout=2000
        )
        print(f"UART0 initialized: TX=GP{self.UART_TX_PIN}, RX=GP{self.UART_RX_PIN}")

    def _init_lights(self):
        """Initialize SeaLite objects for each light."""
        for addr in self.LIGHT_ADDRESSES:
            light = Sealite(address=addr, max_level=100, expect_response=False)
            self._lights.append(light)
        print(f"Initialized {len(self._lights)} SeaLite lights (addresses {self.LIGHT_ADDRESSES})")

    def _init_power_monitor(self):
        """Initialize I2C and INA238 power monitor."""
        i2c = I2C(
            self.I2C_ID,
            sda=Pin(self.I2C_SDA_PIN),
            scl=Pin(self.I2C_SCL_PIN),
            freq=400000
        )

        devices = i2c.scan()
        print(f"I2C devices found: {[hex(d) for d in devices]}")

        try:
            self._power_monitor = INA238(
                i2c,
                address=self.INA238_ADDRESS,
                shunt_ohms=self.SHUNT_RESISTANCE,
                max_current=self.MAX_CURRENT
            )
            print("INA238 power monitor initialized")
        except RuntimeError as e:
            print(f"Warning: INA238 not found: {e}")
            self._power_monitor = None

    def _init_pwm_reader(self):
        """Initialize PWM input reader."""
        self._pwm_reader = PWMReader(
            self.PWM_INPUT_PIN,
            valid_min_us=self.PWM_VALID_MIN_US,
            valid_max_us=self.PWM_VALID_MAX_US,
        )
        print(f"PWM input initialized: GP{self.PWM_INPUT_PIN}")
        print(f"  PWM range: {self.PWM_MIN_US}-{self.PWM_MAX_US} us -> 0-100% brightness")
        print(f"  Accept range: {self.PWM_VALID_MIN_US}-{self.PWM_VALID_MAX_US} us, median window: {self.MEDIAN_WINDOW}")

    def pwm_to_level(self, pulse_width_us):
        """Map PWM pulse width (us) to light level (0-100)."""
        if pulse_width_us <= self.PWM_MIN_US:
            return 0
        if pulse_width_us >= self.PWM_MAX_US:
            return 100
        return int((pulse_width_us - self.PWM_MIN_US) * 100
                    / (self.PWM_MAX_US - self.PWM_MIN_US))

    def set_light_level(self, light_index, level):
        """
        Set brightness level for a specific light.

        Args:
            light_index: Light index (0-3)
            level: Brightness level (0-100)

        Returns:
            True on success, False on failure
        """
        if light_index < 0 or light_index >= len(self._lights):
            print(f"Invalid light index: {light_index}")
            return False

        try:
            return self._lights[light_index].set_level(self._uart, level)
        except Exception as e:
            print(f"Error setting light {light_index}: {e}")
            return False

    def set_all_lights(self, level):
        """Set brightness level for all lights."""
        success = 0
        for i in range(len(self._lights)):
            if self.set_light_level(i, level):
                success += 1

            # Arbitrary pause to let each light respond
            # This could either be tuned to better match the
            # actual time to respond, or the code to catch
            # replies from the lights could be made more robust.
            time.sleep_us(100000)
        return success

    def get_light_level(self, light_index):
        """Read current brightness level from a light."""
        if light_index < 0 or light_index >= len(self._lights):
            return None

        try:
            return self._lights[light_index].read_level(self._uart)
        except Exception as e:
            print(f"Error reading light {light_index}: {e}")
            return None

    def get_light_temperature(self, light_index):
        """Read temperature from a light."""
        if light_index < 0 or light_index >= len(self._lights):
            return None

        try:
            return self._lights[light_index].read_temperature(self._uart)
        except Exception as e:
            print(f"Error reading temp from light {light_index}: {e}")
            return None

    def read_power(self):
        """Read power consumption from INA238."""
        if self._power_monitor is None:
            return None

        try:
            return self._power_monitor.read_all()
        except Exception as e:
            print(f"Error reading power: {e}")
            return None

    def all_off(self):
        """Turn off all lights."""
        return self.set_all_lights(0)

    def status(self):
        """Get status of all lights and power consumption."""
        return {
            'lights': [
                {'index': i, 'level': self._lights[i].level}
                for i in range(len(self._lights))
            ],
            'power': self.read_power()
        }

    def run(self):
        """Main control loop. Reads PWM input and updates light brightness.

        Runs until KeyboardInterrupt (Ctrl-C). Turns lights off on exit.
        """
        print("PWM control loop running (Ctrl-C to stop)")

        last_status_ms = time.ticks_ms()
        pw_samples = []

        try:
            while True:
                signal_age = self._pwm_reader.signal_age_ms
                signal_lost = (signal_age < 0 or
                               signal_age > self.SIGNAL_TIMEOUT_MS)

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
                if abs(target_level - self._current_level) >= self.LEVEL_DEADBAND:
                    self.set_all_lights(target_level)
                    self._current_level = target_level

                # Periodic status output
                now_ms = time.ticks_ms()
                if time.ticks_diff(now_ms, last_status_ms) >= self.STATUS_INTERVAL_MS:
                    if signal_lost:
                        print(f"PWM: NO SIGNAL | Level: {self._current_level}%")
                    else:
                        print(f"PWM: {pw_filtered} us | Level: {self._current_level}%")
                    last_status_ms = now_ms

                time.sleep_ms(self.LOOP_INTERVAL_MS)

        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.all_off()
            self._pwm_reader.deinit()
            print("Lights off. PWM reader disabled.")


controller = None


def main():
    """Main entry point. Initializes controller and starts PWM control loop."""
    global controller

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
