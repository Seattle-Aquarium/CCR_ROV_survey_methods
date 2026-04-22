from pydspl_seasense import seasense

# The netburner serial-to-ethernet starts replies with a null,
# so that is included that in the list of characters to strip
# from incoming responses
WHITESPACE_TO_STRIP = "\x00\r\n "


class DSPLTimeoutError(Exception):
    """Exception raised when a DSPL read times out."""

    pass


class DSPLNackError(Exception):
    """Exception raised when a DSPL device returns "?", indicating
    an unknown request from the user.
    """

    pass


class PeripheralBase:
    """
    Base class which provides generic I/O and commands common to all
    DSP&L devices.
    """

    # Magic strings for commands supports by all DSPL devices
    DSPL_VERB_INFO = "info"
    DSPL_VERB_TEMPERATURE = "temp"
    DSPL_VERB_HUMIDITY = "rhum"

    def __init__(self, address: int, local_echo: bool = False,
                 expect_response: bool = True) -> None:
        """
        Address is the device ID programmed into the unit

        If local_echo is True, the class will expect all outgoing commands
        (to the device) to be echoed back before any response.  This is
        the case with some half-duplex RS485 architectures where the
        transceiver's RX "hears" its own TX.

        If expect_response is False, write commands will not wait for
        ACK/NAK from the device (fire-and-forget).
        """

        self._address = address
        self._local_echo = local_echo
        self._expect_response = expect_response

    @property
    def address(self) -> None:
        return self._address

    def do_query(self, fp, command):
        """
        Generic read function that queries device for information.

        Requires a pyserial fp as well as the four character "command".
        Reads one line of output.  For commands that return multiple lines,
        additional lines need to be read manually.

        * Raises DSPLTimeoutError if there is no response from the device
        * Raises DSPLNackError if it received "?" from the device

        Command may be:
        * "lout" -- output light level
        * "temp" -- temperature
        * "rhum" -- relative humidity
        * "info" -- information string
        """

        # Drain any stale data from the RX buffer
        if hasattr(fp, 'any') and fp.any():
            fp.read(fp.any())

        fp.write(seasense.build_packet(self.address, command, "?").encode("ascii"))
        if self._local_echo:
            result = fp.read_until()

        result = fp.read_until()

        if len(result) == 0:
            # Zero-length response ... typically from a comms timeout
            raise DSPLTimeoutError

        elif result[0] == "?":
            # NACK
            raise DSPLNackError

        return result.decode("ascii").strip(WHITESPACE_TO_STRIP)

    def do_write(self, fp, command, data, verb="="):
        """
        Generic write function.

        Requires a pyserial fp, the four-character "command" to the device and
        the data to be sent.  Return True if the device ACKnolwedges the
        command, otherwise:

        * Raises DSPLTimeoutError if there is no response from the device
        * Raises DSPLNackError if it received "?" from the device
        """
        fp.write(
            seasense.build_packet(self.address, command, verb, data).encode("ascii")
        )
        if not self._expect_response:
            return True

        if self._local_echo:
            result = fp.read_until()

        result = fp.read_until()

        if len(result) == 0:
            # Zero-length response ... typically from a comms timeout
            raise DSPLTimeoutError

        elif result[0] == "?":
            # NACK
            raise DSPLNackError

        return True

    def do_increment(self, fp, command, data):
        return self.do_write(fp, command, data, verb="+")

    def do_decrement(self, fp, command, data):
        return self.do_write(fp, command, data, verb="-")

    def read_info(self, fp):
        """Read the info string (INFO) from the device.

        Currently returns the unparsed text string from the device.
        See the DSPL protocol manual for further details on the contents

        \todo{}:  Parse info string to extract meaning?  Only if we need to...
        """
        return self.do_query(fp, self.DSPL_VERB_INFO)

    def read_temperature(self, fp):
        """Read the temperature (TEMP) from the device in degrees C

        Raises ValueError if the value from the light can't be
        converted to a float.   May also raise DSPLTimeoutError
        or DPSLNackError.
        """
        result = self.do_query(fp, self.DSPL_VERB_TEMPERATURE)

        # This will throw an exception if a valid float isn't returned by the
        # light (e.g., a timeout or no response)
        return float(result)

    def read_humidity(self, fp):
        """Read the relative humidity (RHUM) from the device.


        Raises ValueError if the value from the light can't be
        converted to a float.   May also raise DSPLTimeoutError
        or DPSLNackError.
        """
        result = self.do_query(fp, self.DSPL_VERB_HUMIDITY)

        # This will throw an exception if a valid float isn't returned by the
        # light (e.g., a timeout or no response)
        return float(result)
