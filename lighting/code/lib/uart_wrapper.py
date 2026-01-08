"""
MicroPython UART wrapper that provides a pyserial-compatible interface.
This allows the pydspl_seasense library to work with MicroPython's machine.UART.
"""

from machine import UART


class UARTWrapper:
    """
    Wraps MicroPython's machine.UART to provide pyserial-compatible methods.

    The pydspl_seasense library expects:
    - write(data: bytes)
    - read_until() -> bytes (reads until newline or timeout)
    """

    def __init__(self, uart_id, baudrate=9600, tx=None, ty=None, timeout=1000):
        """
        Initialize UART wrapper.

        Args:
            uart_id: UART peripheral number (0 or 1 on Pico)
            baudrate: Communication speed (9600, 19200, or 57600 for SeaSense)
            tx: TX pin number (optional, uses default if not specified)
            rx: RX pin number (optional, uses default if not specified)
            timeout: Read timeout in milliseconds
        """
        self._timeout = timeout

        if tx is not None and ty is not None:
            self._uart = UART(uart_id, baudrate=baudrate, tx=tx, rx=ty, timeout=timeout)
        else:
            self._uart = UART(uart_id, baudrate=baudrate, timeout=timeout)

    def write(self, data):
        """Write data to UART. Accepts bytes or string."""
        if isinstance(data, str):
            data = data.encode('ascii')
        return self._uart.write(data)

    def read_until(self, terminator=b'\n', size=None):
        """
        Read until terminator character or timeout.

        This mimics pyserial's read_until behavior.
        """
        result = bytearray()
        while True:
            char = self._uart.read(1)
            if char is None:
                # Timeout occurred
                break
            result.extend(char)
            if char == terminator:
                break
            if size and len(result) >= size:
                break
        return bytes(result)

    def read(self, size=1):
        """Read specified number of bytes."""
        return self._uart.read(size)

    def readline(self):
        """Read a line (until newline or timeout)."""
        return self._uart.readline()

    def any(self):
        """Return number of bytes available to read."""
        return self._uart.any()

    def flush(self):
        """Flush the write buffer (no-op on MicroPython UART)."""
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
