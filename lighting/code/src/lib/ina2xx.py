"""
INA226 and INA238 power monitors, told apart by asking them.

The light box's bill of materials lists an INA226 module; the firmware's
original driver (ina238.py) is written for an INA238; Keenan Johnson's PCB
(J5) expects an Adafruit INA238 breakout at 0x40. The two chips keep their
readings in different registers, so the wrong driver on the wrong chip reads
plausible-looking nonsense and reports success. This module reads each
chip's identity registers first and drives only what it recognises:

=========  ======================  =====================
chip       manufacturer ID         device / die ID
=========  ======================  =====================
INA226     0xFE = 0x5449 ("TI")    0xFF = 0x2260
INA238     0x3E = 0x5449           0x3F = 0x238x
=========  ======================  =====================

**Bus voltage is right whatever the shunt.** Current and power depend on the
shunt resistor's value, which is set here (`shunt_ohms`) and is *not known*
for any monitor that has been fitted -- Lutris has none as of 13 October 2026.
So with `shunt_ohms=None` only voltage, the raw shunt voltage and (INA238) the
die temperature are reported; current and power are left out rather than
guessed.

Sizing, for whoever fits one: four lights at full output draw about 4 x 98 W
(Keenan's bench measurement) = ~390 W, i.e. ~16 A on the 24 V side or ~30 A on
the 13 V side. A 10 A breakout's built-in shunt cannot carry that; an external
shunt sized for it (e.g. 2 mOhm) and `shunt_ohms` set to match is what is needed
to read the lights' total current.

**Brief dips.** Polling every quarter second misses a dip shorter than that,
and the converter's dropout (Aaron Marburg's scope capture, CCR_development#50)
lasted 1-3 s with sub-second flickers. Both chips can latch a bus
under-voltage flag in hardware: set `uv_volts`, and each read reports whether
the bus went below it at any point since the last read, then clears it.

Register details are from TI's datasheets (INA226 SBOS547A, INA238 SBOSA20)
and have **not been run against a chip** -- see the bench checklist.
"""

TI = 0x5449


def _u16(i2c, addr, reg):
    b = i2c.readfrom_mem(addr, reg, 2)
    return (b[0] << 8) | b[1]


def _s16(i2c, addr, reg):
    v = _u16(i2c, addr, reg)
    return v - 0x10000 if v & 0x8000 else v


def _u24(i2c, addr, reg):
    b = i2c.readfrom_mem(addr, reg, 3)
    return (b[0] << 16) | (b[1] << 8) | b[2]


def _w16(i2c, addr, reg, value):
    i2c.writeto_mem(addr, reg, bytes([(value >> 8) & 0xFF, value & 0xFF]))


def identify(i2c, addr):
    """'INA238', 'INA226', or None."""
    try:
        if _u16(i2c, addr, 0x3E) == TI and (_u16(i2c, addr, 0x3F) >> 4) == 0x238:
            return "INA238"
    except Exception:
        pass
    try:
        if _u16(i2c, addr, 0xFE) == TI and _u16(i2c, addr, 0xFF) == 0x2260:
            return "INA226"
    except Exception:
        pass
    return None


class _Monitor:
    chip = "?"

    def __init__(self, i2c, addr, shunt_ohms=None, max_current=20.0, uv_volts=None):
        self.i2c = i2c
        self.addr = addr
        self.shunt_ohms = shunt_ohms
        self.max_current = max_current
        self.current_lsb = (max_current / 32768.0) if shunt_ohms else None
        self.uv_volts = uv_volts
        self.configure()

    def describe(self):
        return {"addr": self.addr, "chip": self.chip, "shunt_ohms": self.shunt_ohms,
                "uv_volts": self.uv_volts}


