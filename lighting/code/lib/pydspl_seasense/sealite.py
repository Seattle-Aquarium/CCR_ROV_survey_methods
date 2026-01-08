from pydspl_seasense.peripheral_base import PeripheralBase
import serial


# QUESTION(lindzey): Why the two layers of abstraction? Does DSP&L have other
#    devices that we anticipate using?
#
# (Aaron):  Short answer, yes.  The protocol is divided between commands which
#    are supported by all DSPL devices, with extensions for different kinds of
#    peripherals (lights, lasers, etc) which we don't currently use.
#
class Sealite(PeripheralBase):

    # Magic strings for commands supported by DSPL lights
    DSPL_VERB_LIGHT_LEVEL = "lout"

    """
    Class which implements interfaces specific to a DSP&L Sealite
    LED lamp.

    It knows its own adress, but does NOT own the port itself since that
    may be shared by other lights.

    This class stores an internal copy of the last level read from the light
    to allow the "{increment,decrement}_light functions.
    """

    def __init__(self, address, fp=None, max_level=100, local_echo=False):
        super().__init__(address, local_echo=local_echo)

        self._level = 0

        if fp:
            level = self.read_level(fp)
            if level:
                self._level = level

        self._max_level = max_level

    @property
    def level(self):
        return self._level

    def read_level(self, fp):
        """Read the brightness level (LOUT) from the device.

        Also updates the cached value.

        Raises ValueError if the value from the light can't be
        converted to a float.   May also raise DSPLTimeoutError
        or DPSLNackError.
        """
        value = self.do_query(fp, self.DSPL_VERB_LIGHT_LEVEL)

        self._level = int(value)
        return self._level

    def set_level(self, fp, level, force=False):
        """Set the brightness level (LOUT) from the device 0-100.

        Raises ValueError if the requested brightness is outside of
        the range [0,100]

        Will also raise ValueError if the requested value is greater
        than max_level and force is `False`.

        On success, updates the cached value.

        Raises ValueError if the value from the light can't be
        converted to a float.   May also raise DSPLTimeoutError
        or DPSLNackError.
        """

        level = int(level)
        # Built-in limits on output are 0 - 100%
        if level < 0 or level > 100:
            raise ValueError

        # User may specify a lower maximum for light output
        if level > self._max_level and not force:
            return ValueError

        ack = self.do_write(fp, self.DSPL_VERB_LIGHT_LEVEL, "%03d" % level)

        if ack:
            # Check for success
            self._level = level

        return ack

    def increment_level(self, fp, delta=10, force=False):
        delta = int(delta)

        if (self._level + delta) > self._max_level and not force:
            return False

        ack = self.do_increment(fp, self.DSPL_VERB_LIGHT_LEVEL, "%d" % delta)

        if ack:
            self._level = min(self._level + delta, self._max_level)

        return ack

    def decrement_level(self, fp, delta=10):
        delta = int(delta)

        new_level = self._level - delta

        if new_level >= 0:
            ack = self.do_decrement(fp, self.DSPL_VERB_LIGHT_LEVEL, "%d" % delta)
            if ack:
                self._level = new_level
        else:
            ack = self.do_write(fp, self.DSPL_VERB_LIGHT_LEVEL, "0")
            if ack:
                self._level = 0

        return ack


#
# Provide a command line interface as an entry_point
#


def cli_request_info(args, sealite, fp):
    print(sealite.read_info(fp))


def cli_request_temperature(args, sealite, fp):
    print(sealite.read_temperature(fp))


def cli_request_humidity(args, sealite, fp):
    print(sealite.read_humidity(fp))


def cli_set_level(args, sealite, fp):
    if args.level is None:
        print(sealite.read_level(fp))
    else:
        print(sealite.set_level(fp, args.level))


def cli_set_off(args, sealite, fp):
    if sealite.set_level(fp, 0):
        print("Command acknowledged")
    else:
        print("No acknowledgement")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sealite command line interface.")
    parser.add_argument(
        "--port",
        required=True,
        help='Path or pyserial URL to Sealite device.   Use "socket://10.10.10.135:1000/" for the Raven VIB.',
    )

    parser.add_argument(
        "--timeout", default=1, help="Timeout in seconds for reply from DSPL device"
    )

    parser.add_argument(
        "--address",
        default="001",
        help="RS485 address for light (current Raven lights are addr 3 and 4)",
    )

    parser.add_argument(
        "--local-echo",
        action="store_true",
        help="If set, library will expect a local echo of outgoing commands",
    )

    subparsers = parser.add_subparsers(required=True, dest="command")

    info_parser = subparsers.add_parser("info", help="Query help string")
    info_parser.set_defaults(func=cli_request_info)

    temp_parser = subparsers.add_parser(
        "temperature", help="Query device temperature string", aliases=["temp"]
    )
    temp_parser.set_defaults(func=cli_request_temperature)

    rhum_parser = subparsers.add_parser(
        "humidity", help="Query device relative humidity string", aliases=["rhum"]
    )
    rhum_parser.set_defaults(func=cli_request_humidity)

    level_parser = subparsers.add_parser("level", help="Set/query light brightness")
    level_parser.add_argument(
        "level",
        type=int,
        nargs="?",
        default=None,
        help="If provided, set lamp brightness (0-100)",
    )
    level_parser.set_defaults(func=cli_set_level)

    off_parser = subparsers.add_parser("off", help="Turn light off")
    off_parser.set_defaults(func=cli_set_off)

    args = parser.parse_args()

    with serial.serial_for_url(args.port, timeout=args.timeout) as fp:
        sealite = Sealite(args.address, fp=fp, local_echo=args.local_echo)

        args.func(args, sealite, fp)
