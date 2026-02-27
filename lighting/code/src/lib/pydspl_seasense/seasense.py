def build_packet(address, cmd, access, data=None, do_checksum=True):
    """
    "Assembles" a valid DSPL protocol packet with the requested parameters.

    * address is the integer address for the device in the range 0-999
    * cmd is a four-letter "verb" string
    * access is the access code, one of ['=', '?', '+', '-']
    * data is string which is included verbatim in the packet
    * If do_checksum is true, a checksum will be appended.

    This function automatically includes a line ending (CR-LF) in the packet
    """

    # Validation
    if address is None:
        raise ValueError

    # Address must be integer between 0 and 999
    address = int(address)
    if address < 0 or address > 999:
        raise ValueError

    # cmd must be a four-letter string
    if len(str(cmd)) != 4:
        raise ValueError

    # None is also a valid access code
    if access and access not in "=?+-":  # ['=', '?', '+', '-']:
        raise ValueError

    out = "!{:03d}:{:4s}{:1s}{:s}".format(
        address,
        cmd,
        access[0] if access else "",
        data if data else "",
    )

    if do_checksum:
        out = out + "*"
        chksum = sum(out.encode("ascii")) % 256

        out = out + "{:02x}".format(chksum)

    out = out + "\r\n"

    return out
