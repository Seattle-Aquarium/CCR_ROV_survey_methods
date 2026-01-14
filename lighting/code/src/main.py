"""
Main application for the Lutris Lighting System.
Controls 4 SeaLite LED lights (each on separate serial) and monitors power via INA238.
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
from pio_uart import PioUart
from ina238 import INA238
from pydspl_seasense.sealite import Sealite


class LightingController:
    """
    Main controller for the Lutris lighting system.

    Manages 4 SeaLite LED lights, each on its own serial connection:
    - Lights 0-1: Hardware UART0 and UART1
    - Lights 2-3: PIO-based software UARTs
    """

    # Hardware UART pin assignments
    UART0_TX_PIN = 0   # GP0
    UART0_RX_PIN = 1   # GP1
    UART1_TX_PIN = 4   # GP4
    UART1_RX_PIN = 5   # GP5

    # PIO UART pin assignments
    PIO_UART2_TX_PIN = 8   # GP8
    PIO_UART2_RX_PIN = 9   # GP9
    PIO_UART3_TX_PIN = 10  # GP10
    PIO_UART3_RX_PIN = 11  # GP11

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

    def __init__(self):
        """Initialize the lighting controller."""
        self._uarts = []
        self._lights = []
        self._power_monitor = None

        self._init_uarts()
        self._init_lights()
        self._init_power_monitor()

    def _init_uarts(self):
        """Initialize 4 UART connections (2 hardware + 2 PIO)."""
        # Hardware UART 0
        uart0 = UARTWrapper(
            uart_id=0,
            baudrate=self.BAUDRATE,
            tx=Pin(self.UART0_TX_PIN),
            rx=Pin(self.UART0_RX_PIN),
            timeout=1000
        )
        self._uarts.append(uart0)
        print(f"UART0 initialized: TX=GP{self.UART0_TX_PIN}, RX=GP{self.UART0_RX_PIN}")

        # Hardware UART 1
        uart1 = UARTWrapper(
            uart_id=1,
            baudrate=self.BAUDRATE,
            tx=Pin(self.UART1_TX_PIN),
            rx=Pin(self.UART1_RX_PIN),
            timeout=1000
        )
        self._uarts.append(uart1)
        print(f"UART1 initialized: TX=GP{self.UART1_TX_PIN}, RX=GP{self.UART1_RX_PIN}")

        # PIO UART 2
        uart2 = PioUart(
            tx_pin=self.PIO_UART2_TX_PIN,
            rx_pin=self.PIO_UART2_RX_PIN,
            baudrate=self.BAUDRATE,
            timeout=1000
        )
        self._uarts.append(uart2)
        print(f"PIO UART2 initialized: TX=GP{self.PIO_UART2_TX_PIN}, RX=GP{self.PIO_UART2_RX_PIN}")

        # PIO UART 3
        uart3 = PioUart(
            tx_pin=self.PIO_UART3_TX_PIN,
            rx_pin=self.PIO_UART3_RX_PIN,
            baudrate=self.BAUDRATE,
            timeout=1000
        )
        self._uarts.append(uart3)
        print(f"PIO UART3 initialized: TX=GP{self.PIO_UART3_TX_PIN}, RX=GP{self.PIO_UART3_RX_PIN}")

    def _init_lights(self):
        """Initialize SeaLite objects for each light."""
        for _ in range(4):
            # Each light uses address 1 since they're on separate serial lines
            light = Sealite(address=1, max_level=100)
            self._lights.append(light)
        print(f"Initialized {len(self._lights)} SeaLite lights")

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
            uart = self._uarts[light_index]
            return self._lights[light_index].set_level(uart, level)
        except Exception as e:
            print(f"Error setting light {light_index}: {e}")
            return False

    def set_all_lights(self, level):
        """Set brightness level for all lights."""
        success = 0
        for i in range(len(self._lights)):
            if self.set_light_level(i, level):
                success += 1
        return success

    def get_light_level(self, light_index):
        """Read current brightness level from a light."""
        if light_index < 0 or light_index >= len(self._lights):
            return None

        try:
            uart = self._uarts[light_index]
            return self._lights[light_index].read_level(uart)
        except Exception as e:
            print(f"Error reading light {light_index}: {e}")
            return None

    def get_light_temperature(self, light_index):
        """Read temperature from a light."""
        if light_index < 0 or light_index >= len(self._lights):
            return None

        try:
            uart = self._uarts[light_index]
            return self._lights[light_index].read_temperature(uart)
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


controller = None


def main():
    """Main entry point. Returns controller for REPL use."""
    global controller

    print("Lutris Lighting System Starting...")
    print("-" * 40)

    controller = LightingController()

    print("-" * 40)
    print("System ready. Commands:")
    print("  c.set_light_level(0, 50)  # Light 0 to 50%")
    print("  c.get_light_level(0)      # Read level")
    print("  c.get_light_temperature(0)")
    print("  c.set_all_lights(50)")
    print("  c.all_off()")
    print("  c.read_power()")
    print("  c.status()")

    return controller


if __name__ == "__main__":
    main()
