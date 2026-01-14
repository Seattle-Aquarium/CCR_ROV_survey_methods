"""
PIO-based software UART for Raspberry Pi Pico.
Provides additional UART channels beyond the 2 hardware UARTs.
"""

import rp2
from machine import Pin
import time


@rp2.asm_pio(
    sideset_init=rp2.PIO.OUT_HIGH,
    out_shiftdir=rp2.PIO.SHIFT_RIGHT,
    autopull=True,
    pull_thresh=8
)
def uart_tx():
    """PIO program for UART transmit."""
    # Wait for data in OSR, then output start bit (low)
    pull()
    set(x, 7)              .side(0) [7]  # Start bit + delay
    # Output 8 data bits
    label("bitloop")
    out(pins, 1)                    [6]
    jmp(x_dec, "bitloop")           [6]
    # Stop bit (high)
    nop()                  .side(1) [6]


@rp2.asm_pio(
    in_shiftdir=rp2.PIO.SHIFT_RIGHT,
    autopush=True,
    push_thresh=8
)
def uart_rx():
    """PIO program for UART receive."""
    # Wait for start bit (falling edge)
    wait(0, pin, 0)
    # Delay to middle of first data bit
    set(x, 7)                       [10]
    # Sample 8 data bits
    label("bitloop")
    in_(pins, 1)                    [6]
    jmp(x_dec, "bitloop")           [6]
    # Wait for stop bit
    wait(1, pin, 0)


class PioUart:
    """
    Software UART using Pico's PIO.

    Provides a serial interface compatible with UARTWrapper.
    """

    def __init__(self, tx_pin, rx_pin, baudrate=9600, timeout=1000):
        """
        Initialize PIO UART.

        Args:
            tx_pin: GPIO pin number for TX
            rx_pin: GPIO pin number for RX
            baudrate: Baud rate (default 9600)
            timeout: Read timeout in milliseconds
        """
        self._timeout = timeout
        self._baudrate = baudrate

        # Calculate PIO clock divider for desired baud rate
        # PIO runs at 125MHz, each bit needs 8 cycles in our program
        div = 125_000_000 // (baudrate * 8)

        # Initialize TX state machine
        self._sm_tx = rp2.StateMachine(
            0,  # State machine ID (0-7)
            uart_tx,
            freq=baudrate * 8,
            out_base=Pin(tx_pin),
            sideset_base=Pin(tx_pin)
        )

        # Initialize RX state machine
        self._sm_rx = rp2.StateMachine(
            1,  # State machine ID
            uart_rx,
            freq=baudrate * 8,
            in_base=Pin(rx_pin, Pin.IN, Pin.PULL_UP)
        )

        self._sm_tx.active(1)
        self._sm_rx.active(1)

    def write(self, data):
        """Write data to UART. Accepts bytes or string."""
        if isinstance(data, str):
            data = data.encode('ascii')
        for byte in data:
            self._sm_tx.put(byte)
        return len(data)

    def read(self, size=1):
        """Read specified number of bytes with timeout."""
        result = bytearray()
        start = time.ticks_ms()

        while len(result) < size:
            if self._sm_rx.rx_fifo():
                result.append(self._sm_rx.get() & 0xFF)
            elif time.ticks_diff(time.ticks_ms(), start) > self._timeout:
                break
            else:
                time.sleep_us(100)

        return bytes(result) if result else None

    def read_until(self, terminator=b'\n', size=None):
        """Read until terminator character or timeout."""
        result = bytearray()
        start = time.ticks_ms()

        while True:
            if self._sm_rx.rx_fifo():
                byte = self._sm_rx.get() & 0xFF
                result.append(byte)
                if bytes([byte]) == terminator:
                    break
                if size and len(result) >= size:
                    break
            elif time.ticks_diff(time.ticks_ms(), start) > self._timeout:
                break
            else:
                time.sleep_us(100)

        return bytes(result)

    def readline(self):
        """Read a line (until newline or timeout)."""
        return self.read_until(b'\n')

    def any(self):
        """Return number of bytes available to read."""
        return self._sm_rx.rx_fifo()

    def flush(self):
        """Flush buffers."""
        # Drain RX FIFO
        while self._sm_rx.rx_fifo():
            self._sm_rx.get()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._sm_tx.active(0)
        self._sm_rx.active(0)
