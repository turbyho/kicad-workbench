"""Fixtures of the tests of the tools.

long64 and long32 set the size of a C long, thus a test gives the same result on
each system: a C long has 64 bits on Linux and macOS and 32 bits on Windows
(parsers/c_numbers.py).

Before each tool, the server compares the schematic files, the board and the
libraries of the project with the version that the installed KiCad writes
(utils/file_versions.py): an older file
gives a warning, or an error for a tool that writes. The tests of a tool test its rules
on the fixtures as they are, files of KiCad 8, 9 and 10: thus each test gets no check,
except a test with the marker file_versions (the tests of the check;
spike/kicad-versions/schematic_versions_check.py compares it with KiCad).
"""

import pytest

from kicad_workbench.parsers import c_numbers
from kicad_workbench.parsers.sub_symbols import sub_symbol_unit
from kicad_workbench.utils import file_versions


@pytest.fixture(autouse=True)
def _no_file_version_check(request, monkeypatch):
    if request.node.get_closest_marker("file_versions") is None:
        for check in ("check_schematic_versions", "check_board_file_version"):
            monkeypatch.setattr(
                file_versions, check, lambda path, write: file_versions.FileVersions("", "")
            )
        monkeypatch.setattr(file_versions, "library_versions", lambda kind, libraries, kicad: [])
        monkeypatch.setattr(
            file_versions,
            "check_library_versions",
            lambda kind, libraries, kicad: file_versions.LibraryCheck([], [], set()),
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
