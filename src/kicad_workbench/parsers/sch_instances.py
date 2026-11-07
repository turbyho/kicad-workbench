"""The placed symbols of a project, with the reference and the unit of each sheet path.

KiCad keeps the reference and the unit of a placed symbol for each sheet path,
in (instances (project ... (path "<sheet path>" (reference ...) (unit ...)))).
A sheet that the hierarchy uses more than one time gives each of its symbols
one reference for each sheet path. The rules of KiCad 8, 9 and 10 for the read
of the instances and for the changes after the load are in
parsers/annotation.py (placed_symbols_after_load):

- The last (path ...) of a sheet path wins (SCH_SYMBOL::AddHierarchicalReference),
  and the first instance sets the Reference field and the unit of the symbol.
- KiCad 8 and 9 add no instance for a (path ...) with no items.
- A symbol with no instance for a sheet path: the schematic editor of KiCad 8 and
  9 gives it "U?" (not annotated) and unit 1, KiCad 10 a copy of an instance of
  the project (CheckForMissingSymbolInstances). kicad-cli 8 and 9 use the
  Reference field (SCH_SYMBOL::GetRef).

parsers/unit_fields and tools/symbol_fields use these rules.
"""

from pathlib import Path

from .annotation import PlacedSymbol, placed_symbols_after_load
from .sheet_list import Screen

# The names of the callers: a placed symbol in one sheet path, and a schematic file.
PlacedInstance = PlacedSymbol
SchematicFile = Screen


def placed_instances(
    project: Path, version: str, view: str
) -> tuple[list[PlacedInstance], dict[str, SchematicFile], list[str]]:
    """Return the placed symbols of each sheet path of a project, the files (by the
    text of the file name that KiCad gives each screen: Screen.key), and the problems
    of the sheet hierarchy (placed_symbols_after_load).

    Args:
        project: The .kicad_pro file (it can be missing).
        version: The version of the installed KiCad (kicad-cli version).
        view: "gui": the schematic editor after the project opens. "cli": the jobs
            of kicad-cli (for example the BOM).

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
        KicadFileError: A file has a version that the server does not know.
        KicadParseError: The parser of KiCad stops in a sub-sheet (build_sheet_list).
        InputError: A unit is not a number, KiCad has no verified rules for the
            version, or KiCad cannot load the root schematic.
    """
    return placed_symbols_after_load(project, version, view)
