"""The placed symbols of a project, with the reference and the unit of each sheet path.

KiCad keeps the reference and the unit of a placed symbol for each sheet path,
in (instances (project ... (path "<sheet path>" (reference ...) (unit ...)))).
A sheet that the hierarchy uses more than one time gives each of its symbols
one reference for each sheet path. With no instance for the sheet path, KiCad
uses the Reference field and the (unit) of the symbol (SCH_SYMBOL::GetRef).
parsers/unit_fields and tools/symbol_fields use these rules.
"""

from dataclasses import dataclass
from pathlib import Path

from ..utils.kicad_lock import FileStamp, file_stamp
from ..utils.kicad_text import read_as_kicad
from ..utils.sch_hierarchy import SheetPath, sheet_paths
from .kicad_fields import rules_for_file
from .kicad_versions import schematic_version
from .lib_symbols import unit_count
from .sch_geometry import definition_key, definitions, placed_symbols, symbol_unit
from .sexpr import Node, parse, property_field


@dataclass
class SchematicFile:
    """A schematic file of the project, as the tool read it."""

    root: Node
    stamp: FileStamp  # before the read (kicad_lock.file_stamp)
    version: int
    kicad: int  # the field rules: 9 (KiCad 8 and 9) or 10
    library: dict[str, Node]  # the definitions of lib_symbols


@dataclass
class PlacedInstance:
    """One placed symbol in one sheet path."""

    file: Path
    symbol: Node
    path: Node | None  # (path ...) of the instance; None: KiCad uses the Reference field
    reference: str
    unit: int
    unit_count: int

    @property
    def key(self) -> tuple[Path, int]:
        """The placed symbol: its file and its offset (one symbol, more instances)."""
        return (self.file, self.symbol.start)


def placed_instances(
    project: Path,
) -> tuple[list[PlacedInstance], dict[Path, SchematicFile], list[str]]:
    """Return the placed symbols of each sheet path of a project, the files, and the
    problems of the sheet hierarchy (sch_hierarchy.sheet_paths). A sheet with no
    UUID gets new UUIDs at each load of KiCad: no instance matches it, thus its
    symbols are not in the list (and sheet_paths gives a problem).

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
        KicadFileError: A file has a version that the server does not know.
        ValueError: A unit is not a number.
    """
    paths, problems = sheet_paths(project)
    files: dict[Path, SchematicFile] = {}
    instances: list[PlacedInstance] = []
    for sheet in paths:
        if sheet.uuids is None:
            continue
        if sheet.file not in files:
            stamp = file_stamp(sheet.file)
            root = parse(read_as_kicad(sheet.file))
            files[sheet.file] = SchematicFile(
                root, stamp, schematic_version(root), rules_for_file(root).kicad, definitions(root)
            )
        record = files[sheet.file]
        for symbol in placed_symbols(record.root):
            found = _instance(sheet, symbol)
            if found is None:
                continue
            path, reference, unit = found
            key = definition_key(symbol)
            count = unit_count(record.library[key]) if key in record.library else 1
            instances.append(PlacedInstance(sheet.file, symbol, path, reference, unit, count))
    return instances, files, problems


def _instance(sheet: SheetPath, symbol: Node) -> tuple[Node | None, str, int] | None:
    """Return (the instance path node, the reference, the unit) of a symbol in a
    sheet path, as SCH_SYMBOL::GetRef. None for a symbol with no reference."""
    instances = symbol.first("instances")
    path = next(
        (p for p in (instances.walk("path") if instances else []) if p.value(0) == sheet.path),
        None,
    )
    node = path.first("reference") if path is not None else None
    reference = node.value(0, "") if node is not None else ""
    if not reference:
        reference = next(
            (
                f[1]
                for p in symbol.children("property")
                if (f := property_field(p)) and f[0] == "Reference"
            ),
            "",
        )
    if not reference:
        return None
    unit = path.first("unit") if path is not None else None
    return path, reference, int(unit.number(0, 1)) if unit is not None else symbol_unit(symbol)
