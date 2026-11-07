"""The clock of the library caches, for the tests.

A cache trusts the stamp of a file only when the file is older than the time
resolution of the file system (lib_symbols.files_stamp). A new file of a test
is not older. A test that must see the cache with a stamp only moves the clock
of lib_symbols forward. A change of the time of the file cannot do this,
because os.utime() sets the change time of the file to the current time.
"""

import time

from kicad_workbench.parsers import lib_symbols

# More than the 2 s of lib_symbols._RACY_NS.
LATER_NS = 10_000_000_000


def move_clock_forward(monkeypatch) -> None:
    """Make lib_symbols see each file as older than LATER_NS."""
    monkeypatch.setattr(lib_symbols, "_now_ns", lambda: time.time_ns() + LATER_NS)
