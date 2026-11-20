"""A global symbol library table for tests that add symbols to a schematic.

add_component_from_library finds a library in the library tables of the
installed KiCad. These tests use a KiCad 10.0 configuration in a temporary
directory, thus they run also without KiCad.
"""

import os
import re
from pathlib import Path

from kicad_workbench.parsers.sexpr import quote
from kicad_workbench.tools import schematic_editor
from kicad_workbench.utils import library_tables

from .kicad_stub import TEST_KICAD


def table(*rows: str) -> str:
    """The text of a sym-lib-table with the rows."""
    return "(sym_lib_table\n\t(version 7)\n" + "".join(f"\t{row}\n" for row in rows) + ")\n"


def row(name: str, uri: Path | str, flags: str = "", kind: str = "KiCad", descr: str = "") -> str:
    """One (lib ...) row of a library table, as KiCad writes it: LIBRARY_TABLE::Format
    of KiCad 10 writes each backslash of the URI as "/", because KiCad 10.0 reads a
    quoted text with no escapes. The tests make each table row with this function."""
    uri = str(uri).replace("\\", "/")
    return (
        f"(lib (name {quote(name)})(type {quote(kind)})(uri {quote(uri)})"
        f'(options "")(descr {quote(descr)}){flags})'
    )


def use_global_table(tmp_path: Path, monkeypatch, *rows: str, kicad: str = TEST_KICAD) -> Path:
    """Make a global symbol library table of the installed KiCad with the rows.

    Args:
        kicad: The version of the installed KiCad (kicad_cli_version_sync of
            add_component_from_library). Give the version of the files of the test:
            with KiCad 10.0 the tool saves the library symbols first (library_saved),
            with KiCad 8 and 9 it uses the text of the file.

    Returns:
        The file of the global table.
    """
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD", name):
            monkeypatch.delenv(name)
    config = tmp_path / "kicad-config"
    major, minor = kicad.split(".")[:2]
    (config / f"{major}.{minor}").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "kicad-documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: tmp_path / "kicad-stock")

    def kicad_version():
        return kicad

    monkeypatch.setattr(schematic_editor, "kicad_cli_version_sync", kicad_version)
    path = config / f"{major}.{minor}" / "sym-lib-table"
    path.write_text(table(*rows), encoding="utf-8")
    return path
