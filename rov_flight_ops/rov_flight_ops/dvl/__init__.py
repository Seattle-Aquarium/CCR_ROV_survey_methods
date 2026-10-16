"""
The Water Linked DVL A50, captured whole.

Everything the DVL will give a topside laptop, logged as it arrives and kept
apart from the vehicle's own recordings, so that a message missing from an
mcap can be looked for in what the DVL actually said.

The pieces, in the order the data passes through them:

``protocol``    the DVL's own formats: framing the TCP stream without changing
                a byte, parsing it, flattening it into CSV rows, the beam
                layout, and the short list of commands this program may send
``websocket``   a small RFC 6455 client for the stream the DVL's web GUI reads
``webapi``      a keep-alive HTTP GET client for the DVL's web API
``cadence``     report-to-report intervals, and what a long one means
``live``        the latest values and short histories, for the DVL tab
``capture``     one capture session: its threads, its files, its record
``recorder``    the application's handle on captures: start, move, stop
``diagnostic``  Water Linked's support log, collected only on request
``simulator``   a stand-in DVL for the tests and the bench

The design is written down in ``specifications/`` beside this program, which
is where the file formats, the invariants and the reasons are.
"""
