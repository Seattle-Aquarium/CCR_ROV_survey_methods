"""
Asking one light one question on the shared RS-485 bus, and waiting no longer
than it takes to answer.

SeaSense is strict command/response: a light speaks only when asked (DeepSea's
SeaSense Serial Protocol Specification, p.6), so every piece of telemetry is a
question and its answer. On a shared half-duplex bus two rules follow:

* **One question at a time.** The next question goes out only after the
  answer to this one has arrived or its time is up. Aaron Marburg saw garbled
  traffic with 10 ms between commands (CCR_development#42); his telemetry
  build asked at ~25 ms intervals and was clean.
* **A hard deadline.** pydspl's `do_query` reads through `UART.read_until`,
  which waits the UART's whole timeout -- two seconds here -- for every
  character that does not come. A light that is dark or unpowered is exactly
  the one we most want to ask about, and it must cost a short, fixed time,
  not stall the controller.

Replies, as observed and as specified:

* single-line answers (LOUT?, TEMP?, RHUM?, STBY?, INFO?) end CR LF -- the
  spec shows `100<CR>` for LOUT? in one place, so a bare CR followed by a
  pause is accepted too;
* STAT? answers with a header row, a value row and a blank row, each CR LF.
  On Lutris's lights (LSL-2075, firmware unknown) the value row has eight
  columns, not the spec's five (Aaron's telemetry build, CCR_development#50);
  the header is kept verbatim so the reader can name them;
* a NAK is "?" CR LF;
* nothing at all is a timeout.

What comes back is passed on raw, as text. Parsing is the reader's job.
"""

import time

import seasense

#: How long a light gets to answer, from the end of the question.
TIMEOUT_MS = 250
#: STAT? is three lines (~90 characters at 9600 baud is ~95 ms on its own).
STAT_TIMEOUT_MS = 450
#: A reply that has stopped arriving for this long is complete, whatever its
#: terminator -- for the bare-CR form of LOUT?.
IDLE_MS = 20

MULTILINE = ("stat", "pset", "boot")


class LightBus:
    def __init__(
        self,
        uart,
        timeout_ms=TIMEOUT_MS,
        stat_timeout_ms=STAT_TIMEOUT_MS,
        idle_ms=IDLE_MS,
        local_echo=False,
    ):
        self.uart = uart
        self.timeout_ms = timeout_ms
        self.stat_timeout_ms = stat_timeout_ms
        self.idle_ms = idle_ms

        self._expect_local_echo = local_echo

        self.asked = 0
        self.answered = 0
        self.failed = 0

        self.send_failed = 0

    def _drain(self):
        """Throw away whatever is waiting -- the ACKs to earlier LOUT= writes."""
        try:
            n = self.uart.any()
            if n:
                self.uart.read(n)
        except Exception:
            pass

    def set_level(self, address, lout):
        """
        Set the current light level

        Args:
            address (hex): Address of light
            lout (int): Light level from 0-100

        Returns: 4-tuple
            ok (int):  0 for "error", 1 for "ok"
            data (str): Response from light (if any), or empty
            err (str): Error or empty string
            rtt (float): response time in ms
        """

        lout = int(lout)
        if lout < 0 or lout > 100:
            return 0, "", "", 0

        sent_packet = seasense.build_packet(address, "lout", "=", f"{lout}")

        try:
            self.uart.write(
                sent_packet.encode("ascii")
                if isinstance(sent_packet, str)
                else sent_packet
            )
            self.uart.flush()
        except Exception:
            self.send_failed += 1
            return 0, "", "", 0

        return self._read_response(sent_packet)

    def query(self, address, command):
        """
        Ask `address` for `command`. Returns (ok, raw_text, error, rtt_ms).

        `error` is "" on success, else "timeout", "nak", "partial" or the
        exception. `raw_text` is whatever arrived, decoded, even on failure.
        """

        multi = command in MULTILINE
        self._drain()
        sent_packet = seasense.build_packet(address, command, "?")
        self.asked += 1

        try:
            self.uart.write(
                sent_packet.encode("ascii")
                if isinstance(sent_packet, str)
                else sent_packet
            )
            self.uart.flush()
        except Exception as ex:
            self.failed += 1
            return 0, "", "write: " + repr(ex), 0

        return self._read_response(sent_packet, multi=multi)

    def _read_response(self, sent_packet=None, multi=False):

        _limit = self.stat_timeout_ms if multi else self.timeout_ms

        # With t0 here, it's now "response time" not "round trip time"
        # but c'est la vie
        t0 = time.ticks_ms()

        # This is very much worse than the existing code, but it works!
        if self._expect_local_echo:
            _ = self.uart.read_until()

        buf = self.uart.read_until()

        if multi:
            buf += self.uart.read_until()

        # Code block is from Claude, I can't figure it out, but it doesn't work
        #
        # # bytes, not bytearray: MicroPython's bytearray has no count() or
        # # endswith(). A reply is under 120 bytes, so copying is nothing.
        # buf = b""
        # last_rx = None

        # done = False

        # while True:
        #     now = time.ticks_ms()
        #     if time.ticks_diff(now, t0) >= limit:
        #         break
        #     n = self.uart.any()
        #     if n:
        #         chunk = self.uart.read(n)
        #         if chunk:
        #             buf += bytes(chunk)
        #             last_rx = now

        #             print(f'>{buf}<')

        #             if _complete(buf, multi):
        #                 done = True
        #                 break
        #         continue

        #     if (last_rx is not None and not multi and buf.endswith(b"\r")
        #             and time.ticks_diff(now, last_rx) >= self.idle_ms):
        #         done = True
        #         break
        #     time.sleep_ms(1)

        rtt = time.ticks_diff(time.ticks_ms(), t0)
        text = _decode(buf, sent_packet)

        if not text.strip():
            self.failed += 1
            return 0, text, "timeout", rtt

        if text.strip() == "?":
            self.failed += 1
            return 0, text, "nak", rtt

        # if not done and multi and text.count("\n") >= 2:
        #     # Header and values arrived but not the closing blank line: usable.
        #     done = True

        # if not done:
        #     self.failed += 1
        #     return 0, text, "partial", rtt

        self.answered += 1
        return 1, text.strip(), "", rtt


def _complete(buf, multi):
    if multi:
        # header CR LF, values CR LF, blank CR LF
        return (
            buf.count(b"\n") >= 3 or buf.endswith(b"\r\n\r\n") or buf.endswith(b"\n\n")
        )
    return b"\n" in buf


def _decode(data, sent_packet=None):
    """Bytes to text, minus NULs and any echo of our own question."""
    try:
        text = data.decode("ascii")
    except Exception:
        text = "".join(
            chr(b) if 32 <= b < 127 or b in (9, 10, 13) else "?" for b in data
        )

    text = text.replace("\x00", "")

    # Some half-duplex transceivers hear their own transmission. Lutris's do
    # not, as far as anyone has seen, but an echo would otherwise be read as
    # the answer.
    #
    # n.b. this is currently handled "manually" by specifying self._local_echo
    # rather than automatically handling both cases
    sent = sent_packet.strip() if isinstance(sent_packet, str) else ""

    if sent and text.startswith(sent):
        text = text[len(sent) :].lstrip("\r\n")

    return text
