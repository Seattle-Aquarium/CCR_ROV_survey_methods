""" """

import time
from machine import Pin


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
        self.rejected = 0

        self._pin = Pin(pin_num, Pin.IN, Pin.PULL_DOWN)
        self._pin.irq(
            trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
            handler=self._irq_handler,
            hard=True,
        )

    # Because a hard ISR interrupts the MicroPython runtime mid-operation,
    # you must follow the strict MicroPython ISR Rules to prevent the
    # system from crashing
    #
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
                else:
                    self.rejected += 1

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
