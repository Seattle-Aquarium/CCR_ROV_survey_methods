"""
Read-only snapshot of the topside navigation chain:

    GNSS Compass --UDP NMEA--> 192.168.2.2:6200 wl_ugps_external (BlueOS)
        --HTTP PUT /api/v1/external/master--> UGPS G2 (192.168.2.94)

Every network request here is a GET (plus one optional ping sweep). Nothing
on the vehicle, the extension, the G2 or the compass is configured,
restarted or written. The only thing written is this script's own report
folder.

    python probe_topside.py                 # defaults for the ROV network
    python probe_topside.py --sweep         # also ping 192.168.2.1-254
    python probe_topside.py --g2 https://demo.waterlinked.com --blueos none

Standard library only, so it runs on any Python 3.9+ on the laptop.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

UA = "CCR gnss_compass_debugging probe (read-only)"
IS_WIN = sys.platform == "win32"


class Report:
    def __init__(self, out: Path):
        self.out = out
        self.lines: list[str] = []
        self.findings: list[str] = []

    def h(self, title: str) -> None:
        self.p("")
        self.p("=" * 78)
        self.p(title)
        self.p("=" * 78)

    def p(self, s: str = "") -> None:
        print(s, flush=True)
        self.lines.append(s)

    def find(self, s: str) -> None:
        self.findings.append(s)

    def raw(self, name: str, text: str) -> None:
        (self.out / name).write_text(text, encoding="utf-8", errors="replace")

    def close(self) -> None:
        self.h("FINDINGS (observations, not conclusions)")
        for f in self.findings or ["(none)"]:
            self.p(" * " + f)
        (self.out / "report.txt").write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        print(f"\nreport and raw files: {self.out}")


def get(url: str, timeout: float = 4.0) -> tuple[int | None, str, str]:
    """(status, body, error). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2_000_000).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as ex:
        body = ex.read(20_000).decode("utf-8", "replace") if ex.fp else ""
        return ex.code, body, f"HTTP {ex.code}"
    except Exception as ex:  # noqa: BLE001
        return None, "", f"{type(ex).__name__}: {ex}"


def get_json(url: str, timeout: float = 4.0):
    status, body, err = get(url, timeout)
    if err and status is None:
        return None, err
    try:
        return json.loads(body), "" if status == 200 else f"HTTP {status}"
    except Exception:  # noqa: BLE001
        return None, f"HTTP {status}, not JSON: {body[:120]!r}"


