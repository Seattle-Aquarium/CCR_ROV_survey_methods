"""
PWM input measurement test for Raspberry Pi Pico 2.

Uses pin interrupts to measure pulse width on GP16.
Prints measured pulse width (µs) and duty cycle once per second.
"""

from machine import Pin
import time

# Configuration
PWM_INPUT_PIN = 16


class PWMReader:
    """Reads PWM signals using pin edge interrupts."""

    def __init__(self, pin_num):
        self._rise_us = 0
        self._pulse_width_us = 0
        self._period_us = 0
        self._last_rise_us = 0

        self._pin = Pin(pin_num, Pin.IN, Pin.PULL_DOWN)
        self._pin.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING,
                       handler=self._irq_handler)

    def _irq_handler(self, pin):
        now = time.ticks_us()
        if pin.value():
            # Rising edge
            if self._last_rise_us:
                self._period_us = time.ticks_diff(now, self._last_rise_us)
            self._last_rise_us = now
            self._rise_us = now
        else:
            # Falling edge
            if self._rise_us:
                self._pulse_width_us = time.ticks_diff(now, self._rise_us)

    @property
    def pulse_width_us(self):
        """Pulse width (high time) in microseconds."""
        return self._pulse_width_us

    @property
    def period_us(self):
        """Period (rise-to-rise) in microseconds."""
        return self._period_us

    @property
    def duty_percent(self):
        """Duty cycle as a percentage."""
        if self._period_us == 0:
            return 0.0
        return (self._pulse_width_us / self._period_us) * 100.0

    @property
    def frequency_hz(self):
        """Signal frequency in Hz."""
        if self._period_us == 0:
            return 0.0
        return 1_000_000.0 / self._period_us

    def deinit(self):
        """Disable the interrupt."""
        self._pin.irq(handler=None)


def main():
    print(f"PWM Input Test - GP{PWM_INPUT_PIN}")
    print("-" * 40)
    print("Waiting for PWM signal...")
    print()

    reader = PWMReader(PWM_INPUT_PIN)

    try:
        while True:
            pw = reader.pulse_width_us
            period = reader.period_us
            duty = reader.duty_percent
            freq = reader.frequency_hz

            if pw > 0:
                print(f"Pulse: {pw:6d} µs | "
                      f"Period: {period:6d} µs | "
                      f"Duty: {duty:5.1f}% | "
                      f"Freq: {freq:.1f} Hz")
            else:
                print("No signal detected")

            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        reader.deinit()


if __name__ == "__main__":
    main()
