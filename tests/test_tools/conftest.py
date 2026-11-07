"""Fixtures of the tests of the tools.

long64 and long32 set the size of a C long, thus a test gives the same result on
each system: a C long has 64 bits on Linux and macOS and 32 bits on Windows
(parsers/c_numbers.py).

The tools convert the schematic files of an older version with the installed
KiCad before they work (utils/schematic_upgrade.py). The tests of a tool test
its rules on the files as they are, and the conversion must not change the
fixtures of the repository: thus each test gets no conversion, except a test with
the marker schematic_conversion (the tests of the conversion use copies in
tmp_path; spike/kicad-versions/upgrade_check.py measures it with KiCad).
"""

import pytest

from kicad_workbench.parsers import c_numbers
from kicad_workbench.parsers.sub_symbols import sub_symbol_unit
from kicad_workbench.utils import schematic_upgrade


@pytest.fixture(autouse=True)
def _no_schematic_conversion(request, monkeypatch):
    if request.node.get_closest_marker("schematic_conversion") is None:
        monkeypatch.setattr(
            schematic_upgrade,
            "current_schematics",
            lambda path: schematic_upgrade.CurrentSchematics("", []),
        )


def _long(monkeypatch, bits: int):
    monkeypatch.setattr(c_numbers, "LONG_MIN", -(2 ** (bits - 1)))
    monkeypatch.setattr(c_numbers, "LONG_MAX", 2 ** (bits - 1) - 1)
    # The cache of sub_symbol_unit holds the results of the long of the system.
    sub_symbol_unit.cache_clear()
    yield
    sub_symbol_unit.cache_clear()


@pytest.fixture
def long64(monkeypatch):
    """The long of Linux and macOS (LP64)."""
    yield from _long(monkeypatch, 64)


@pytest.fixture
def long32(monkeypatch):
    """The long of Windows (LLP64, the MSVC build of KiCad)."""
    yield from _long(monkeypatch, 32)
