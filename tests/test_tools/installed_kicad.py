"""Test schematics of a file version, and the version of the installed KiCad.

add_component_from_library puts a symbol into a schematic only when KiCad reads
it there as in the library (kicad_versions.symbol_library_problem). Thus a test
that adds a symbol of the installed libraries uses a schematic of their release.
"""

import functools
import subprocess
import uuid as uuidlib
from pathlib import Path

from kicad_workbench.parsers.kicad_versions import (
    KICAD10,
    NEWEST_SCHEMATIC,
    header_version,
    release_of_symbol_library,
)
from kicad_workbench.parsers.lib_symbols import library_files
from kicad_workbench.utils.kicad_cli import decode_output, run_kicad_cli_sync
from kicad_workbench.utils.kicad_text import read_as_kicad
from kicad_workbench.utils.library_index import row_problem
from kicad_workbench.utils.library_tables import LibraryTableError, find_library

SCHEMATIC = """(kicad_sch
\t(version {version})
\t(generator "eeschema")
\t(uuid "{uuid}")
\t(paper "A4")
\t(lib_symbols)
)
"""


def write_schematic(directory: Path, version: int) -> Path:
    """Write an empty schematic "board.kicad_sch" of a file version."""
    path = directory / "board.kicad_sch"
    path.write_text(SCHEMATIC.format(version=version, uuid=uuidlib.uuid4()), encoding="utf-8")
    return path


# The header of the test schematics.
TEST_HEADER = '(version 20250114)\n\t(generator "eeschema")\n\t(generator_version "9.0")'


@functools.cache
def installed_device_library() -> Path | None:
    """Return the library "Device" of the global library table of the installed KiCad.

    None when kicad-cli is not found, or the global table has no library
    "Device" that KiCad can load. add_component_from_library uses this library.
    """
    # The synchronous call also works in a test that runs in an event loop.
    try:
        result = run_kicad_cli_sync(["version"], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    version = decode_output(result.stdout).strip()
    if result.returncode != 0 or not version:
        return None
    try:
        row, _problems = find_library("symbol", "Device", version)
    except LibraryTableError:
        return None
    if row is None or row_problem(row) is not None:
        return None
    if not library_files(row.path):  # An empty directory library.
        return None
    return row.path


@functools.cache
def installed_schematic_version() -> int | None:
    """Return the schematic version of the release of the installed libraries, or None.

    None when the library Device of the installed KiCad is not found.
    """
    library = installed_device_library()
    if library is None:
        return None
    library = library_files(library)[0]
    version = header_version(read_as_kicad(library), "kicad_symbol_lib")
    if version is None:
        raise AssertionError(f"{library} has no file version")
    if version > KICAD10.symbol_library:
        # A nightly build: its schematics are newer than the release.
        return NEWEST_SCHEMATIC
    return release_of_symbol_library(version).schematic


def _installed_header() -> str:
    version = installed_schematic_version()
    if version is None:
        return TEST_HEADER
    return f'(version {version})\n\t(generator "eeschema")'


def as_installed(schematic: str) -> str:
    """Give a test schematic the version of the release of the installed libraries."""
    assert TEST_HEADER in schematic
    return schematic.replace(TEST_HEADER, _installed_header(), 1)
