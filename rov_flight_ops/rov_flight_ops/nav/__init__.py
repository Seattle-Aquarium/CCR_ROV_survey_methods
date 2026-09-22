"""
Navigation: live mapping, flight instruments, power and the navigation suite.

This package is everything the Navigation chapter knows that is *not* a widget.
The split is the same one the rest of the program uses -- `flightlog.py` records
a flight and `monitorpage.py` draws it -- and for the same reason: the parts
that talk to the vehicle have to be testable without a screen, and the parts
that draw have to be replaceable without touching what they draw.

===================  =========================================================
`model`              the typed reading: value, unit, frame, quality, age. The
                     one rule the whole package is built on -- **unknown is
                     not zero** -- lives here.
`mav2rest`           the versioned adapter over BlueOS's mavlink2rest, and the
                     freshness rule that a successful HTTP GET proves nothing.
`extensions`         the DVL, both Water Linked UGPS extensions, and the
                     service discovery that finds their ports.
`collector`          one shared background reader. Every consumer on the page
                     reads its snapshot; nothing else polls the vehicle.
`geo`                WGS-84 distance, initial bearing and local-NED projection,
                     without adding a dependency.
`power`              watts, the observed peak, and Wh integrated over the real
                     sample intervals.
`profiles`           the two navigation profiles, what each requires, and what
                     the vehicle actually has.
`origin`             which mechanism owns the EKF origin on this vehicle, and
                     the ordering that keeps a Lua applet from reading half a
                     coordinate.
`session`            the navigation-session log: a manifest and an append-only
                     event stream.
`waypoints`          points captured at the instant the button is pressed.
`tiles`              the basemap cache, kept outside the repository.
`replay`             a recorded session or an existing flight, played back.
                     Never sends anything to a vehicle.
===================  =========================================================

**Nothing in this package writes to the vehicle except `profiles.apply` and
`origin.apply`**, both of which are reached only through an operator's explicit
review-and-confirm, and neither of which exists at all in replay.
"""

from __future__ import annotations

#: Bumped when the on-disk navigation session schema changes shape.
SCHEMA = "ccr.nav/1"
