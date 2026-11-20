"""The tool update_symbols_from_library: "Update Symbols from Library" of the schematic
editor of KiCad 10.0.6 (DIALOG_CHANGE_SYMBOLS in the mode UPDATE; plans/roadmap.md,
"Step 10: plan").

- isMatch: all symbols, the reference or the value (WildCompareString with no case), or
  the library ID. processSymbols changes the symbol, thus each instance of a shared
  sheet: the tool refuses a match that does not have each instance (user decisions of
  the steps 1, 8 and 9).
- The library symbol comes from the library tables, flattened (lib_symbols.flatten).
  SCH_SYMBOL::SetLibSymbol and UpdatePins give the new shape, pins, keywords and
  footprint filters; the fields, the attributes and the alternate pins follow the
  options of the dialog.
- The copy of the library symbol in (lib_symbols ...) (SCH_SCREEN::Remove and Append):
  the updated symbols use "<nickname>:<name>" again. When a symbol that the match does
  not have keeps an other copy with that name, KiCad makes a copy "<name>_<n>": the tool
  refuses this (user decision, 2026-11-19).
- "Update/reset field positions" of a symbol with fields_autoplaced runs AutoplaceFields,
  which needs the text sizes of the stroke font: the tool refuses this (user decision,
  2026-11-19).
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..errors import InputError
from ..parsers.annotation import kicad_major, split_reference
from ..parsers.bus_names import unescape_string
from ..parsers.kicad_fields import (
    SIM_LEGACY_FIELDS,
    SYMBOL_FIELD_NAMES,
    WX_SPACE,
    rules_for_version,
)
from ..parsers.kicad_versions import KicadFileError
from ..parsers.lib_symbols import (
    flatten,
    mandatory_name,
    pin_number,
    placed_fields,
    unit_count,
)
from ..parsers.sch_geometry import (
    body_style,
    definition_key,
    placement,
    symbol_transform,
    symbol_unit,
    transform_coordinate,
)
from ..parsers.sch_instances import PlacedInstance, SchematicFile, placed_instances
from ..parsers.sexpr import (
    Node,
    SExprError,
    edited,
    indent,
    parse,
    parse_bool,
    parse_maybe_absent_bool,
    quote,
    removal,
)
from ..parsers.sub_symbols import sub_symbols
from ..parsers.symbol_info import letter_reference
from ..server import mcp
from ..utils import file_versions
from ..utils.file_versions import (
    record_library_warnings,
    with_library_versions,
    with_schematic_versions,
)
from ..utils.footprint_filter import parse_lib_id
from ..utils.kicad_cli import get_kicad_version, kicad_10_problem
from ..utils.kicad_lock import write_schematics
from ..utils.kicad_text import KiCadEncodingError
from ..utils.library_copy import escape_lib_id
from ..utils.library_lookup import LibraryLookupError, find_library_symbol
from ..utils.library_saved import SavedLibraryError, SymbolLibraryError, schematic_chain
from ..utils.sch_hierarchy import AmbiguousRootError, schematic_project_file
from ..utils.symbol_update import (
    FieldState,
    TextAttributes,
    field_text,
    read_field,
    reset_reference,
    wild_compare,
)
from ..utils.tool_errors import error_answer
from .library import cell
from .symbol_fields import (
    read_variants,
    screens_of_files,
    symbol_tokens,
    variant_edit,
)

# The flags of a library symbol and their values with no token (the LIB_SYMBOL
# constructor): the attributes of "Update/reset symbol attributes".
_LIBRARY_FLAGS = {"exclude_from_sim": False, "in_bom": True, "on_board": True, "in_pos_files": True}
# The mandatory fields that KiCad shows when the library symbol has no such field.
_SHOWN = ("Reference", "Value")


class UpdateOptions(BaseModel):
    """The "Update Options" of the dialog, with its defaults in the mode UPDATE."""

    model_config = ConfigDict(extra="forbid")

    remove_extra_fields: bool = Field(
        default=False, description='"Remove fields if not in library symbol".'
    )
    reset_empty_fields: bool = Field(
        default=False, description='"Reset fields if empty in library symbol".'
    )
    update_field_text: bool = Field(default=True, description='"Update/reset field text".')
    update_field_visibilities: bool = Field(
        default=False, description='"Update/reset field visibilities".'
    )
    update_field_styles: bool = Field(
        default=False, description='"Update/reset field text sizes and styles".'
    )
    update_field_positions: bool = Field(
        default=False, description='"Update/reset field positions".'
    )
    update_attributes: bool = Field(default=False, description='"Update/reset symbol attributes".')
    update_pin_text_visibility: bool = Field(
        default=False, description='"Update/reset pin name/number visibilities".'
    )
    reset_alternate_pins: bool = Field(
        default=False, description='"Reset alternate pin functions".'
    )
    reset_custom_power: bool = Field(default=False, description='"Reset custom power symbols".')


@dataclass(frozen=True)
class _LibPin:
    number: str
    name: str
    alternates: dict[str, tuple[str, str]]  # name: (electrical type, shape)
    unit: int = 1  # the unit of the pin (0: the pin of each unit)


@dataclass(eq=False)
class _Pin:
    """A pin of a placed symbol: (pin "N" (uuid ...) (alternate ...)). Two pins are equal
    only when they are the same object (KiCad keeps the pins in sets of pointers)."""

    number: str
    uuid: str
    alternate: str
    alternates: dict[str, tuple[str, str]] = field(default_factory=dict)
    name: str = ""  # the name of its library pin (GetBaseName)
    unit: int = 0  # the unit of its library pin (UpdatePins: SetUnit)


@dataclass
class _Definition:
    """The flattened library symbol of one library ID, for each symbol that uses it."""

    node: Node
    text: str  # the text for (lib_symbols ...), indented for its place
    normalized: str  # the text with no indent, to compare with a copy
    # The fields, and the user field names in their order (_library_fields).
    library: tuple[dict[str, FieldState], list[str]]
    units: int  # unit_count
    pins: dict[int, list["_LibPin"]] = field(default_factory=dict)  # by body style

    def pins_of(self, style: int) -> list["_LibPin"]:
        if style not in self.pins:
            self.pins[style] = _library_pins(self.node, style)
        return self.pins[style]


@dataclass
class _Symbol:
    """The update of one placed symbol (its file and its node)."""

    instances: list[PlacedInstance]
    lib_id: str
    definition: _Definition | None = None
    states: list[FieldState] | None = None  # the fields of the file (_placed_states)


def _library_pins(definition: Node, style: int) -> list[_LibPin]:
    """The pins of the library symbol that UpdatePins gives a placed symbol: each pin of
    each unit with the body style 0 or the body style of the symbol."""
    groups = [(1, 1, definition), *sub_symbols(definition)]
    pins = []
    for unit, pin_style, group in groups:
        if pin_style and style and pin_style != style:
            continue
        for pin in group.children("pin"):
            number = pin.first("number")
            name = pin.first("name")
            alternates = {
                alt.value(0, ""): (alt.value(1, ""), alt.value(2, ""))
                for alt in pin.children("alternate")
            }
            pins.append(
                _LibPin(
                    pin_number(number.value(0, "")) if number else "",
                    name.value(0, "") if name else "",
                    alternates,
                    unit,
                )
            )
    return pins


def _file_pins(symbol: Node) -> list[_Pin]:
    pins = []
    for node in symbol.children("pin"):
        uid = node.first("uuid")
        alt = node.first("alternate")
        pins.append(
            _Pin(
                node.value(0, ""),
                uid.value(0, "") if uid else str(uuid.uuid4()),
                alt.value(0, "") if alt else "",
            )
        )
    return pins


def _update_pins(pins: list[_Pin], new: list[_LibPin]) -> list[_Pin]:
    """SCH_SYMBOL::UpdatePins with a library symbol: at the load with the copy of the
    file (SetLibSymbol of UpdateLocalLibSymbolLinks), and again with the new library
    symbol. The pins are the same objects: a pin that the result does not have is
    removed, a pin that the input does not have is new.

    KiCad keeps the pins in sets of pointers: the order in which a library pin with no
    pin of its number takes an unused pin of the file, and the order of the new pins,
    is the order in memory. The tool uses the order of the placed pins in the file, and
    of the library pins in the save of KiCad (library_saved).
    """
    by_number: dict[str, list[_Pin]] = {}
    for pin in pins:
        by_number.setdefault(pin.number, []).append(pin)
    # altPinMap and altPinDefs, in the order of the pins (the last pin of a number wins):
    # the alternates of a pin are the alternates of its library pin (none at the load).
    alt_map = {}
    alt_defs = {}
    for pin in pins:
        if pin.alternate:
            alt_map[pin.number] = pin.alternate
            if pin.alternate in pin.alternates:
                alt_defs[pin.number] = pin.alternates[pin.alternate]
    unassigned = list(pins)

    def alternate(pin: _Pin, library_pin: _LibPin) -> None:
        if library_pin.number not in alt_map:
            return
        name = alt_map[library_pin.number]
        if name not in pin.alternates and library_pin.number in alt_defs:
            # std::map: the alternates in the order of their names.
            for other in sorted(pin.alternates):
                if pin.alternates[other] == alt_defs[library_pin.number]:
                    name = other
                    break
        # SCH_PIN::SetAlt: no alternate for the name of the library pin (GetBaseName) or
        # for a name that the library pin does not have.
        valid = name and name != library_pin.name and name in library_pin.alternates
        pin.alternate = name if valid else ""

    free_library_pins = []
    for library_pin in new:
        candidates = by_number.get(library_pin.number)
        if not candidates:
            free_library_pins.append(library_pin)
            continue
        pin = candidates.pop(0)
        pin.alternates, pin.name = library_pin.alternates, library_pin.name
        pin.unit = library_pin.unit
        unassigned.remove(pin)
        alternate(pin, library_pin)
    result = list(pins)
    for library_pin in free_library_pins:
        if unassigned:
            pin = unassigned.pop(0)
        else:
            pin = _Pin(library_pin.number, str(uuid.uuid4()), "")
            result.append(pin)
        pin.alternates, pin.name = library_pin.alternates, library_pin.name
        pin.number, pin.unit = library_pin.number, library_pin.unit
        alternate(pin, library_pin)
    return [pin for pin in result if pin not in unassigned]


def _pin_text(pin: _Pin) -> str:
    """saveSymbol: the alternate only when it is not empty and not the pin name."""
    lines = [f"(pin {quote(pin.number)}", f'(uuid "{pin.uuid}")']
    if pin.alternate and pin.alternate != pin.name:
        lines.append(f"(alternate {quote(pin.alternate)})")
    return "\n".join([*lines, ")"])


def _library_fields(definition: Node, where: str) -> tuple[dict[str, FieldState], list[str]]:
    """The fields of the flattened library symbol (LIB_SYMBOL::GetField): the mandatory
    fields by canonical name, the user fields by name. A mandatory field that the library
    symbol does not have has the defaults of LIB_SYMBOL.

    Returns:
        (the fields, the user field names in their order).
    """
    fields: dict[str, FieldState] = {}
    users = []
    for placed in placed_fields(definition, 10):
        if placed.node is None:
            state = FieldState(
                placed.name,
                placed.text or "",
                0,
                0,
                "0",
                placed.name in _SHOWN,
                False,
                True,
                TextAttributes(),
                "",
            )
        else:
            state = read_field(placed.node, where, library=True)
            if placed.text is not None:
                state = replace(state, text=placed.text)
            state = replace(state, name=placed.name)
            if placed.name in SYMBOL_FIELD_NAMES:
                # parseProperty: SCH_FIELD::SetText strips the text of a mandatory field.
                state = replace(state, text=state.text.strip(WX_SPACE))
        fields[placed.name] = state
        if placed.name not in SYMBOL_FIELD_NAMES:
            users.append(placed.name)
    return fields, users


@dataclass
class _FieldPlan:
    # (the old node or None: a new field, the new state or None: removed). A field that
    # the update does not change has its state of the file (_placed_states).
    fields: list[tuple[Node | None, FieldState | None]]
    changes: list[str]
    references: dict[int, str]  # the offset of a (path ...): the new reference


def _placed_states(item: _Symbol) -> list[FieldState]:
    """The (property ...) of the placed symbol as read_field reads them, one time."""
    if item.states is None:
        symbol = item.instances[0].symbol
        where = _label(item)
        item.states = [read_field(node, where) for node in symbol.children("property")]
    return item.states


def _plan_fields(
    item: _Symbol,
    symbol: Node,
    library: dict[str, FieldState],
    library_users: list[str],
    update: set[str],
    options: UpdateOptions,
    power: bool,
) -> _FieldPlan:
    """The fields of processSymbols for one placed symbol.

    The position of a field: processSymbols sets the text position to the position of
    the symbol plus the position of the library field, and saveField writes
    SCH_FIELD::GetPosition, which applies the transform of the symbol (its rotation and
    mirror) to the offset.
    """
    where = placement(symbol)
    transform = symbol_transform(where)

    def position(library_field: FieldState) -> tuple[int, int]:
        dx, dy = transform_coordinate(transform, library_field.x, library_field.y)
        return where.x + dx, where.y + dy

    nodes = symbol.children("property")
    states = _placed_states(item)
    result: list[tuple[Node | None, FieldState | None]] = []
    changes: list[str] = []
    references: dict[int, str] = {}
    keys = [mandatory_name(state.name) or state.name for state in states]
    present = set(keys)
    # parseSchematicSymbol changes these fields at the load (a field of an older file, a
    # second field of one name), and KiCad 10 never writes them.
    for key in keys:
        if key in SIM_LEGACY_FIELDS:
            raise InputError(
                f"The symbol {_label(item)} has the field {key!r} of an older KiCad: KiCad "
                "makes it the attribute exclude_from_sim at the load. Open and save the "
                "schematic in KiCad first."
            )
        if keys.count(key) > 1:
            raise InputError(
                f"The symbol {_label(item)} has more than one field {key!r}: KiCad renames "
                "or replaces them at the load. Open and save the schematic in KiCad first."
            )
    # SCH_SYMBOL::Init makes the mandatory fields, and saveSymbol writes each of them:
    # a file that KiCad saved has them all.
    for name in SYMBOL_FIELD_NAMES:
        if name not in present:
            raise InputError(
                f"The symbol {_label(item)} has no field {name!r}: KiCad 10 writes each "
                "mandatory field. Open and save the schematic in KiCad first."
            )
    for node, raw in zip(nodes, states, strict=True):
        canonical = mandatory_name(raw.name)
        key = canonical or raw.name
        # parseSchField of KiCad 10: a mandatory field has its canonical name (saveField
        # writes GetCanonicalName), SCH_FIELD::SetText strips its text (WX_SPACE), and it
        # is never private (SetPrivate only for FIELD_T::USER).
        state = (
            replace(raw, name=key, text=raw.text.strip(WX_SPACE), private=False)
            if canonical
            else raw
        )
        do_update = state.private or key in update
        if not do_update:
            result.append((node, raw))
            continue
        library_field = library.get(key)
        if library_field is None:
            if canonical is None and options.remove_extra_fields:
                result.append((node, None))
                changes.append(f"{state.name} removed")
            else:
                result.append((node, raw))
            continue
        new = replace(state, private=library_field.private)
        reset = (
            options.reset_empty_fields if library_field.text == "" else options.update_field_text
        )
        if reset:
            if canonical == "Reference":
                new = replace(new, text=_ordinal_reference(item, library_field.text))
                for instance in item.instances:
                    reference = reset_reference(library_field.text, instance.reference)
                    if instance.path is not None:
                        references[instance.path.start] = reference
                    elif reference != instance.reference:
                        # SetRef makes the instance that the load made (KiCad 10:
                        # CheckForMissingSymbolInstances), and the save writes it.
                        raise InputError(
                            f"The symbol {_label(item)} has no instance for the sheet path "
                            f"{instance.sheet.shown}: KiCad writes its new reference "
                            f"{reference} into a new instance. Open and save the schematic in "
                            "KiCad first."
                        )
            elif canonical == "Value":
                if not power or options.reset_custom_power:
                    # SetValueFieldText calls SetText, which strips the text.
                    text = unescape_string(library_field.text).strip(WX_SPACE)
                    new = replace(new, text=text)
            else:
                new = replace(new, text=library_field.text)
        if options.update_field_visibilities:
            new = replace(new, visible=library_field.visible)
        if options.update_field_styles:
            new = replace(
                new,
                attributes=library_field.attributes,
                angle=library_field.angle,
                show_name=library_field.show_name,
                can_autoplace=library_field.can_autoplace,
            )
        if options.update_field_positions:
            x, y = position(library_field)
            new = replace(new, x=x, y=y)
        if new != state:
            changes.append(f"{state.name} updated")
            result.append((node, new))
        else:
            result.append((node, raw))  # no change: the text of the file stays
    for name in library_users:
        if name not in update or name in present:
            continue
        library_field = library[name]
        x, y = position(library_field)
        result.append(
            (
                None,
                FieldState(
                    name,
                    library_field.text,
                    x,
                    y,
                    library_field.angle,
                    library_field.visible,
                    False,
                    True,
                    library_field.attributes,
                    "",
                    library_field.private,
                ),
            )
        )
        present.add(name)  # AddField: the next GetField finds it
        changes.append(f"{name} added")
    return _FieldPlan(result, changes, references)


def _ordinal_reference(item: _Symbol, library_text: str) -> str:
    """The new text of the Reference field: saveSymbol writes the reference of the
    ordinal instance, the instance of the first sheet path of the screen in the
    hierarchy (GetOrdinalPath; SCHEMATIC::Hierarchy is sorted by the page numbers).
    The instances of the item are in that order.

    Raises:
        InputError: KiCad sorts the first path with an other path of the symbol by a
            hash (the same DFS count in an other top-level sheet), and the two paths get
            different references.
    """
    first = item.instances[0]
    reference = reset_reference(library_text, first.reference)
    for other in item.instances[1:]:
        if other.sheet.dfs == first.sheet.dfs and other.sheet.top != first.sheet.top:
            if reset_reference(library_text, other.reference) != reference:
                raise InputError(
                    f"KiCad sorts the sheet paths {first.sheet.shown} and {other.sheet.shown} "
                    "of the symbol "
                    f"{_label(item)} in an order that the tool cannot know (a hash of the "
                    "path), and the Reference field gets the reference of the first one. "
                    "Update the Reference of this symbol in KiCad."
                )
    return reference


def _check_autoplace(item: _Symbol, symbol: Node, options: UpdateOptions) -> None:
    if not options.update_field_positions:
        return
    # parseSchematicSymbol: each true token sets AUTOPLACE_AUTO; a false one does not
    # reset it.
    where = f"the symbol {_label(item)}"
    # The parser reads each token: a token that is not yes or no is an error.
    values = [parse_maybe_absent_bool(node, where) for node in symbol.children("fields_autoplaced")]
    if True in values:
        raise InputError(
            f"The fields of {_label(item)} are placed automatically (fields_autoplaced): with "
            '"Update/reset field positions", KiCad places them again with the text sizes of '
            "its stroke font (AutoplaceFields), which the tool cannot measure. Update this "
            "symbol in KiCad, or keep update_field_positions false."
        )


def _reference_key(instance: PlacedInstance) -> tuple[str, int, str, int]:
    """The sort key of the rows: the prefix, the number and the unit of the reference."""
    split = split_reference(instance.reference)
    return split.prefix, split.number, instance.reference, instance.unit


def _label(item: _Symbol) -> str:
    """The references of the symbol in each sheet path, with the letters of the unit of a
    package with more units (U1A), as tools/symbol_fields writes them."""
    return ", ".join(
        dict.fromkeys(
            i.reference + (letter_reference(i.unit) if i.unit_count > 1 else "")
            for i in sorted(item.instances, key=_reference_key)
        )
    )


def _matches(
    instance: PlacedInstance, match: str, pattern: str, wanted_id: tuple[str, str]
) -> bool:
    """DIALOG_CHANGE_SYMBOLS::isMatch for one instance."""
    if match == "all":
        return True
    if match == "reference":
        return wild_compare(pattern, unescape_string(instance.reference))
    if match == "value":
        # GetField( VALUE )->GetText(): the text of the Value field after the load.
        return wild_compare(pattern, unescape_string(instance.value))
    # GetLibId() == id: the parser of the schematic changes "{slash}" to "/" and refuses
    # a library ID that is not valid.
    lib_id = instance.symbol.first("lib_id")
    text = lib_id.value(0, "") if lib_id else ""
    return parse_lib_id(text.replace("{slash}", "/")) == wanted_id


def _wanted_lib_id(pattern: str) -> tuple[str, str]:
    """isMatch: LIB_ID::Parse of getLibIdValue (EscapeString(CTX_LIBID) of the two parts
    at the first ":"; with no ":", the full text is the library)."""
    library, colon, name = pattern.partition(":")
    return parse_lib_id(escape_lib_id(library) + ":" + escape_lib_id(name if colon else ""))


@dataclass
class _Result:
    texts: dict[Path, str]
    files: dict[Path, SchematicFile]
    rows: list[tuple[str, str, str]]  # (references, library ID, changes)
    warnings: list[str]
    # The warnings of older libraries: recorded after the write (check_library_files).
    library_warnings: list[str]


def _definition(
    item: _Symbol,
    kicad: str,
    project: Path,
    version: int,
    cache: dict[tuple[str, int], _Definition],
    problems: list[str],
    library_warnings: list[str],
) -> None:
    """Find and flatten the library symbol (SCH_BASE_FRAME::GetLibSymbol, Flatten), and
    read its fields. The cache holds the definition of each library ID and file version.

    Args:
        problems: Gets the problems of the library tables (KiCad shows them too).
        library_warnings: Gets the warnings of the older libraries
            (check_library_files): the caller records them after all checks.

    Raises:
        InputError: The library ID is not valid, the libraries do not have the symbol, the
            library symbol cannot go into the schematic, or it has too few units.
    """
    library_name, _colon, name = item.lib_id.partition(":")
    if not name:
        raise InputError(f"{_label(item)}: the library ID {item.lib_id!r} is not valid.")
    if (item.lib_id, version) in cache:
        item.definition = cache[item.lib_id, version]
        _check_units(item)
        return
    try:
        chain, row, table_problems = find_library_symbol(library_name, name, kicad, project)
        files = [(symbol.file, symbol.version) for symbol in chain]
        warnings = file_versions.check_library_files("symbol", {row.path: files}, kicad)
    except (LibraryLookupError, file_versions.FileVersionError) as e:
        raise InputError(f"{_label(item)}: KiCad does not find the symbol: {e}") from e
    try:
        # flatten copies the text of the file: the text of a save in KiCad, which KiCad
        # must read in the schematic in the same way (library_saved.schematic_chain).
        saved = schematic_chain(chain, row.path, kicad, version)
    except SymbolLibraryError as e:
        raise InputError(f"{_label(item)}: {e.problem}") from e
    except (KicadFileError, SavedLibraryError) as e:
        raise InputError(
            f"{_label(item)}: the library symbol {item.lib_id} of {row.path}: {e}"
        ) from e
    library_warnings.extend(w for w in warnings if w not in library_warnings)
    problems.extend(p for p in table_problems if p not in problems)
    try:
        text = flatten(saved, item.lib_id, rules_for_version(version).kicad)
        node = parse(text)
        # KicadParseError (a sub-symbol name) is a KicadFileError.
        item.definition = _Definition(
            node,
            indent(text, 2),
            indent(text, 0),
            _library_fields(node, item.lib_id),
            unit_count(node),
        )
    except (KicadFileError, SExprError) as e:
        raise InputError(
            f"{_label(item)}: the library symbol {item.lib_id} of {row.path}: {e}"
        ) from e
    cache[item.lib_id, version] = item.definition
    _check_units(item)


def _check_units(item: _Symbol) -> None:
    """processSymbols: a library symbol with fewer units than the symbol is an error."""
    assert item.definition is not None
    unit = symbol_unit(item.instances[0].symbol)
    if item.definition.units < unit:
        raise InputError(
            f"{_label(item)}: the library symbol {item.lib_id} has fewer units than the unit "
            f"{unit} of the symbol."
        )


def _update_set(items: list[_Symbol], fields: list[str] | None) -> set[str]:
    """The checked fields of m_fieldsBox: given, or the defaults of the dialog.

    The list of the dialog has the mandatory fields, and the user fields (not private)
    of the matching symbols and of their library symbols.

    Raises:
        InputError: A given name is not in the list of the dialog.
    """
    users = set()
    for item in items:
        for state in _placed_states(item):
            if mandatory_name(state.name) is None and not state.private:
                users.add(state.name)
        assert item.definition is not None
        library, names = item.definition.library
        users.update(name for name in names if not library[name].private)
    if fields is None:
        return {"Footprint", "Datasheet", "Description"} | users
    for name in fields:
        canonical = mandatory_name(name)
        if canonical is not None and canonical != name:
            raise InputError(f"Give the field name {canonical!r}, not {name!r}.")
        if canonical is None and name not in users:
            raise InputError(
                f"The matching symbols and their library symbols have no field {name!r} that "
                "the dialog lists (the names of the user fields have case): "
                f"{', '.join(sorted(users)) or 'no user fields'}."
            )
    return set(fields)


def _symbol_edits(
    item: _Symbol,
    record: SchematicFile,
    update: set[str],
    options: UpdateOptions,
    old_pins_cache: dict[tuple[Path, str, int], list["_LibPin"]],
) -> tuple[list[tuple[int, int, str]], list[str]]:
    """The edits of one placed symbol, and its changes for the output.

    Args:
        old_pins_cache: The pins of the copies of the files, by file, name and body style.
    """
    symbol = item.instances[0].symbol
    assert item.definition is not None
    shared = item.definition
    definition = shared.node
    label = _label(item)
    edits: list[tuple[int, int, str]] = []
    changes: list[str] = []
    lib_name = symbol.first("lib_name")
    if lib_name is not None:
        edits.append(_remove(lib_name))
    power = definition.first("power") is not None
    library, users = shared.library
    plan = _plan_fields(item, symbol, library, users, update, options, power)
    changes += plan.changes
    nodes = symbol.children("property")
    unchanged = {id(state) for state in _placed_states(item)}
    for old, new in plan.fields:
        if old is None or (new is not None and id(new) in unchanged):
            continue
        if new is None:
            edits.append(_remove(old))
        else:
            edits.append((old.start, old.end, field_text(new, 2)))
    added = [new for old, new in plan.fields if old is None and new is not None]
    for instance in item.instances:
        path = instance.path
        if path is None or path.start not in plan.references:
            continue
        new_reference = plan.references[path.start]
        ref = path.first("reference")
        if ref is None or not ref.atoms():
            if new_reference != instance.reference:
                raise InputError(
                    f"The instance of {label} for the sheet path {instance.sheet.shown} has no "
                    f"(reference ...): KiCad writes its new reference {new_reference}. Open and "
                    "save the schematic in KiCad first."
                )
        elif ref.value(0, "") != new_reference:
            atom = ref.atoms()[0]
            edits.append((atom.start, atom.end, quote(new_reference)))
            changes.append(
                f"reference {ref.value(0, '')} of {instance.sheet.shown} is now {new_reference}"
            )
    # The attributes of the flattened library symbol (not DNP).
    if options.update_attributes:
        before = symbol_tokens(symbol, label)
        after = dict(before)
        for token, default in _LIBRARY_FLAGS.items():
            node = definition.children(token)
            # The library parser reads each flag with parseBool; the last one stays.
            values = [parse_bool(flag, f"the library symbol {item.lib_id}") for flag in node]
            after[token] = values[-1] if values else default
            current = symbol.children(token)
            if after[token] != before[token]:
                if not current:
                    raise InputError(
                        f"The symbol {label} has no ({token} ...): KiCad 10 writes it. Open and "
                        "save the schematic in KiCad first."
                    )
                atom = current[-1].atoms()[0]
                edits.append((atom.start, atom.end, "yes" if after[token] else "no"))
                changes.append(f"{token} {'yes' if after[token] else 'no'}")
        instances = symbol.first("instances")
        for project in instances.children("project") if instances is not None else []:
            for path in project.children("path"):
                if path.children("variant"):
                    state = read_variants(label, record, path, item.instances[0].key, symbol)
                    edit = variant_edit(state, before, after)
                    if edit is not None:
                        edits.append(edit)
    # The pins (UpdatePins) and the alternates (processSymbols).
    old_key = definition_key(symbol)
    old_definition = record.library.get(old_key)
    style = body_style(symbol)
    if old_definition is not None and (record.file, old_key, style) not in old_pins_cache:
        old_pins_cache[record.file, old_key, style] = _library_pins(old_definition, style)
    old_pins = old_pins_cache.get((record.file, old_key, style), [])
    file_pins = _file_pins(symbol)
    before = {id(pin): (pin.number, pin.alternate) for pin in file_pins}
    # The load (UpdateLocalLibSymbolLinks): UpdatePins with the copy of the file. With
    # no copy, KiCad keeps the pins as they are (no library symbol).
    loaded = _update_pins(file_pins, old_pins) if old_definition is not None else file_pins
    pins = _update_pins(loaded, shared.pins_of(style))
    # processSymbols clears the alternates of the pins of each instance
    # (SCH_SYMBOL::GetPins of the sheet: the pins of its unit and of each unit; each
    # pin for the unit 0).
    units = {instance.unit for instance in item.instances}
    for pin in pins:
        if (
            (0 in units or pin.unit == 0 or pin.unit in units)
            and pin.alternate
            and (
                options.reset_alternate_pins
                or pin.alternate == pin.name
                or pin.alternate not in pin.alternates
            )
        ):
            pin.alternate = ""
    # The changes of the pins against the file: the same pin objects (the UUID stays).
    # The alternates that UpdatePins (SetAlt, an alternate of the same type and shape)
    # or the clear changed.
    kept = {id(pin) for pin in pins}
    new_numbers = [pin.number for pin in pins if id(pin) not in before]
    removed = [pin.number for pin in file_pins if id(pin) not in kept]
    for pin in pins:
        if id(pin) not in before:
            continue
        number, old = before[id(pin)]
        if number != pin.number:
            changes.append(f"pin {number} is now pin {pin.number}")
        if old and not pin.alternate:
            changes.append(f"alternate {old} of pin {pin.number} cleared")
        elif old and pin.alternate != old:
            changes.append(f"alternate {old} of pin {pin.number} is now {pin.alternate}")
    if new_numbers:
        changes.append(f"new pins {', '.join(new_numbers)}")
    if removed:
        changes.append(f"pins {', '.join(removed)} removed")
    old_pin_nodes = symbol.children("pin")
    starts = [child.start for child in symbol.children()]
    if old_pin_nodes:
        first = starts.index(old_pin_nodes[0].start)
        if starts[first : first + len(old_pin_nodes)] != [pin.start for pin in old_pin_nodes]:
            raise InputError(
                f"The pins of {label} are not next to each other in {record.file.name}. Open "
                "and save the schematic in KiCad first."
            )
    text = "\n".join(_pin_text(pin) for pin in pins)
    if old_pin_nodes:
        start, end = old_pin_nodes[0].start, old_pin_nodes[-1].end
        new_text = indent(text, 2).lstrip("\t") if text else ""
        if new_text != symbol.source[start:end]:
            if new_text:
                edits.append((start, end, new_text))
            else:
                edits.append(removal(symbol.source, start, end))
    elif text:
        # saveSymbol writes the pins after the fields and before the instances.
        instances = symbol.first("instances")
        pin_text = indent(text, 2).lstrip("\t")
        if instances is not None:
            edits.append((instances.start, instances.start, pin_text + "\n\t\t"))
        else:
            end = symbol.end - 1  # the ")" of the symbol, on its own line
            edits.append((end, end, "\t" + pin_text + "\n\t"))
    if added:
        # The new fields go after the last field. When the removal of all pins starts
        # there, they go after the removed text (edited: no insertion at the start of a
        # replaced range).
        offset = nodes[-1].end
        offset = next((end for start, end, _text in edits if start == offset < end), offset)
        edits.append((offset, offset, "".join("\n\t\t" + field_text(f, 2) for f in added)))
    return edits, changes


def _remove(node: Node) -> tuple[int, int, str]:
    return removal(node.source, node.start, node.end)


def _same_copy(text: str, definition: _Definition) -> bool:
    """A copy of (lib_symbols ...) with the text of the library symbol. KiCad compares the
    symbols (LIB_SYMBOL::operator==): the tool compares only the texts."""
    return indent(text, 0) == definition.normalized


def _library_edits(
    record: SchematicFile,
    updated: list[_Symbol],
    all_instances: list[PlacedInstance],
) -> tuple[list[tuple[int, int, str]], set[str]]:
    """The edit of the (lib_symbols ...) of one file (SCH_SCREEN::Remove and Append), and
    the names of the copies with a new text.

    processSymbols first removes each updated symbol: Remove deletes its copy when no
    other symbol of the screen uses that name. Then Append compares the new library
    symbol with the copy of the name "<nickname>:<name>" that stays, also a copy that no
    symbol uses (the load keeps each copy: UpdateLocalLibSymbolLinks), and adds it when
    there is no such copy. KiCad writes the copies in the order of their names
    (std::map).

    Raises:
        InputError: A copy with the name of an updated symbol stays and is not the
            library symbol (KiCad would make a copy "<name>_<n>").
    """
    root = record.root
    section = root.first("lib_symbols")
    if section is None:
        raise InputError(f"{record.file.name} has no (lib_symbols ...).")
    entries = {node.value(0, ""): node for node in section.children("symbol")}
    texts = {name: root.source[node.start : node.end] for name, node in entries.items()}
    updated_keys = {i.key for item in updated for i in item.instances}
    # The names that the other symbols of the file use (each placed symbol one time).
    kept: dict[str, list[str]] = {}
    seen = set()
    for instance in all_instances:
        if instance.file != record.file or instance.key in updated_keys or instance.key in seen:
            continue
        seen.add(instance.key)
        kept.setdefault(definition_key(instance.symbol), []).append(instance.reference)
    for item in updated:
        name = definition_key(item.instances[0].symbol)
        if name not in kept:
            texts.pop(name, None)
    for item in updated:
        name = item.lib_id
        assert item.definition is not None
        old = texts.get(name)
        if old is None:
            texts[name] = item.definition.text.lstrip("\t")
        elif not _same_copy(old, item.definition):
            result = (
                "its text is not the text of the library symbol. When KiCad finds the two "
                "symbols different (LIB_SYMBOL::operator==; the tool compares only the texts), "
                "it gives the updated symbols a copy with an other name."
            )
            if name in kept:
                raise InputError(
                    f"The symbols {', '.join(sorted(kept[name]))} of {record.file.name} keep "
                    f"the old copy of {name}, and {result} Add them to the match."
                )
            raise InputError(
                f"{record.file.name} has an old copy of {name} that no symbol uses, and "
                f"{result} The tool does not do this: update the symbols in KiCad."
            )
    old_texts = {name: root.source[node.start : node.end] for name, node in entries.items()}
    changed = {name for name, text in texts.items() if old_texts.get(name) != text}
    if list(texts) == list(entries) and not changed:
        return [], changed
    if not texts:
        return [(section.start, section.end, "(lib_symbols)")], changed
    body = "\n".join("\t\t" + texts[name] for name in sorted(texts))
    return [(section.start, section.end, f"(lib_symbols\n{body}\n\t)")], changed


def _plan(
    project: Path,
    kicad: str,
    match: str,
    pattern: str,
    fields: list[str] | None,
    options: UpdateOptions,
) -> _Result:
    instances, screens, problems = placed_instances(project, kicad, "gui")
    if problems:
        raise InputError(
            "The tool cannot read the full sheet hierarchy: "
            + " ".join(problems)
            + " Correct it in KiCad first."
        )
    files, shared = screens_of_files(list(screens.values()))
    wanted_id = _wanted_lib_id(pattern) if match == "lib_id" else ("", "")
    by_key: dict[tuple[str, int], list[PlacedInstance]] = {}
    for instance in instances:
        by_key.setdefault(instance.key, []).append(instance)
    items: list[_Symbol] = []
    for group in by_key.values():
        matched = [i for i in group if _matches(i, match, pattern, wanted_id)]
        if not matched:
            continue
        if len(matched) != len(group):
            others = sorted({i.reference for i in group} - {i.reference for i in matched})
            raise InputError(
                f"The symbol of {', '.join(sorted({i.reference for i in matched}))} is in a sheet "
                "that the hierarchy uses more than one time: KiCad updates the symbol, thus also "
                f"{', '.join(others)}. Give a match that has all of them."
            )
        if group[0].screen.key in shared:
            raise InputError(
                f"KiCad loads the file {group[0].file} under more than one name: the tool does "
                "not update its symbols. Give the file one name in all sheets."
            )
        lib_id = group[0].symbol.first("lib_id")
        items.append(_Symbol(group, lib_id.value(0, "") if lib_id else ""))
    if not items:
        raise InputError("No symbols matching criteria found (the match finds no symbol).")
    # The parser changes "{slash}" to "/" in lib_id, lib_name and the names of the copies,
    # and KiCad 10 writes "/" (LIB_ID::Format): such a file was not saved by KiCad 10.
    for item in items:
        record = files[item.instances[0].file]
        names = [item.lib_id, definition_key(item.instances[0].symbol), *record.library]
        if any("{slash}" in name for name in names):
            raise InputError(
                f'{record.file.name} has a library ID with "{{slash}}" (the symbol '
                f'{_label(item)} or a copy of lib_symbols): KiCad reads it as "/". Open and '
                "save the schematic in KiCad first."
            )
    definitions: dict[tuple[str, int], _Definition] = {}
    table_problems: list[str] = []
    library_warnings: list[str] = []
    for item in items:
        record = files[item.instances[0].file]
        _definition(
            item, kicad, project, record.version, definitions, table_problems, library_warnings
        )
        _check_autoplace(item, item.instances[0].symbol, options)
    update = _update_set(items, fields)
    edits: dict[Path, list[tuple[int, int, str]]] = {}
    old_pins: dict[tuple[Path, str, int], list[_LibPin]] = {}
    planned = []
    for item in sorted(items, key=lambda i: min(map(_reference_key, i.instances))):
        record = files[item.instances[0].file]
        symbol_edits, changes = _symbol_edits(item, record, update, options, old_pins)
        edits.setdefault(record.file, []).extend(symbol_edits)
        planned.append((item, record, bool(symbol_edits), changes))
    changed_copies: dict[Path, set[str]] = {}
    for path, record in files.items():
        updated = [item for item in items if item.instances[0].file == path]
        if updated:
            copy_edits, changed_copies[path] = _library_edits(record, updated, instances)
            edits.setdefault(path, []).extend(copy_edits)
    rows = []
    for item, record, edited_symbol, changes in planned:
        if not changes:
            renamed = definition_key(item.instances[0].symbol) != item.lib_id
            if renamed or item.lib_id in changed_copies.get(record.file, set()):
                changes = ["the copy of the library symbol"]
            elif edited_symbol:
                # For example a pin with no (uuid ...): KiCad gives it one at the load.
                changes = ["the text of the symbol, as KiCad writes it"]
            else:
                changes = ["no change"]
        rows.append((_label(item), item.lib_id, "; ".join(changes)))
    texts = {}
    for path, file_edits in edits.items():
        root = files[path].root
        text = root.source[: root.start] + edited(root, file_edits) + root.source[root.end :]
        if text != root.source:
            texts[path] = text
    warnings = []
    if table_problems:
        # KiCad uses no row of these tables, as the tool, and shows the problems.
        warnings.append(
            "Library table problems (KiCad shows them too): " + " ".join(table_problems)
        )
    if options.update_pin_text_visibility:
        warnings.append(
            '"Update/reset pin name/number visibilities" changes nothing in the file: the '
            "pin name and number visibility is a part of the copy of the library symbol, which "
            "the update replaces (SCH_SYMBOL::SetShowPinNames)."
        )
    return _Result(texts, files, rows, warnings, library_warnings)


def _text_problem(texts: dict[Path, str]) -> str | None:
    """The error of a new text that the parser refuses (a defect of the tool), or None."""
    for file, text in texts.items():
        try:
            parse(text)
        except SExprError as e:
            return (
                f"❌ The new text of {file.name} is not valid ({e}): this is an error of the tool."
            )
    return None


@mcp.tool()
@with_schematic_versions("schematic_path", write=True)
@with_library_versions
async def update_symbols_from_library(
    schematic_path: str,
    match: Literal["all", "reference", "value", "lib_id"] = "all",
    pattern: str = "",
    fields: list[str] | None = None,
    options: UpdateOptions | None = None,
) -> str:
    """Update placed symbols from their library symbols, as "Update Symbols from Library"
    of the schematic editor of KiCad 10 does it.

    Each matching symbol gets the library symbol of its library ID (from the global and
    the project symbol library tables, flattened): the new shape and pins, the keywords
    and the footprint filters (always, as the dialog). The fields, the attributes and the
    alternate pins follow the options, with the defaults of the dialog. The library
    symbol is the text of a save in kicad-cli, as KiCad loads it: kicad-cli saves each
    library symbol with its parents one time (about 0.2 s), and again after a change of
    their text; a cache keeps the last 256 saves.

    The pins follow the new library symbol (SCH_SYMBOL::UpdatePins): a pin keeps its
    UUID when the library symbol has its number; a new pin gets a new UUID, and a pin
    that the library symbol does not have goes. KiCad makes random UUIDs, and the order
    of a new pin comes from the memory: the tool uses the order of the placed pins in
    the file and of the library pins in the save of KiCad.

    The fields of the update (fields): with no list, the dialog defaults: Footprint,
    Datasheet and Description, and each user field of the matching symbols and of their
    library symbols; not Reference and not Value. A private field is always updated. The
    position of a field (update_field_positions, and a new field) is the position of the
    symbol plus the position of the library field, rotated and mirrored as the symbol
    (SCH_FIELD::GetPosition). A user field of the library that the symbol does not have
    becomes a new field.

    The tool refuses these updates:

    - A match with no symbol, or a field name that the dialog does not list.
    - A library ID that is not valid, or that the libraries do not have. A library
      symbol with fewer units than the symbol.
    - A symbol with no mandatory field (Reference, Value, Footprint, Datasheet or
      Description), with two fields of one name, or with a field or a library ID that the
      load changes (a field of the simulation of KiCad 7, "{slash}"): KiCad 10 never
      writes them (open and save the file in KiCad).
    - A symbol with fields_autoplaced and update_field_positions: KiCad places the
      fields with the sizes of its font, which the tool cannot measure.
    - A match that has only some instances of a symbol of a shared sheet.
    - A new reference for a sheet path with no instance in the file (KiCad makes the
      instance), or for two sheet paths that KiCad sorts by a hash.
    - A match that leaves an old copy of the library symbol with the same name and an
      other text in the file: a copy of an other symbol (add those symbols to the
      match), or a copy that no symbol uses. KiCad makes a copy with an other name when
      it finds the two symbols different. The tool compares only the texts, thus it can
      refuse a copy that KiCad finds equal.

    "Update/reset pin name/number visibilities" changes nothing in the file: the update
    replaces the copy of the library symbol that holds it.

    The tool checks all symbols and all files first, and writes no file when a check
    fails. It writes all files or no file. KiCad must not have the files open. The
    installed KiCad must be KiCad 10.0.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool finds the symbols in
            the full project.
        match: "all" (each symbol), "reference" or "value" (pattern with "*" and "?", no
            case), or "lib_id" (pattern is "<library>:<symbol>").
        pattern: The text of the match "reference", "value" or "lib_id".
        fields: The names of the fields to update, or none for the dialog defaults. The
            names of the user fields have case.
        options: The update options of the dialog; a missing option has the default of
            the dialog.

    Returns:
        A table of the updated symbols and their changes, and the files.
    """
    try:
        return await _update(schematic_path, match, pattern, fields, options or UpdateOptions())
    except Exception as e:
        return error_answer(e, "Error")


async def _update(
    schematic_path: str,
    match: str,
    pattern: str,
    fields: list[str] | None,
    options: UpdateOptions,
) -> str:
    path = Path(os.path.abspath(os.path.expanduser(schematic_path)))
    if path.suffix != ".kicad_sch" or not path.is_file():
        return f"❌ The schematic {path} does not exist or is not a .kicad_sch file."
    if match != "all" and not pattern:
        return f'❌ The match "{match}" needs a pattern.'
    try:
        project = await asyncio.to_thread(schematic_project_file, path)
    except AmbiguousRootError as e:
        return f"❌ {e}. Give the root schematic of the project."
    kicad = await get_kicad_version()
    if problem := kicad_10_problem(kicad, "update_symbols_from_library has"):
        return f"❌ {problem}"
    try:
        # The rules of the load of the hierarchy need a known patch release.
        kicad_major(kicad)
    except InputError as e:
        return f"❌ {e}"
    try:
        result = await asyncio.to_thread(_plan, project, kicad, match, pattern, fields, options)
    except (OSError, KiCadEncodingError, SExprError, KicadFileError) as e:
        return f"❌ The schematic files of {project} cannot be read: {e}"
    if problem := await asyncio.to_thread(_text_problem, result.texts):
        return problem
    changed = sum(1 for _refs, _lib_id, change in result.rows if change != "no change")
    same = len(result.rows) - changed
    lines = [f"# Update of the symbols of {project.name} from the libraries", ""]
    warnings: list[str] = []
    if result.texts:
        stamps = {file: result.files[file].stamp for file in result.texts}
        written = await asyncio.to_thread(write_schematics, result.texts, stamps)
        if isinstance(written, str):
            return written
        files, warnings = written
        names = ", ".join(f"`{p.name}`" for p in files)
        symbols = "1 symbol" if changed == 1 else f"{changed} symbols"
        unchanged = {0: "", 1: " 1 matching symbol has no change."}.get(
            same, f" {same} matching symbols have no change."
        )
        lines.append(f"✅ The tool updated {symbols} and wrote {names}.{unchanged}")
    else:
        lines.append(
            f"✅ No change: the matching symbols ({same}) and the copies of their library "
            "symbols are the same as the library symbols. The tool wrote no file."
        )
    # After the write: an error has no warning before it (check_library_files).
    record_library_warnings(result.library_warnings)
    lines += ["", "| Symbols | Library ID | Changes |", "|---|---|---|"]
    lines += [f"| {cell(r)} | {cell(i)} | {cell(c)} |" for r, i, c in result.rows]
    notes = result.warnings + warnings
    if notes:
        lines += ["", *(f"⚠️ {w}" for w in notes)]
    return "\n".join(lines)
