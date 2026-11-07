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


def table(*rows: str) -> str:
    """The text of a sym-lib-table with the rows."""
    return "(sym_lib_table\n\t(version 7)\n" + "".join(f"\t{row}\n" for row in rows) + ")\n"


def row(name: str, uri: Path | str, flags: str = "", kind: str = "KiCad") -> str:
    """One (lib ...) row of a library table."""
    return (
        f"(lib (name {quote(name)})(type {quote(kind)})(uri {quote(str(uri))})"
        f'(options "")(descr ""){flags})'
    )


def use_global_table(tmp_path: Path, monkeypatch, *rows: str) -> Path:
    """Make a global symbol library table of KiCad 10.0.6 with the rows.

    Returns:
        The file of the global table.
    """
    for name in list(os.environ):
        if re.fullmatch(r"KICAD\d*_.*|KIPRJMOD", name):
            monkeypatch.delenv(name)
    config = tmp_path / "kicad-config"
    (config / "10.0").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(config))
    monkeypatch.setenv("KICAD_DOCUMENTS_HOME", str(tmp_path / "kicad-documents"))
    monkeypatch.setattr(library_tables, "_stock_dir", lambda: tmp_path / "kicad-stock")

    async def kicad_version():
        return "10.0.6"

    monkeypatch.setattr(schematic_editor, "get_kicad_version", kicad_version)
    path = config / "10.0" / "sym-lib-table"
    path.write_text(table(*rows), encoding="utf-8")
    return path
