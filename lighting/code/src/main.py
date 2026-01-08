"""
Main application for the Lutris Lighting System.
Controls 4 SeaLite LED lights and monitors power via INA238 current sensor.
"""

import time
from machine import Pin, I2C

# Add lib directory to path
import sys
sys.path.append('/lib')

from uart_wrapper import UARTWrapper
from ina238 import INA238
from pydspl_seasense.sealite import Sealite


class LightingController:
    """
    Main controller for the Lutris lighting system.

    Manages 4 SeaLite LED lights over RS485 and monitors power consumption
    via INA238 current sensor.
    """

    # Pin assignments for Raspberry Pi Pico
    # Adjust these based on your wiring
    UART_ID = 0          # UART0 for RS485 to lights
    UART_TX_PIN = 0      # GP0 (Pin 1)
    UART_RX_PIN = 1      # GP1 (Pin 2)

    I2C_ID = 0           # I2C0 for INA238
    I2C_SDA_PIN = 4      # GP4 (Pin 6)
    I2C_SCL_PIN = 5      # GP5 (Pin 7)

    # Light RS485 addresses (configure to match your lights)
    LIGHT_ADDRESSES = [1, 2, 3, 4]

    # INA238 configuration
    INA238_ADDRESS = 0x40
    SHUNT_RESISTANCE = 0.1  # 100 mOhm shunt resistor
    MAX_CURRENT = 10.0      # Max expected current in amps

    def __init__(self):
        """Initialize the lighting controller."""
        self._lights = []
        self._uart = None
        self._power_monitor = None

        self._init_uart()
        self._init_i2c()
        self._init_lights()

    def _init_uart(self):
        """Initialize UART for RS485 communication with lights."""
        self._uart = UARTWrapper(
            uart_id=self.UART_ID,
            baudrate=9600,
            tx=Pin(self.UART_TX_PIN),
            ty=Pin(self.UART_RX_PIN),
            timeout=1000
        )
        print("UART initialized for SeaLite communication")

    def _init_i2c(self):
        """Initialize I2C and INA238 power monitor."""
        i2c = I2C(
            self.I2C_ID,
            sda=Pin(self.I2C_SDA_PIN),
            scl=Pin(self.I2C_SCL_PIN),
            freq=400000
        )

        # Scan for I2C devices
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
            print(f"Warning: Could not initialize INA238: {e}")
            self._power_monitor = None

    def _init_lights(self):
        """Initialize SeaLite light objects."""
        for addr in self.LIGHT_ADDRESSES:
            light = Sealite(address=addr, max_level=100)
            self._lights.append(light)
        print(f"Initialized {len(self._lights)} SeaLite lights")

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
            print(f"Error setting light {light_index} to {level}: {e}")
            return False

    def set_all_lights(self, level):
        """
        Set brightness level for all lights.

        Args:
            level: Brightness level (0-100)

        Returns:
            Number of lights successfully set
        """
        success_count = 0
        for i in range(len(self._lights)):
            if self.set_light_level(i, level):
                success_count += 1
        return success_count

    def get_light_level(self, light_index):
        """
        Read current brightness level from a light.

        Args:
            light_index: Light index (0-3)

        Returns:
            Brightness level (0-100) or None on error
        """
        if light_index < 0 or light_index >= len(self._lights):
            return None

        try:
            return self._lights[light_index].read_level(self._uart)
        except Exception as e:
            print(f"Error reading light {light_index}: {e}")
            return None

    def get_light_temperature(self, light_index):
        """
        Read temperature from a light.

        Args:
            light_index: Light index (0-3)

        Returns:
            Temperature in Celsius or None on error
        """
        if light_index < 0 or light_index >= len(self._lights):
            return None

        try:
            return self._lights[light_index].read_temperature(self._uart)
        except Exception as e:
            print(f"Error reading temperature from light {light_index}: {e}")
            return None

    def read_power(self):
        """
        Read power consumption from INA238.

        Returns:
            dict with voltage, current, power, temperature or None if unavailable
        """
        if self._power_monitor is None:
            return None

        try:
            return self._power_monitor.read_all()
        except Exception as e:
            print(f"Error reading power monitor: {e}")
            return None

    def all_off(self):
        """Turn off all lights."""
        return self.set_all_lights(0)

    def status(self):
        """
        Get status of all lights and power consumption.

        Returns:
            dict with light levels and power data
        """
        status = {
            'lights': [],
            'power': self.read_power()
        }

        for i, light in enumerate(self._lights):
            light_status = {
                'index': i,
                'address': light.address,
                'level': light.level  # Cached level
            }
            status['lights'].append(light_status)

        return status


# Global controller instance
controller = None


def main():
    """Main entry point."""
    global controller

    print("Lutris Lighting System Starting...")
    print("-" * 40)

    # Initialize controller
    controller = LightingController()

    print("-" * 40)
    print("System ready")
    print("Commands:")
    print("  controller.set_all_lights(level)  - Set all lights (0-100)")
    print("  controller.set_light_level(n, level) - Set light n")
    print("  controller.all_off()              - Turn off all lights")
    print("  controller.read_power()           - Read power consumption")
    print("  controller.status()               - Get system status")


if __name__ == "__main__":
    main()
