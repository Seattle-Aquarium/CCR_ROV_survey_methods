"""
MicroPython driver for the INA238 power monitor.
Provides current, voltage, and power measurements over I2C.
"""

from machine import I2C


class INA238:
    """
    Driver for the Texas Instruments INA238 power monitor.

    The INA238 is a 16-bit digital power monitor with I2C interface.
    It measures bus voltage, shunt voltage, current, and power.
    """

    # Register addresses
    REG_CONFIG = 0x00
    REG_ADC_CONFIG = 0x01
    REG_SHUNT_CAL = 0x02
    REG_SHUNT_VOLTAGE = 0x04
    REG_BUS_VOLTAGE = 0x05
    REG_DIETEMP = 0x06
    REG_CURRENT = 0x07
    REG_POWER = 0x08
    REG_MANUFACTURER_ID = 0x3E
    REG_DEVICE_ID = 0x3F

    # Default I2C address (A0=GND, A1=GND)
    DEFAULT_ADDRESS = 0x40

    # Conversion factors
    BUS_VOLTAGE_LSB = 3.125e-3  # 3.125 mV/LSB
    SHUNT_VOLTAGE_LSB = 5e-6    # 5 uV/LSB (for ADCRANGE=0)

    def __init__(self, i2c, address=DEFAULT_ADDRESS, shunt_ohms=0.1, max_current=3.2):
        """
        Initialize INA238 driver.

        Args:
            i2c: MicroPython I2C object
            address: I2C address (default 0x40)
            shunt_ohms: Shunt resistor value in ohms
            max_current: Maximum expected current in amps (for calibration)
        """
        self._i2c = i2c
        self._address = address
        self._shunt_ohms = shunt_ohms

        # Calculate current LSB: max_current / 2^15
        self._current_lsb = max_current / 32768

        # Verify device is present
        if not self._device_present():
            raise RuntimeError(f"INA238 not found at address 0x{address:02X}")

        # Configure and calibrate
        self._configure()
        self._calibrate()

    def _device_present(self):
        """Check if device responds at the configured address."""
        devices = self._i2c.scan()
        return self._address in devices

    def _write_register(self, reg, value):
        """Write a 16-bit value to a register."""
        data = bytes([(value >> 8) & 0xFF, value & 0xFF])
        self._i2c.writeto_mem(self._address, reg, data)

    def _read_register(self, reg):
        """Read a 16-bit value from a register."""
        data = self._i2c.readfrom_mem(self._address, reg, 2)
        return (data[0] << 8) | data[1]

    def _read_register_signed(self, reg):
        """Read a signed 16-bit value from a register."""
        value = self._read_register(reg)
        if value & 0x8000:
            value -= 0x10000
        return value

    def _configure(self):
        """Configure the INA238 with default settings."""
        # Default configuration: continuous mode, all measurements
        # CONFIG register: RST=0, CONVDLY=0, ADCRANGE=0
        self._write_register(self.REG_CONFIG, 0x0000)

        # ADC configuration: continuous mode, 1024 averages, 1052us conversion
        # This provides good accuracy with reasonable update rate
        adc_config = (
            (0x0 << 12) |  # MODE: Continuous shunt and bus voltage
            (0x5 << 9) |   # VBUSCT: 1052us
            (0x5 << 6) |   # VSHCT: 1052us
            (0x5 << 3) |   # VTCT: 1052us (temperature)
            (0x4 << 0)     # AVG: 128 samples
        )
        self._write_register(self.REG_ADC_CONFIG, adc_config)

    def _calibrate(self):
        """
        Set the calibration register based on shunt resistor value.

        SHUNT_CAL = 819.2e6 * CURRENT_LSB * R_SHUNT
        """
        shunt_cal = int(819.2e6 * self._current_lsb * self._shunt_ohms)
        # Ensure value fits in 15 bits
        shunt_cal = min(shunt_cal, 0x7FFF)
        self._write_register(self.REG_SHUNT_CAL, shunt_cal)

    def read_bus_voltage(self):
        """
        Read bus voltage in volts.

        Returns:
            Bus voltage in V
        """
        raw = self._read_register_signed(self.REG_BUS_VOLTAGE)
        return raw * self.BUS_VOLTAGE_LSB

    def read_shunt_voltage(self):
        """
        Read shunt voltage in millivolts.

        Returns:
            Shunt voltage in mV
        """
        raw = self._read_register_signed(self.REG_SHUNT_VOLTAGE)
        return raw * self.SHUNT_VOLTAGE_LSB * 1000  # Convert to mV

    def read_current(self):
        """
        Read current in amps.

        Returns:
            Current in A (positive = into load)
        """
        raw = self._read_register_signed(self.REG_CURRENT)
        return raw * self._current_lsb

    def read_power(self):
        """
        Read power in watts.

        Returns:
            Power in W
        """
        raw = self._read_register(self.REG_POWER)
        # Power LSB = 0.2 * current_lsb
        return raw * 0.2 * self._current_lsb

    def read_temperature(self):
        """
        Read die temperature in degrees Celsius.

        Returns:
            Temperature in degrees C
        """
        raw = self._read_register_signed(self.REG_DIETEMP)
        # Temperature is in bits [15:4], LSB = 125 m°C
        return (raw >> 4) * 0.125

    def read_all(self):
        """
        Read all measurements.

        Returns:
            dict with keys: voltage, current, power, temperature
        """
        return {
            'voltage': self.read_bus_voltage(),
            'current': self.read_current(),
            'power': self.read_power(),
            'temperature': self.read_temperature()
        }

    def get_manufacturer_id(self):
        """Read manufacturer ID (should be 0x5449 for 'TI')."""
        return self._read_register(self.REG_MANUFACTURER_ID)

    def get_device_id(self):
        """Read device ID."""
        return self._read_register(self.REG_DEVICE_ID)
