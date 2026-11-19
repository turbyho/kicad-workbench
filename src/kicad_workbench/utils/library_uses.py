"""The uses of a library item in a project: the places that keep the old library ID
when a symbol or a footprint of a library gets an other name, or is deleted.

KiCad 10.0.6 does not change them (SYMBOL_EDITOR_CONTROL::RenameSymbol,
FOOTPRINT_EDITOR_CONTROL::RenameFootprint, DeleteSymbolFromLibrary,
DeleteFootprintFromLibrary change only the library). The uses:

- Symbols: the lib_id of each placed symbol of the schematic (the hierarchy as the
  schematic editor loads it, placed_instances).
- Footprints: the Footprint field of each placed symbol and of its variants (KiCad 10),
  the library ID of each footprint of the board of the project, and the Footprint field
  of each symbol of the symbol libraries of the project table.

A mandatory field has its name in any case (KiCad 10 reads a mandatory field with no
difference of case).
"""

from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path

from ..errors import WorkbenchError
from ..parsers.board_models import board_models
from ..parsers.lib_symbols import read_library
from ..parsers.sch_instances import PlacedInstance, placed_instances
from ..parsers.sexpr import Node, property_atoms
from ..parsers.sheet_list import str_num_cmp
from .kicad_project import top_level_sheets
from .kicad_text import KiCadEncodingError, read_as_kicad


@dataclass
class Uses:
    """The uses of the library IDs in a project, as lines, and the parts that the
    tool could not read."""

    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)  # the library IDs with a use


def _footprint_field(symbol: Node) -> str | None:
    for prop in symbol.children("property"):
        atoms = property_atoms(prop)
        if len(atoms) > 1 and atoms[0].value.lower() == "footprint":
            return atoms[1].value
    return None


def _variant_footprints(path: Node | None) -> list[tuple[str, str]]:
    """(variant name, Footprint text) of the variants of an instance that set it."""
    found = []
    for variant in path.children("variant") if path is not None else []:
        name = variant.first("name")
        if name is None or not name.value(0, ""):
            continue  # as unit_fields: a variant has a name
        for item in variant.children("field"):
            field_name, value = item.first("name"), item.first("value")
            if field_name is None or value is None:
                continue
            if field_name.value(0, "").lower() == "footprint":
                found.append((name.value(0, ""), value.value(0, "")))
    return found


def _items(kind: str, names: list[str], verb: str) -> str:
    """The text 'the symbol U4 has' or 'the symbols U1, U2 have', with the names in the
    order of StrNumCmp."""
    ordered = sorted(names, key=cmp_to_key(str_num_cmp))
    if len(ordered) == 1:
        return f"the {kind} {ordered[0]} {verb}"
    plural = {"has": "have", "is": "are"}.get(verb, verb)
    return f"the {kind}s {', '.join(ordered)} {plural}"


def _add(found: dict[str, list[str]], key: str, item: str) -> None:
    items = found.setdefault(key, [])
    if item not in items:
        items.append(item)


def _has_schematic(project: Path) -> bool:
    default = project.with_suffix(".kicad_sch")
    return default.is_file() or any(sheet.file.is_file() for sheet in top_level_sheets(project))


def _placed(project: Path, kicad: str, uses: Uses) -> list[PlacedInstance]:
    if not _has_schematic(project):
        return []
    try:
        placed, _files, problems = placed_instances(project, kicad, "gui")
    except (WorkbenchError, OSError, KiCadEncodingError) as e:
        uses.problems.append(f"The tool cannot read the schematic for the uses: {e}")
        return []
    # The problems of the hierarchy (sheet_list): the references of the list can then
    # differ from the references of KiCad.
    uses.problems += [f"The schematic has a problem: {problem}" for problem in problems]
    return placed


def symbol_uses(project: Path, kicad: str, lib_ids: list[str]) -> Uses:
    """The placed symbols whose lib_id is one of lib_ids."""
    uses = Uses()
    found: dict[str, list[str]] = {}
    for placed in _placed(project, kicad, uses):
        lib_id = placed.symbol.first("lib_id")
        text = lib_id.value(0, "") if lib_id is not None else ""
        if text in lib_ids:
            _add(found, text, placed.reference)
    for lib_id in lib_ids:
        if lib_id in found:
            uses.used.append(lib_id)
            uses.lines.append(
                f"Schematic: {_items('symbol', found[lib_id], 'has')} the lib_id {lib_id}."
            )
    return uses


def footprint_uses(
    project: Path, kicad: str, lib_id: str, symbol_libraries: list[tuple[str, Path]]
) -> Uses:
    """The places that have the footprint lib_id.

    Args:
        symbol_libraries: (nickname, file) of each symbol library file of the project
            table.
    """
    uses = Uses()
    fields: list[str] = []
    variants: dict[str, list[str]] = {}
    for placed in _placed(project, kicad, uses):
        if _footprint_field(placed.symbol) == lib_id and placed.reference not in fields:
            fields.append(placed.reference)
        for variant, text in _variant_footprints(placed.variants):
            if text == lib_id:
                _add(variants, variant, placed.reference)
    if fields:
        uses.lines.append(
            f"Schematic: {_items('symbol', fields, 'has')} the Footprint field {lib_id}."
        )
    for variant, references in variants.items():
        uses.lines.append(
            f"Schematic, variant {variant!r}: {_items('symbol', references, 'has')} the "
            f"Footprint field {lib_id}."
        )
    board = project.with_suffix(".kicad_pcb")
    if board.is_file():
        try:
            footprints, _files = board_models(read_as_kicad(board))
        except (WorkbenchError, OSError, KiCadEncodingError) as e:
            uses.problems.append(f"The tool cannot read the board {board} for the uses: {e}")
        else:
            references = []
            for footprint in footprints:
                if footprint.lib_id == lib_id and footprint.reference not in references:
                    references.append(footprint.reference)
            if references:
                uses.lines.append(
                    f"Board: {_items('footprint', references, 'has')} the library ID {lib_id}."
                )
    for nickname, path in symbol_libraries:
        try:
            symbols = read_library(path)
        except (WorkbenchError, OSError, KiCadEncodingError) as e:
            uses.problems.append(
                f"The tool cannot read the symbol library '{nickname}' for the uses: {e}"
            )
            continue
        names = [
            name for name, symbol in symbols.items() if _footprint_field(symbol.node) == lib_id
        ]
        if names:
            uses.lines.append(
                f"Symbol library '{nickname}': {_items('symbol', names, 'has')} the Footprint "
                f"field {lib_id}."
            )
    if uses.lines:
        uses.used.append(lib_id)
    return uses