class INA226(_Monitor):
    chip = "INA226"
    BUS_LSB = 1.25e-3           # V
    SHUNT_LSB = 2.5e-6          # V

    def configure(self):
        # AVG 16, VBUSCT 1.1 ms, VSHCT 1.1 ms, continuous shunt and bus:
        # a fresh average every ~35 ms.
        _w16(self.i2c, self.addr, 0x00, 0x4527)
        if self.current_lsb:
            cal = int(0.00512 / (self.current_lsb * self.shunt_ohms))
            _w16(self.i2c, self.addr, 0x05, min(cal, 0x7FFF))
        if self.uv_volts:
            _w16(self.i2c, self.addr, 0x07, int(self.uv_volts / self.BUS_LSB) & 0xFFFF)
            _w16(self.i2c, self.addr, 0x06, (1 << 12) | 1)     # BUL, latched

    def read(self):
        out = {"v": _u16(self.i2c, self.addr, 0x02) * self.BUS_LSB,
               "shunt_mv": _s16(self.i2c, self.addr, 0x01) * self.SHUNT_LSB * 1000.0}
        if self.current_lsb:
            out["a"] = _s16(self.i2c, self.addr, 0x04) * self.current_lsb
            out["w"] = _u16(self.i2c, self.addr, 0x03) * 25.0 * self.current_lsb
        if self.uv_volts:
            # AFF (bit 4); reading Mask/Enable clears it when latched.
            out["uv"] = 1 if _u16(self.i2c, self.addr, 0x06) & (1 << 4) else 0
        return out


class INA238(_Monitor):
    chip = "INA238"
    BUS_LSB = 3.125e-3          # V
    SHUNT_LSB = 5e-6            # V, ADCRANGE = 0 (+/-163.84 mV)

    def configure(self):
        _w16(self.i2c, self.addr, 0x00, 0x0000)                # ADCRANGE 0
        # Continuous bus, shunt and temperature; 540 us each; 16 averages:
        # a fresh average every ~26 ms.
        _w16(self.i2c, self.addr, 0x01, 0xF922)
        if self.current_lsb:
            cal = int(819.2e6 * self.current_lsb * self.shunt_ohms)
            _w16(self.i2c, self.addr, 0x02, min(cal, 0x7FFF))
        if self.uv_volts:
            _w16(self.i2c, self.addr, 0x0F, int(self.uv_volts / self.BUS_LSB) & 0x7FFF)
            _w16(self.i2c, self.addr, 0x0B, 1 << 15)           # ALATCH

    def read(self):
        out = {"v": _u16(self.i2c, self.addr, 0x05) * self.BUS_LSB,
               "shunt_mv": _s16(self.i2c, self.addr, 0x04) * self.SHUNT_LSB * 1000.0,
               "t": (_s16(self.i2c, self.addr, 0x06) >> 4) * 0.125}
        if self.current_lsb:
            out["a"] = _s16(self.i2c, self.addr, 0x07) * self.current_lsb
            # POWER is 24 bits on the INA238 (the old driver read 16).
            out["w"] = _u24(self.i2c, self.addr, 0x08) * 0.2 * self.current_lsb
        if self.uv_volts:
            # BUSUL (bit 3); reading DIAG_ALRT clears latched flags.
            out["uv"] = 1 if _u16(self.i2c, self.addr, 0x0B) & (1 << 3) else 0
        return out


DRIVERS = {"INA226": INA226, "INA238": INA238}

#: Where INA2xx chips can answer (A0/A1 strapping).
ADDRESSES = range(0x40, 0x50)


def find(i2c, shunt_ohms=None, max_current=20.0, uv_volts=None, labels=None):
    """Every recognised monitor on the bus, as (label, driver) pairs.

    `labels` maps an address to a name for it ("bus", "light1", ...); a
    monitor without one is called "ina_0x41" and so on. Returns
    (monitors, notes), notes saying what was found and not driven.
    """
    labels = labels or {}
    found, notes = [], []
    try:
        present = [a for a in i2c.scan() if a in ADDRESSES]
    except Exception as ex:
        return [], ["I2C scan failed: " + repr(ex)]
    for addr in present:
        chip = identify(i2c, addr)
        if chip is None:
            notes.append("0x%02X answers but is not an INA226 or INA238" % addr)
            continue
        try:
            mon = DRIVERS[chip](i2c, addr, shunt_ohms=shunt_ohms,
                                max_current=max_current, uv_volts=uv_volts)
        except Exception as ex:
            notes.append("0x%02X %s could not be configured: %r" % (addr, chip, ex))
            continue
        found.append((labels.get(addr, "bus" if addr == 0x40 else "ina_0x%02x" % addr), mon))
    if not present:
        notes.append("no power monitor on I2C")
    return found, notes
