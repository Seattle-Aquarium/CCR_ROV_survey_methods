"""
Would this NMEA kill wl_ugps_external?

The WL UGPS External extension (v1.1.0-beta.1, the version on our vehicles)
listens on UDP 6200 and hands every sentence to pynmea2 1.19.0. Its listener
loop catches only socket errors, so *any* sentence pynmea2 cannot parse, or
any GGA/HDT with an empty number in it, raises out of the loop and the
listener thread dies. The web page keeps answering, and GGA and HDT go red
four seconds later and stay red until the extension is restarted.

This script reproduces that behavior without pynmea2 or the extension, so it
runs on a bare Windows Python:

    python nmea_check.py selftest
        Check the classifier against cases verified with the real pynmea2
        1.19.0 on 2026-10-14.

    python nmea_check.py listen --port 6201 --seconds 180 [--save FILE]
        Receive UDP datagrams (point a spare GNSS Compass data port at this
        laptop) and report, per sentence type, whether the extension would
        use it, ignore it, or die on it. Listen for at least 70 s: $GPHBT is
        sent only once a minute.

    python nmea_check.py file PATH
        Classify sentences from a text file, one per line.

Read-only: it binds a UDP port and listens. It never sends anything.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
from collections import Counter, defaultdict
from functools import reduce

# --- what pynmea2 1.19.0 knows --------------------------------------------------
# TalkerSentence.sentence_types, read from the 1.19.0 wheel. Anything else with
# a talker prefix (GPHEV, GPHBT, GPTHS, ...) raises SentenceTypeError.
KNOWN_TALKER_TYPES = {
    "AAM", "ALK", "ALM", "APA", "APB", "BEC", "BOD", "BWC", "BWR", "BWW", "DBS",
    "DBT", "DPT", "DTM", "GBS", "GGA", "GLL", "GNS", "GRS", "GSA", "GST", "GSV",
    "HDG", "HDM", "HDT", "HSC", "MDA", "MTW", "MWD", "MWV", "OSD", "R00", "RMA",
    "RMB", "RMC", "ROT", "RPM", "RSA", "RTE", "STN", "TLL", "TRF", "TTM", "TXT",
    "VBW", "VDR", "VHW", "VLW", "VPW", "VTG", "VWR", "VWT", "WCV", "WNC", "WPL",
    "XDR", "XTE", "ZDA",
}

# pynmea2's NMEASentence.sentence_re, verbatim.
SENTENCE_RE = re.compile(r'''
        ^\s*\$?
        (?P<nmea_str>
            (?P<sentence_type>
                (P\w{3})|
                (\w{2}\w{2}Q,\w{3})|
                (\w{2}\w{3},)
            )
            (?P<data>[^*]*)
        )
        (?:[*](?P<checksum>[A-F0-9]{2}))?
        \s*[\r\n]*$
        ''', re.X | re.IGNORECASE)
TALKER_RE = re.compile(r'^(?P<talker>\w{2})(?P<sentence>\w{3}),$')
QUERY_RE = re.compile(r'^(?P<talker>\w{2})(?P<listener>\w{2})Q,(?P<sentence>\w{3})$')
PROPRIETARY_RE = re.compile(r'^P(?P<manufacturer>\w{3})$')
DM_RE = re.compile(r'^(\d+)(\d\d\.\d+)$')

RECV_BUFFER = 1024  # the extension's sock.recvfrom(1024)

USED, IGNORED, FATAL = "used", "ignored", "FATAL"


def _float(s: str) -> float:
    return float(s)


def _dm(s: str) -> float:
    """pynmea2.nmea_utils.dm_to_sd."""
    if not s or s == "0":
        return 0.0
    m = DM_RE.match(s)
    if not m:
        raise ValueError(f"Geographic coordinate value '{s}' is not valid DDDMM.MMM")
    return float(m.group(1)) + float(m.group(2)) / 60


def classify_sentence(line: str) -> tuple[str, str, str]:
    """(type label, verdict, detail) for one sentence, as the extension sees it."""
    m = SENTENCE_RE.match(line)
    if not m:
        return "?", FATAL, "ParseError: could not parse data"
    nmea_str, data_str = m.group("nmea_str"), m.group("data")
    checksum, stype = m.group("checksum"), m.group("sentence_type").upper()
    data = data_str.split(",")
    if checksum:
        calc = reduce(lambda a, c: a ^ ord(c), nmea_str, 0)
        if int(checksum, 16) != calc:
            return stype.rstrip(","), FATAL, f"ChecksumError: {checksum} != {calc:02X}"

    t = TALKER_RE.match(stype)
    if t:
        label = t.group("talker") + t.group("sentence")
        kind = t.group("sentence")
        if kind not in KNOWN_TALKER_TYPES:
            return label, FATAL, f"SentenceTypeError: unknown sentence type {kind}"
        if kind == "GGA":
            try:
                # nmea_sentences.GGA.from_sentence + pynmea2's latitude/longitude
                _float(data[0]); _dm(data[1]); _dm(data[3])
                int(data[5]); int(data[6]); _float(data[7]); _float(data[8])
            except (ValueError, IndexError) as ex:
                return label, FATAL, f"{type(ex).__name__} converting GGA: {ex}"
            q = data[5]
            note = "" if q not in ("0", "6") else f" (fix quality {q})"
            return label, USED, "position" + note
        if kind == "HDT":
            try:
                _float(data[0])
            except (ValueError, IndexError) as ex:
                return label, FATAL, f"{type(ex).__name__} converting HDT: {ex}"
            return label, USED, "heading"
        return label, IGNORED, "parsed, not used"

    q = QUERY_RE.match(stype)
    if q and not data_str:
        # A QuerySentence carries the queried type as its sentence_type and no
        # data, so a query for GGA or HDT reaches from_sentence() and fails.
        if q.group("sentence") in ("GGA", "HDT"):
            return stype, FATAL, "IndexError: query sentence has no data"
        return stype, IGNORED, "query sentence"
    p = PROPRIETARY_RE.match(stype)
    if p:
        label = "P" + p.group("manufacturer")
        if p.group("manufacturer") == "ASH":
            # pynmea2's ASH.__new__ reads data[1] for every $PASH sentence.
            if len(data) < 2:
                return label, FATAL, "IndexError: $PASH sentence with fewer than two fields"
            if data[0] == "R":
                if not re.match(r"^\d{6}\.\d{2,3}$", data[1]):
                    return label + "R", IGNORED, ("PASHR with an unexpected timestamp -- "
                                                  "FATAL if the extension's LOG_NMEA is on")
                return label + "R", IGNORED, "PASHR (only logged, if LOG_NMEA is on)"
        return label, IGNORED, "proprietary, not used"
    return stype, FATAL, "ParseError: could not parse sentence type"


def classify_datagram(payload: bytes) -> list[tuple[str, str, str, str]]:
    """[(sentence text, type, verdict, detail)] for one UDP datagram.

    Stops at the first fatal sentence, because that is where the extension's
    listener thread stops.
    """
    out = []
    if len(payload) > RECV_BUFFER:
        out.append(("", "<datagram>", "warning",
                    f"{len(payload)} B; the extension reads only the first {RECV_BUFFER}"))
    payload = payload[:RECV_BUFFER]
    try:
        text = payload.decode()
    except UnicodeDecodeError as ex:
        out.append((repr(payload[:40]), "<binary>", FATAL, f"UnicodeDecodeError: {ex.reason}"))
        return out
    for s in text.split("\r\n"):
        if s == "":
            continue
        label, verdict, detail = classify_sentence(s)
        out.append((s, label, verdict, detail))
        if verdict == FATAL:
            break
    return out


def report(rows_by_type: dict, counts: Counter, first_seen: dict, last_seen: dict,
           fatal_examples: dict, datagrams: int, sizes: list[int], lf_only: int,
           elapsed: float) -> str:
    lines = [f"{datagrams} datagrams in {elapsed:.0f} s"]
    if sizes:
        lines.append(f"datagram size: min {min(sizes)} B, max {max(sizes)} B"
                     + ("  <-- larger than the extension's 1024 B read" if max(sizes) > RECV_BUFFER else ""))
    if lf_only:
        lines.append(f"{lf_only} datagram(s) used LF-only line endings with more than one "
                     "sentence -- the extension splits on CRLF only, so these are FATAL")
    lines.append("")
    lines.append(f"{'type':<10} {'count':>6} {'Hz':>6}  verdict  detail")
    for label in sorted(counts):
        span = last_seen[label] - first_seen[label]
        hz = (counts[label] - 1) / span if span > 0 and counts[label] > 1 else 0.0
        verdict, detail = rows_by_type[label]
        lines.append(f"{label:<10} {counts[label]:>6} {hz:>6.2f}  {verdict:<7}  {detail}")
    fatal = [k for k, (v, _) in rows_by_type.items() if v == FATAL]
    lines.append("")
    if fatal:
        lines.append("WOULD KILL wl_ugps_external's listener: " + ", ".join(sorted(fatal)))
        for k in sorted(fatal):
            lines.append(f"  e.g. {fatal_examples[k]!r}")
    else:
        lines.append("Nothing here would kill wl_ugps_external's listener.")
    have = {k[2:] for k in counts if len(k) == 5}
    for need in ("GGA", "HDT"):
        if need not in have:
            lines.append(f"MISSING: no {need} -- the extension sends nothing to the G2 "
                         "unless both GGA and HDT arrived in the last 4 s")
    return "\n".join(lines)


def cmd_listen(args) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(0.5)
    print(f"listening on {args.bind}:{args.port} for {args.seconds} s "
          "(Windows may ask to allow Python through the firewall)", flush=True)
    save = open(args.save, "w", encoding="utf-8") if args.save else None
    counts, rows, first, last, ex = Counter(), {}, {}, {}, {}
    sizes, lf_only, n = [], 0, 0
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.seconds:
            try:
                payload, src = sock.recvfrom(65535)
            except socket.timeout:
                continue
            now = time.monotonic()
            n += 1
            sizes.append(len(payload))
            if b"\n" in payload.rstrip(b"\r\n") and b"\r\n" not in payload:
                lf_only += 1
            if save:
                save.write(json.dumps({"t": time.time(), "src": f"{src[0]}:{src[1]}",
                                       "len": len(payload), "hex": payload.hex(),
                                       "text": payload.decode("ascii", "replace")}) + "\n")
            for s, label, verdict, detail in classify_datagram(payload):
                if label == "<datagram>":
                    continue
                counts[label] += 1
                first.setdefault(label, now)
                last[label] = now
                if label not in rows or verdict == FATAL:
                    rows[label] = (verdict, detail)
                if verdict == FATAL:
                    ex.setdefault(label, s)
                if args.verbose:
                    print(f"{src[0]} {verdict:<7} {s}")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if save:
            save.close()
    if n == 0:
        print("nothing received. Is a compass data port set to UDP Client -> this "
              f"laptop's IP, port {args.port}, NMEA output? Is the firewall allowing it?")
        return 2
    print(report(rows, counts, first, last, ex, n, sizes, lf_only, time.monotonic() - t0))
    return 1 if any(v == FATAL for v, _ in rows.values()) else 0


def cmd_file(args) -> int:
    worst = 0
    with open(args.path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            label, verdict, detail = classify_sentence(s)
            worst = max(worst, 1 if verdict == FATAL else 0)
            print(f"{verdict:<7} {label:<8} {detail:<45} {s}")
    return worst


def _cs(body: str) -> str:
    return f"${body}*{reduce(lambda a, c: a ^ ord(c), body, 0):02X}"


# Verified against pynmea2 1.19.0 + the extension's conversions on 2026-10-14
# (gnss_compass_debugging/listener_failure_modes.py).
SELFTEST = [
    (_cs("GPGGA,213044.000,4736.4546,N,12220.6343,W,1,12,0.8,12.3,M,-18.6,M,,"), USED),
    (_cs("GNGGA,213044.000,4736.4546,N,12220.6343,W,1,12,0.8,12.3,M,-18.6,M,,"), USED),
    (_cs("GPGGA,031644.460,3352.3501851,S,15112.2355488,E,6,00,1.4,150.0,M,0.0,M,,"), USED),
    (_cs("GPHDT,123.4,T"), USED),
    (_cs("HEHDT,123.4,T"), USED),
    (_cs("GPGLL,4736.4546,N,12220.6343,W,213044.000,A,A"), IGNORED),
    (_cs("GPGNS,213044.000,4736.4546,N,12220.6343,W,AAN,12,0.8,12.3,-18.6,,,S"), IGNORED),
    (_cs("GPGSA,A,3,01,03,06,09,14,17,19,22,,,,,1.5,0.8,1.2"), IGNORED),
    (_cs("GPGSV,3,1,12,01,40,083,46,03,52,281,48,06,15,045,42,09,64,123,50"), IGNORED),
    (_cs("GPHBT,60.0,A,1"), FATAL),
    (_cs("GPHDG,138.6,,,15.4,E"), IGNORED),
    (_cs("GPHDM,108.0,M"), IGNORED),
    (_cs("GPHEV,0.02,A"), FATAL),
    (_cs("GPRMC,213044.000,A,4736.4546,N,12220.6343,W,0.0,123.4,141026,15.4,E,A"), IGNORED),
    (_cs("GPROT,0.12,A"), IGNORED),
    (_cs("GPVBW,,,A,0.01,0.00,A,,V,,V"), IGNORED),
    (_cs("GPVLW,,N,,N,1.25,N,0.31,N"), IGNORED),
    (_cs("GPVTG,123.4,T,108.0,M,0.0,N,0.0,K,A"), IGNORED),
    (_cs("GPZDA,213044.000,14,10,2026,00,00"), IGNORED),
    (_cs("PASHR,213044.000,123.40,T,-0.30,+0.31,+0.01,0.029,0.029,0.059,1,1"), IGNORED),
    (_cs("PFEC,GPatt,123.4,-0.3,+0.3"), IGNORED),
    (_cs("PFEC,GPhve,0.02,A"), IGNORED),
    (":00FFFF -0012 0034 0056", FATAL),                       # TSS1
    (_cs("GPTHS,123.4,A"), FATAL),
    (_cs("GPGGA,213044.000,,,,,0,00,,,M,,M,,"), FATAL),       # no fix
    (_cs("GPGGA,213044.000,4736.4546,N,12220.6343,W,1,12,,12.3,M,-18.6,M,,"), FATAL),
    (_cs("GPGGA,213044.000,4736.4546,N,12220.6343,W,1,12,0.8,,M,,M,,"), FATAL),
    (_cs("GPHDT,,T"), FATAL),
    (_cs("GPGGA,213044.000,4736.4546,N,12220.6343,W,1,12,0.8,12.3,M,-18.6,M,,")[:-2] + "00", FATAL),
]


def cmd_selftest(_args) -> int:
    bad = 0
    for s, want in SELFTEST:
        label, got, detail = classify_sentence(s)
        ok = got == want
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {label:<8} {got:<7} {detail}")
    gga = _cs("GPGGA,213044.000,4736.4546,N,12220.6343,W,1,12,0.8,12.3,M,-18.6,M,,")
    hdt = _cs("GPHDT,123.4,T")
    crlf = classify_datagram((gga + "\r\n" + hdt + "\r\n").encode())
    lf = classify_datagram((gga + "\n" + hdt + "\n").encode())
    binary = classify_datagram(bytes([0xAA, 0x14, 0x20, 0x64, 0x00, 0xFF]) + b"\r\n")
    for name, rows, want in (("CRLF datagram", crlf, USED), ("LF-only datagram", lf, FATAL),
                             ("binary datagram", binary, FATAL)):
        got = rows[-1][2]
        ok = got == want
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {name:<16} {got:<7} {rows[-1][3]}")
    print("selftest", "PASSED" if not bad else f"FAILED ({bad})")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("listen")
    p.add_argument("--port", type=int, default=6201)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--seconds", type=float, default=180.0)
    p.add_argument("--save", help="write every datagram as JSON lines here")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_listen)
    p = sub.add_parser("file")
    p.add_argument("path")
    p.set_defaults(fn=cmd_file)
    p = sub.add_parser("selftest")
    p.set_defaults(fn=cmd_selftest)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
