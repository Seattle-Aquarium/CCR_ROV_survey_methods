"""The plain-language NWS marine forecast.

api.weather.gov has no JSON marine-zone forecast, so we pull the Coastal Waters
Forecast (CWF) text product for the responsible office and lift out the synopsis
and the block for our zone -- including the "Wave Detail: NW 6 ft at 7 seconds
and S 3 ft at 12 seconds" lines, which are the best swell description available
for the outer coast.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import config
from ._http import fetch_json

POINTS = "https://api.weather.gov/points/{lat},{lon}"
PROD_LIST = "https://api.weather.gov/products/types/{typ}/locations/{office}"
PROD = "https://api.weather.gov/products/{pid}"


@dataclass
class MarineNarrative:
    office: str
    zone_id: str
    area: str = ""
    synopsis: str = ""
    advisory: str = ""                       # e.g. "SMALL CRAFT ADVISORY IN EFFECT..."
    periods: list[tuple[str, str]] = field(default_factory=list)  # (name, text)
    issued: str = ""
    product_type: str = "CWF"

    def bool(self) -> bool:  # truthy if we got anything useful
        return bool(self.synopsis or self.periods)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_block(block: str, zone_id: str) -> MarineNarrative | None:
    lines = block.strip().splitlines()
    if not lines:
        return None
    header = lines[0].strip()
    if not header.startswith(zone_id):
        return None
    # The area description wraps across 1-2 lines, then a "HHMM AM/PM ... YYYY" stamp.
    area_lines, i = [], 1
    while i < len(lines) and not re.match(r"\s*\d{1,4}\s*[AP]M\b", lines[i]):
        area_lines.append(lines[i].strip())
        i += 1
    area = " ".join(area_lines).strip().rstrip("-").strip()
    body = "\n".join(lines[i:])

    adv = ""
    m = re.search(r"\n\.\.\.(.+?)\.\.\.", body, re.S)
    if m and ("advisory" in m.group(1).lower() or "warning" in m.group(1).lower()
              or "statement" in m.group(1).lower()):
        adv = _clean(m.group(1))
    # Drop the "...HEADER..." lines so they are not also parsed as periods.
    body = re.sub(r"\n\.\.\..+?\.\.\.\s*", "\n", body, flags=re.S)

    periods: list[tuple[str, str]] = []
    for pm in re.finditer(r"\n\.([A-Z][A-Za-z0-9 ]+?)\.\.\.(.+?)(?=\n\.[A-Z]|\Z)", body, re.S):
        periods.append((pm.group(1).title().strip(), _clean(pm.group(2))))
    return MarineNarrative(office="", zone_id=zone_id, area=area,
                           advisory=adv, periods=periods[:4])


def get_marine_narrative(lat: float, lon: float, *,
                         force: bool = False) -> MarineNarrative | None:
    try:
        pts, _ = fetch_json(POINTS.format(lat=lat, lon=lon),
                            ttl=config.CACHE_TTL_STATIC_S, force=force)
        p = pts["properties"]
        office = p.get("gridId") or p.get("cwa") or ""
        zone_id = (p.get("forecastZone") or "").rsplit("/", 1)[-1]
    except Exception:
        return None
    if not office:
        return None

    for typ in ("CWF", "NSH", "OFF"):
        try:
            lst, _ = fetch_json(PROD_LIST.format(typ=typ, office=office), ttl=1800,
                                force=force)
            graph = lst.get("@graph") or []
            if not graph:
                continue
            prod, _ = fetch_json(PROD.format(pid=graph[0]["id"]), ttl=1800, force=force)
            text = prod.get("productText", "")
            issued = graph[0].get("issuanceTime", "")

            blocks = text.split("$$")
            synopsis = ""
            for b in blocks:
                if ".synopsis" in b.lower():
                    sm = (re.search(r"\.synopsis.*\.\.\.\s*\n\s*\n(.+)", b, re.S | re.I)
                          or re.search(r"\.synopsis.*?\.\.\.(.+)", b, re.S | re.I))
                    if sm:
                        synopsis = _clean(sm.group(1))
                    break
            for b in blocks:
                nar = _parse_block(b, zone_id) if zone_id else None
                if nar and (nar.periods or nar.advisory):
                    nar.office = office
                    nar.synopsis = synopsis
                    nar.issued = issued
                    nar.product_type = typ
                    return nar
            if synopsis:
                return MarineNarrative(office=office, zone_id=zone_id,
                                       synopsis=synopsis, issued=issued,
                                       product_type=typ)
        except Exception:
            continue
    return None
