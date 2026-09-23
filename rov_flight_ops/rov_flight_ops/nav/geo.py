"""
Distance, bearing and local-frame projection on the WGS-84 ellipsoid.

Written out rather than pulled in. `geopy`/`geographiclib` would do this and
the transect extractor already uses them, but ROV Flight Operations does not
depend on either, and adding two packages to a field laptop's environment for
four functions is the wrong trade -- especially when the launcher installs from
a pinned constraints file and every new name is another thing that can fail to
install at a dock with no signal.

The formulae are Vincenty's, with a documented spherical fallback for the one
case Vincenty does not converge on (near-antipodal points, which cannot arise
between an ROV and its own support vessel but is handled rather than hung).
Agreement with geographiclib was checked to better than a millimeter over the
distances this program sees; the test suite pins a handful of those values.

**Bearings here are true, clockwise from north, in degrees.** There is no
magnetic variation anywhere in this module: the ROV's compass heading and a
geodesic bearing between two positions are different quantities with different
north references, and anything that wants to compare them has to say so at the
point of comparison rather than have this module quietly reconcile them.
"""

from __future__ import annotations

import math

# WGS-84.
A = 6378137.0                     # semi-major axis, meters
F = 1.0 / 298.257223563           # flattening
B = A * (1.0 - F)                 # semi-minor axis

#: Vincenty's inverse is iterative. Twenty passes is far more than the handful
#: any realistic ROV-to-vessel pair needs; the cap exists for the antipodal
#: case, where it would otherwise not converge at all.
_MAX_ITER = 20
_TOL = 1e-12