def stream(url: str, max_seconds: float, max_bytes: int) -> tuple[str, str]:
    """Read a streaming response until EOF, a deadline, or a byte cap."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    chunks, n, t0, note = [], 0, time.monotonic(), ""
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            while True:
                if time.monotonic() - t0 > max_seconds:
                    note = f"stopped after {max_seconds:.0f} s (stream still open)"
                    break
                try:
                    b = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                except socket.timeout:
                    note = "stream idle; stopped"
                    break
                if not b:
                    break
                chunks.append(b)
                n += len(b)
                if n >= max_bytes:
                    note = f"stopped at {max_bytes} bytes"
                    break
    except Exception as ex:  # noqa: BLE001
        return b"".join(chunks).decode("utf-8", "replace"), f"{type(ex).__name__}: {ex}"
    return b"".join(chunks).decode("utf-8", "replace"), note


def run(cmd: list[str], timeout: float = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return r.stdout + (r.stderr or "")
    except Exception as ex:  # noqa: BLE001
        return f"<{' '.join(cmd)} failed: {ex}>"


def haversine_m(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------
# 1. This laptop
# ---------------------------------------------------------------------------

def laptop(rep: Report) -> list[str]:
    rep.h("1. LAPTOP NETWORK")
    if IS_WIN:
        ipc = run(["ipconfig", "/all"])
        rep.raw("ipconfig_all.txt", ipc)
        rep.raw("route_print.txt", run(["route", "print", "-4"]))
        rep.raw("arp_before.txt", run(["arp", "-a"]))
        keep = re.compile(r"^\S.*adapter|IPv4 Address|Subnet Mask|Default Gateway|"
                          r"Physical Address|Media State|Description|Autoconfiguration IPv4", re.I)
        for line in ipc.splitlines():
            if keep.search(line):
                rep.p(line.rstrip())
        addrs = re.findall(r"IPv4 Address[ .]*: ([\d.]+)", ipc)
        addrs += re.findall(r"Autoconfiguration IPv4 Address[ .]*: ([\d.]+)", ipc)
    else:
        txt = run(["ip", "-4", "addr"])
        rep.raw("ip_addr.txt", txt)
        rep.raw("ip_route.txt", run(["ip", "route"]))
        rep.raw("ip_neigh.txt", run(["ip", "neigh"]))
        rep.p(txt)
        addrs = re.findall(r"inet ([\d.]+)/", txt)
    on_rov = [a for a in addrs if a.startswith("192.168.2.")]
    rep.p("")
    rep.p(f"IPv4 addresses on this laptop: {', '.join(addrs) or 'none'}")
    if not on_rov:
        rep.find("No laptop address in 192.168.2.0/24 -- the bridge/tether adapter "
                 "has no ROV-network address (see ipconfig_all.txt)")
    return addrs


# ---------------------------------------------------------------------------
# 2. The compass
# ---------------------------------------------------------------------------

def compass(rep: Report, name: str) -> str | None:
    rep.h(f"2. GNSS COMPASS ({name})")
    ip = None
    try:
        infos = socket.getaddrinfo(name, 80, socket.AF_INET, socket.SOCK_STREAM)
        ips = sorted({i[4][0] for i in infos})
        ip = ips[0]
        rep.p(f"resolves to: {', '.join(ips)}")
    except Exception as ex:  # noqa: BLE001
        rep.p(f"does not resolve: {ex}")
        rep.find(f"{name} does not resolve (mDNS/Bonjour, or the compass is off the "
                 "network) -- use --sweep and the ARP table to find it")
        return None
    status, body, err = get(f"http://{ip}/", timeout=4)
    title = re.search(r"<title>(.*?)</title>", body or "", re.I | re.S)
    rep.p(f"web UI http://{ip}/ -> {status if status else err}"
          + (f", title {title.group(1).strip()!r}" if title else ""))
    try:
        addr = ipaddress.ip_address(ip)
        if addr in ipaddress.ip_network("169.254.0.0/16"):
            rep.find(f"Compass is on a LINK-LOCAL address ({ip}): it is in DHCP mode "
                     "with no DHCP server, so it cannot reach 192.168.2.2")
        elif addr not in ipaddress.ip_network("192.168.2.0/24"):
            rep.find(f"Compass address {ip} is OUTSIDE 192.168.2.0/24 -- its UDP "
                     "output to 192.168.2.2:6200 has no route unless it has a gateway")
        else:
            rep.p(f"{ip} is inside 192.168.2.0/24")
    except ValueError:
        pass
    return ip


def sweep(rep: Report, net: str = "192.168.2.0/24") -> None:
    rep.h(f"2b. PING SWEEP {net} (who is on the ROV network)")
    hosts = [str(h) for h in ipaddress.ip_network(net).hosts()]

    def ping(h):
        cmd = (["ping", "-n", "1", "-w", "400", h] if IS_WIN
               else ["ping", "-c", "1", "-W", "1", h])
        out = run(cmd, timeout=5)
        return h, ("TTL=" in out.upper())

    with ThreadPoolExecutor(max_workers=48) as ex:
        alive = [h for h, ok in ex.map(ping, hosts) if ok]
    arp = run(["arp", "-a"]) if IS_WIN else run(["ip", "neigh"])
    rep.raw("arp_after_sweep.txt", arp)
    rep.p(f"answered ping: {', '.join(alive) or 'none'}")
    rep.p("ARP entries on 192.168.2.x (a host that ignores ping can still appear here):")
    for line in arp.splitlines():
        if "192.168.2." in line:
            rep.p("  " + line.strip())
    known = {"192.168.2.1": "topside laptop (usual)", "192.168.2.2": "BlueOS / Raspberry Pi",
             "192.168.2.94": "UGPS G2 (usual)"}
    for h in alive:
        if h in known:
            rep.p(f"  {h}: {known[h]}")


# ---------------------------------------------------------------------------
# 3. BlueOS and the extension
# ---------------------------------------------------------------------------

def blueos(rep: Report, host: str) -> dict:
    rep.h(f"3. BLUEOS ({host}) AND wl_ugps_external")
    found: dict = {}
    services, err = get_json(f"http://{host}/helper/latest/web_services", 6)
    if services is None:
        services, err = get_json(f"http://{host}/helper/v1.0/web_services", 6)
    if services is None:
        rep.p(f"BlueOS service list unavailable: {err}")
        rep.find(f"BlueOS at {host} did not answer /helper/.../web_services ({err})")
        return found
    rep.raw("blueos_web_services.json", json.dumps(services, indent=1))
    entries = services if isinstance(services, list) else services.get("services", [])
    for e in entries:
        name = str(e.get("name") or e.get("title") or "")
        port = e.get("port")
        if "ugps" in name.lower().replace("-", " ") or "dvl" in name.lower():
            rep.p(f"service: {name!r} on port {port}")
        if "external" in name.lower() and "ugps" in name.lower().replace("-", " "):
            found["ext_port"] = port
    port = found.get("ext_port")
    bare = host.rsplit(":", 1)[0]  # the extension answers on its own port
    if not port:
        rep.find("WL UGPS External is not in BlueOS's service list -- extension "
                 "not installed, disabled, or not running")
    else:
        rep.p("")
        rep.p(f"GET http://{bare}:{port}/status, six samples one second apart:")
        samples = []
        for _ in range(6):
            st, err = get_json(f"http://{bare}:{port}/status", 3)
            samples.append(st if st is not None else {"error": err})
            rep.p("  " + json.dumps(samples[-1]))
            time.sleep(1)
        rep.raw("extension_status_samples.json", json.dumps(samples, indent=1))
        ok = [s for s in samples if "error" not in s]
        if ok:
            last = ok[-1]
            found["status"] = last
            g = sum(bool(s.get("gga_status")) for s in ok)
            h = sum(bool(s.get("hdt_status")) for s in ok)
            i = sum(bool(s.get("inject_status")) for s in ok)
            rep.p(f"  gga true {g}/{len(ok)}, hdt true {h}/{len(ok)}, inject true {i}/{len(ok)}")
            if g == 0 and h == 0:
                rep.find("Extension reports NO GGA and NO HDT in the last 4 s (nothing "
                         "arriving on UDP 6200, or the listener thread has died -- check "
                         "the container log below for a Traceback)")
            elif g == 0 or h == 0:
                rep.find(f"Extension gets {'HDT but no GGA' if g == 0 else 'GGA but no HDT'}"
                         " -- it sends nothing to the G2 unless BOTH are fresh")
            if g and h and i == 0:
                rep.find("GGA and HDT arrive but injection into the G2 is failing -- see "
                         "'Got HTTP Error'/'Got exception' lines in the log, and the G2 section")
            if g and h and i:
                rep.find("Extension: GGA, HDT and injection all OK at probe time")
            found["ugps_host"] = last.get("ugps_host")

    # Installed extension record (env vars live here).
    inst, err = get_json(f"http://{host}/kraken/v1.0/installed_extensions", 6)
    if inst is not None:
        rep.raw("kraken_installed_extensions.json", json.dumps(inst, indent=1))
        for e in inst if isinstance(inst, list) else []:
            if "ugps" in json.dumps(e).lower():
                rep.p("")
                rep.p("installed: " + json.dumps({k: e.get(k) for k in
                      ("identifier", "name", "docker", "tag", "enabled")}))
                perms = e.get("user_permissions") or ""
                if perms:
                    rep.p(f"  user_permissions (custom settings): {perms}")
    else:
        rep.p(f"kraken installed_extensions unavailable: {err}")

    # Container log: the listener thread's death leaves a traceback here.
    conts, err = get_json(f"http://{host}/kraken/v1.0/list_containers", 6)
    if conts is None:
        rep.p(f"kraken list_containers unavailable: {err}")
        return found
    rep.raw("kraken_containers.json", json.dumps(conts, indent=1))
    target = None
    for c in conts if isinstance(conts, list) else []:
        blob = (str(c.get("name")) + " " + str(c.get("image"))).lower()
        if "ugps" in blob and "external" in blob.replace("_", "").replace("-", ""):
            target = c
    if target is None:
        for c in conts if isinstance(conts, list) else []:
            if "wl_ugps_external" in json.dumps(c).lower() or "wlugpsexternal" in json.dumps(c).lower():
                target = c
    if target is None:
        rep.p("no running container looks like wl_ugps_external; running containers:")
        for c in conts if isinstance(conts, list) else []:
            rep.p(f"  {c.get('name')}  {c.get('image')}  {c.get('status')}")
        rep.find("No running wl_ugps_external container")
        return found
    cname = str(target.get("name")).lstrip("/")
    rep.p("")
    rep.p(f"container: {cname}  image {target.get('image')}  status {target.get('status')}")
    log, note = "", ""
    for name in (cname, "/" + cname):
        url = f"http://{host}/kraken/v1.0/log?container_name={urllib.request.quote(name)}&timeout=3"
        log, note = stream(url, max_seconds=25, max_bytes=8_000_000)
        if log:
            break
    rep.raw("extension_container.log", log)
    rep.p(f"container log: {len(log.splitlines())} lines saved ({note or 'complete'})")
    analyse_log(rep, log)
    return found


def analyse_log(rep: Report, log: str) -> None:
    lines = log.splitlines()
    types = []
    for ln in lines:
        m = re.search(r"Receiving (\S+) messages", ln)
        if m and m.group(1) not in types:
            types.append(m.group(1))
    starts = [ln for ln in lines if "Listening for NMEA messages" in ln]
    rep.p(f"  extension starts in this log: {len(starts)}")
    rep.p(f"  sentence types it reported receiving: {', '.join(types) or 'none'}")
    tb = [i for i, ln in enumerate(lines) if "Traceback" in ln]
    if tb:
        rep.p(f"  TRACEBACKS: {len(tb)}. Last one:")
        i = tb[-1]
        block = []
        for ln in lines[i:i + 40]:
            block.append(ln)
            if re.match(r"^\s*\w*(Error|Exception)\b", ln) or re.search(r"\w+Error: ", ln):
                break
        for ln in block:
            rep.p("    " + ln)
        exc = block[-1].strip() if block else ""
        thread = any("listen" in b.lower() for b in block)
        rep.find(f"Container log has a traceback{' in the NMEA listener thread' if thread else ''}: "
                 f"{exc[:200]}")
        if "Unknown sentence type" in exc:
            rep.find("-> the compass is sending a sentence type pynmea2 1.19 does not know; "
                     "disable it on the compass data port (GPHEV and GPHBT are the usual ones)")
    http_err = [ln for ln in lines if "Got HTTP Error" in ln or "Got exception" in ln]
    if http_err:
        rep.p(f"  G2 injection errors: {len(http_err)}; last: {http_err[-1][:200]}")
        rep.find(f"Extension's PUTs to the G2 fail: {http_err[-1][-160:]}")
    sends = [ln for ln in lines if "Sending {" in ln]
    if sends:
        rep.p(f"  'Sending' lines: {len(sends)}; last: {sends[-1][:200]}")


# ---------------------------------------------------------------------------
# 4. The G2
# ---------------------------------------------------------------------------

def g2(rep: Report, base: str, ext: dict) -> None:
    rep.h(f"4. UGPS G2 ({base})")
    out = {}
    for path in ("/api/v1/about", "/api/v1/config/generic", "/api/v1/about/status",
                 "/api/v1/position/master", "/api/v1/status_report/",
                 "/api/v1/position/acoustic/filtered", "/api/v1/config/ip",
                 "/api/v1/config/antenna"):
        js, err = get_json(base.rstrip("/") + path, 5)
        out[path] = js if js is not None else {"error": err}
        txt = json.dumps(out[path])
        rep.p(f"{path}: {txt[:600]}{' ...' if len(txt) > 600 else ''}")
    rep.raw("g2_api.json", json.dumps(out, indent=1))

    cfg = out.get("/api/v1/config/generic") or {}
    if "error" not in cfg:
        if cfg.get("gps") != "external":
            rep.find(f"G2 'gps' setting is {cfg.get('gps')!r}, not 'external' -- the G2 "
                     "ignores /external/master positions in that mode")
        if cfg.get("compass") != "external":
            rep.find(f"G2 'compass' setting is {cfg.get('compass')!r}, not 'external'")
    else:
        rep.find(f"G2 API unreachable at {base}: {cfg.get('error')}")
    for grp in out.get("/api/v1/status_report/") or []:
        if isinstance(grp, dict) and grp.get("status") != "ok":
            rep.find(f"G2 status group {grp.get('status_group')!r}: {grp.get('status')} "
                     f"-- {grp.get('message')}")
    master = out.get("/api/v1/position/master") or {}
    st = ext.get("status") or {}
    if "lat" in master and st.get("gga_status"):
        try:
            d = haversine_m((master["lat"], master["lon"]), (st["latitude"], st["longitude"]))
            rep.p(f"G2 master position vs extension's GGA: {d:.1f} m apart; "
                  f"G2 orientation {master.get('orientation')} vs HDT {st.get('heading')}")
            if d > 5:
                rep.find(f"G2 master position is {d:.0f} m from the compass GGA -- the G2 "
                         "is not using the external position")
        except Exception:  # noqa: BLE001
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blueos", default="192.168.2.2", help="BlueOS host, or 'none'")
    ap.add_argument("--g2", default="http://192.168.2.94", help="G2 base URL, or 'none'")
    ap.add_argument("--compass", default="gnss-compass.local", help="compass host, or 'none'")
    ap.add_argument("--sweep", action="store_true", help="ping 192.168.2.1-254 and list ARP")
    ap.add_argument("--out", default=None, help="report folder (default: probe_output/<time>)")
    args = ap.parse_args(argv)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "probe_output" / stamp
    out.mkdir(parents=True, exist_ok=True)
    rep = Report(out)
    rep.p(f"probe_topside.py  {datetime.now().isoformat(timespec='seconds')}  "
          f"python {sys.version.split()[0]}  {sys.platform}")

    laptop(rep)
    if args.compass.lower() != "none":
        compass(rep, args.compass)
    if args.sweep:
        sweep(rep)
    ext = blueos(rep, args.blueos) if args.blueos.lower() != "none" else {}
    if args.g2.lower() != "none":
        g2(rep, args.g2, ext)
    rep.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