#: Below this separation a bearing is not a direction, it is noise. Two points
#: a centimeter apart have a perfectly well-defined bearing and it swings
#: through 360 degrees as either one jitters, which on a display reads as a
#: needle spinning. Callers are told "at/near target" instead.
MIN_BEARING_M = 1.0


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Geodesic distance in meters."""
    return inverse(lat1, lon1, lat2, lon2)[0]


def initial_bearing_deg(lat1: float, lon1: float,
                        lat2: float, lon2: float) -> float | None:
    """Initial bearing from 1 to 2, degrees true, or None if too close.

    *Initial*: on a geodesic the bearing changes along the path. Over the few
    hundred meters between an ROV and its vessel the difference is far below
    the precision of anything steering by it, but the name is honest about
    which one this is.
    """
    d, brg, _ = inverse(lat1, lon1, lat2, lon2)
    if d < MIN_BEARING_M:
        return None
    return brg


def inverse(lat1: float, lon1: float,
            lat2: float, lon2: float) -> tuple[float, float, float]:
    """(distance m, initial bearing deg, final bearing deg) on WGS-84.

    Vincenty's inverse method. Falls back to the great-circle solution on a
    sphere of the mean radius if it fails to converge, which keeps a
    pathological input from hanging a redraw; the fallback is accurate to a few
    tenths of a percent and is only ever reached for point pairs this
    application cannot actually produce.
    """
    if lat1 == lat2 and lon1 == lon2:
        return 0.0, 0.0, 0.0

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    # Wrapped, so a pair straddling the antimeridian takes the short way round
    # rather than three-quarters of the way about the planet.
    ll = math.radians(wrap180(lon2 - lon1))

    u1 = math.atan((1 - F) * math.tan(phi1))
    u2 = math.atan((1 - F) * math.tan(phi2))
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)

    lam = ll
    sin_sigma = cos_sigma = sigma = sin_alpha = cos2_alpha = cos_2sigma_m = 0.0
    for _ in range(_MAX_ITER):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(cos_u2 * sin_lam,
                               cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
        if sin_sigma == 0.0:
            return 0.0, 0.0, 0.0          # coincident
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos2_alpha = 1 - sin_alpha ** 2
        # Equatorial lines have no sigma_m; the zero is the documented
        # convention, not a missing value.
        cos_2sigma_m = (cos_sigma - 2 * sin_u1 * sin_u2 / cos2_alpha
                        if cos2_alpha != 0.0 else 0.0)
        c = F / 16 * cos2_alpha * (4 + F * (4 - 3 * cos2_alpha))
        lam_prev = lam
        lam = ll + (1 - c) * F * sin_alpha * (
            sigma + c * sin_sigma * (
                cos_2sigma_m + c * cos_sigma * (-1 + 2 * cos_2sigma_m ** 2)))
        if abs(lam - lam_prev) < _TOL:
            break
    else:
        return _spherical(phi1, phi2, ll)

    u_sq = cos2_alpha * (A ** 2 - B ** 2) / (B ** 2)
    k1 = (math.sqrt(1 + u_sq) - 1) / (math.sqrt(1 + u_sq) + 1)
    aa = (1 + k1 ** 2 / 4) / (1 - k1)
    bb = k1 * (1 - 3 * k1 ** 2 / 8)
    d_sigma = bb * sin_sigma * (
        cos_2sigma_m + bb / 4 * (
            cos_sigma * (-1 + 2 * cos_2sigma_m ** 2)
            - bb / 6 * cos_2sigma_m * (-3 + 4 * sin_sigma ** 2)
            * (-3 + 4 * cos_2sigma_m ** 2)))
    s = B * aa * (sigma - d_sigma)

    sin_lam, cos_lam = math.sin(lam), math.cos(lam)
    a1 = math.atan2(cos_u2 * sin_lam,
                    cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
    a2 = math.atan2(cos_u1 * sin_lam,
                    -sin_u1 * cos_u2 + cos_u1 * sin_u2 * cos_lam)
    return s, wrap360(math.degrees(a1)), wrap360(math.degrees(a2))


def _spherical(phi1: float, phi2: float, dlon: float) -> tuple[float, float, float]:
    """Great-circle fallback, on a sphere of the WGS-84 mean radius."""
    r = (2 * A + B) / 3.0
    dphi = phi2 - phi1
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2)
    s = r * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
    y = math.sin(dlon) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlon))
    brg = wrap360(math.degrees(math.atan2(y, x)))
    return s, brg, brg


def destination(lat: float, lon: float, bearing_deg: float,
                distance_m_: float) -> tuple[float, float]:
    """Where you arrive going `bearing_deg` true for `distance_m_` meters.

    Vincenty's direct method. Used to lay a dead-reckoned track down from a
    known origin, which is the whole of DVL-only mapping.
    """
    if distance_m_ == 0.0:
        return lat, lon
    phi1 = math.radians(lat)
    alpha1 = math.radians(bearing_deg)
    sin_a1, cos_a1 = math.sin(alpha1), math.cos(alpha1)

    tan_u1 = (1 - F) * math.tan(phi1)
    cos_u1 = 1 / math.sqrt(1 + tan_u1 ** 2)
    sin_u1 = tan_u1 * cos_u1
    sigma1 = math.atan2(tan_u1, cos_a1)
    sin_alpha = cos_u1 * sin_a1
    cos2_alpha = 1 - sin_alpha ** 2
    u_sq = cos2_alpha * (A ** 2 - B ** 2) / (B ** 2)
    k1 = (math.sqrt(1 + u_sq) - 1) / (math.sqrt(1 + u_sq) + 1)
    aa = (1 + k1 ** 2 / 4) / (1 - k1)
    bb = k1 * (1 - 3 * k1 ** 2 / 8)

    sigma = distance_m_ / (B * aa)
    cos_2sigma_m = sin_sigma = cos_sigma = 0.0
    for _ in range(_MAX_ITER):
        cos_2sigma_m = math.cos(2 * sigma1 + sigma)
        sin_sigma, cos_sigma = math.sin(sigma), math.cos(sigma)
        d_sigma = bb * sin_sigma * (
            cos_2sigma_m + bb / 4 * (
                cos_sigma * (-1 + 2 * cos_2sigma_m ** 2)
                - bb / 6 * cos_2sigma_m * (-3 + 4 * sin_sigma ** 2)
                * (-3 + 4 * cos_2sigma_m ** 2)))
        prev = sigma
        sigma = distance_m_ / (B * aa) + d_sigma
        if abs(sigma - prev) < _TOL:
            break

    tmp = sin_u1 * sin_sigma - cos_u1 * cos_sigma * cos_a1
    phi2 = math.atan2(sin_u1 * cos_sigma + cos_u1 * sin_sigma * cos_a1,
                      (1 - F) * math.hypot(sin_alpha, tmp))
    lam = math.atan2(sin_sigma * sin_a1,
                     cos_u1 * cos_sigma - sin_u1 * sin_sigma * cos_a1)
    c = F / 16 * cos2_alpha * (4 + F * (4 - 3 * cos2_alpha))
    ll = lam - (1 - c) * F * sin_alpha * (
        sigma + c * sin_sigma * (
            cos_2sigma_m + c * cos_sigma * (-1 + 2 * cos_2sigma_m ** 2)))
    return math.degrees(phi2), wrap180(lon + math.degrees(ll))


def offset_ned(lat: float, lon: float, north_m: float, east_m: float
               ) -> tuple[float, float]:
    """`lat`/`lon` moved by a local north/east displacement in meters.

    This is the one conversion that turns the EKF's local frame into something
    a map can draw, and getting its axes wrong is invisible: the track still
    looks like a track, it is simply rotated ninety degrees. North is +x and
    east is +y in ArduPilot's NED, and that is the order of the arguments.

    Zero displacement returns the origin unchanged rather than going through
    the direct solution, so a stationary vehicle's position does not wander by
    floating-point noise.
    """
    if north_m == 0.0 and east_m == 0.0:
        return lat, lon
    bearing = wrap360(math.degrees(math.atan2(east_m, north_m)))
    return destination(lat, lon, bearing, math.hypot(north_m, east_m))


def wrap360(deg: float) -> float:
    """An angle into [0, 360)."""
    return deg % 360.0


def wrap180(deg: float) -> float:
    """An angle into [-180, 180). Longitudes, and differences of headings."""
    return (deg + 180.0) % 360.0 - 180.0


def angle_diff(a_deg: float, b_deg: float) -> float:
    """`a - b` as the shortest turn, in [-180, 180).

    The reason this exists rather than a subtraction: a vehicle on 001 deg and
    a target on 359 deg are two degrees apart, and the naive difference says
    they are 358. Every relative-bearing readout on the page goes through here.
    """
    return wrap180(a_deg - b_deg)


def meters_per_degree(lat: float) -> tuple[float, float]:
    """(meters per degree latitude, per degree longitude) at this latitude.

    For the map's scale bar and for the pixels-per-meter a canvas needs. Not
    used for positions -- those go through the geodesic functions -- because
    this linearisation is only good over a few kilometers.
    """
    phi = math.radians(lat)
    m_lat = (111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
             - 0.0023 * math.cos(6 * phi))
    m_lon = (111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)
             + 0.118 * math.cos(5 * phi))
    return m_lat, m_lon


# --------------------------------------------------------------------------
#  Web Mercator, for tiles
# --------------------------------------------------------------------------
#
# The basemap is XYZ tiles, which are Web Mercator (EPSG:3857). Positions are
# WGS-84 geodetic. These two conversions are the only place the projection
# appears; everything else in the program works in latitude and longitude.

#: Web Mercator is undefined at the poles and clipped in every tile scheme.
MERCATOR_MAX_LAT = 85.05112878


def latlon_to_tile_xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Fractional tile coordinates. The integer part is the tile, the
    fraction is where inside it the point falls."""
    lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, lat))
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    phi = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(phi)) / math.pi) / 2.0 * n
    return x, y


def tile_xy_to_latlon(x: float, y: float, zoom: int) -> tuple[float, float]:
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def zoom_for_span(lat: float, span_m: float, pixels: int,
                  tile_px: int = 256) -> int:
    """The integer zoom whose tiles show `span_m` meters across `pixels`.

    Clamped to the range the chart and street services actually publish.
    """
    if span_m <= 0 or pixels <= 0:
        return 16
    _, m_per_deg_lon = meters_per_degree(lat)
    if m_per_deg_lon <= 0:
        return 16
    deg = span_m / m_per_deg_lon
    # World is 360 degrees across 2**z * tile_px pixels.
    want = math.log2(max(1e-9, 360.0 * pixels / (deg * tile_px)))
    return max(0, min(19, int(math.floor(want))))


def format_bearing(deg: float, digits: int = 0) -> str:
    """A bearing as the operator reads it: `275°T`, and never `360°T`.

    Rounding happens before the wrap, not after. A bearing a hair under a full
    turn -- which the inverse solution genuinely produces for a due-north pair
    -- formats as 360 if it is wrapped first and rounded second, and 360 is not
    a bearing anybody writes down.
    """
    return f"{round(deg, digits) % 360:.{digits}f}°T"
