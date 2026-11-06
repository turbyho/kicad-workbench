"""Text parser for KiCad schematic files (.kicad_sch)."""

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from ..errors import InputError
from ..utils.file_handlers import validate_kicad_file
from ..utils.kicad_text import read_as_kicad
from ..utils.sch_hierarchy import sheet_files
from .kicad_fields import (
    FieldRules,
    record_field_rules,
    rules_for_file,
    sheet_fields,
    symbol_fields,
)
from .kicad_versions import KicadFileError, file_text, kicad_release, version_triple
from .lib_symbols import LibrarySymbol, pin_number, sub_symbols, symbol_name
from .sch_geometry import body_style, node_point, symbol_unit
from .sch_geometry import definition_key as _lib_key
from .sexpr import Node, parse
from .sheet_list import str_num_cmp

# KiCad-defined boolean flags (fixed set, not user-extensible)
KICAD_SYMBOL_FLAGS = ("dnp", "in_bom", "on_board", "exclude_from_sim")
KICAD_FLAG_DEFAULTS = {"dnp": False, "in_bom": True, "on_board": True, "exclude_from_sim": False}


@dataclass
class SchematicComponent:
    """Component from schematic file."""

    reference: str
    value: str
    library_id: str
    footprint: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    position: tuple[float, float] = (0.0, 0.0)
    unit: int | None = None
    pins: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=lambda: dict(KICAD_FLAG_DEFAULTS))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchematicComponent":
        """Create from a dict of the parser (_parse_file)."""
        # Extract properties
        properties = {}
        for prop in data.get("properties", []):
            key = prop.get("key", "")
            value = prop.get("value", "")
            if key and value:
                properties[key] = value

        # Get position
        at = data.get("at", {})
        position = (float(at.get("x", 0)), float(at.get("y", 0)))

        # Get unit
        unit = data.get("unit")

        return cls(
            reference=data.get("reference", ""),
            value=properties.get("Value", data.get("value", "")),
            library_id=data.get("lib_id", ""),
            footprint=properties.get("Footprint"),
            properties=properties,
            position=position,
            unit=unit,
            pins=data.get("pins", []),
            flags=data.get("flags", dict(KICAD_FLAG_DEFAULTS)),
        )


@dataclass
class SchematicNet:
    """Net from schematic file."""

    name: str
    code: int
    node_count: int = 0
    pins: list[str] = field(default_factory=list)
    type: str = "unknown"
    position: tuple[float, float] = (0.0, 0.0)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchematicNet":
        """Create from a dict of the parser (_parse_file)."""
        return cls(
            name=data.get("name", ""),
            code=data.get("code", 0),
            type=data.get("type", "unknown"),
            position=data.get("position", (0.0, 0.0)),
        )


@dataclass
class SchematicPin:
    """Pin definition from symbol."""

    number: str
    name: str
    type: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SchematicPin":
        """Create from a dict of the parser (_parse_file)."""
        return cls(
            number=data.get("number", ""),
            name=data.get("name", ""),
            type=data.get("electrical_type", ""),
        )


def _merge_duplicate_references(
    components: list[SchematicComponent],
) -> list[SchematicComponent]:
    """Merge multi-unit symbol instances that share a reference.

    KiCad stores each unit of a multi-unit symbol as its own symbol block
    (often spread across sub-sheets), but the physical component is one
    entity — netlist lookups and per-reference reports expect it once. Pin
    metadata from all units is unioned by pin number; the first instance's
    value/footprint wins (units share them by construction).
    """
    merged: dict[str, SchematicComponent] = {}
    for comp in components:
        key = comp.reference or f"__unannotated_{len(merged)}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = comp
            continue
        known_pins = {str(p.get("number", "")) for p in existing.pins}
        for p in comp.pins:
            if str(p.get("number", "")) not in known_pins:
                existing.pins.append(p)
    return list(merged.values())


# The escapes that KiCad writes in the text of a label, and their characters.
_TEXT_ESCAPES = {
    "dblquote": '"',
    "quote": "'",
    "lt": "<",
    "gt": ">",
    "backslash": "\\",
    "slash": "/",
    "bar": "|",
    "comma": ",",
    "colon": ":",
    "space": " ",
    "dollar": "$",
    "tab": "\t",
    "return": "\n",
    "brace": "{",
}


def _shown_text(text: str) -> str:
    """Return the text of a label as KiCad shows it and names its net.

    KiCad writes some characters of a label as escapes, for example "{slash}"
    for "/". Braces after "$", "~", "^" or "_" are markup (a variable,
    an overbar, a superscript, a subscript) and stay. An unknown escape and
    a brace with no end also stay.
    """
    result = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            result.append(text[i])
            i += 1
            continue
        depth = 1
        end = i + 1
        while end < len(text):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        token = text[i + 1 : end]
        if end >= len(text):
            result.append("{" + _shown_text(token))
        elif i > 0 and text[i - 1] in "$~^_":
            result.append("{" + _shown_text(token) + "}")
        elif token in _TEXT_ESCAPES:
            result.append(_TEXT_ESCAPES[token])
        else:
            result.append("{" + _shown_text(token) + "}")
        i = end + 1
    return "".join(result)


def _definition_pins(definition: Node, file: Path, version: int) -> list[dict[str, Any]]:
    """Return the pins of a symbol definition, with their unit and body style.

    The pins of a unit are in the sub-symbol "<name>_<unit>_<style>" (the name
    rule of lib_symbols.sub_symbols). A pin outside a sub-symbol is in unit 1
    with body style 1 (parseLibSymbol of KiCad 8, 9 and 10). Unit 0 and style 0
    are common to all units and styles. A pin with no (number ...) has an empty
    number.

    Each pin has "file_number" (GetNumber: the number of the file, with "_" for
    each space, SCH_PIN::SetNumber), "file_name" (GetBaseName), "number" and
    "name" (the texts of the netlist:
    file_text; KiCad 8 and 9 give "~" as an empty text with GetShownName and
    GetShownNumber), "electrical_type" (the token of the file) and "alternates"
    (the electrical type of each (alternate "X" <type> <shape>)).

    Raises:
        KicadFileError: KiCad cannot read the name of a sub-symbol.
    """
    symbol = LibrarySymbol(symbol_name(definition.value(0, "")), definition, file, version)
    pins = []
    for unit, style, group in [(1, 1, definition), *sub_symbols(symbol)]:
        # (pin <electrical type> <graphic style> ... (name "X") (number "N"))
        for pin in group.children("pin"):
            name = pin.first("name")
            number = pin.first("number")
            file_number = pin_number(number.value(0, "") if number else "")
            at = pin.first("at")
            pins.append({
                "unit": unit,
                "style": style,
                # (at x y) in IU, with the Y axis of the file (up).
                "at": node_point(at) if at is not None else (0, 0),
                "file_number": file_number,
                "number": file_text(file_number, version),
                "file_name": name.value(0, "") if name else "",
                "name": file_text(name.value(0, "") if name else "", version),
                "electrical_type": pin.value(0, ""),
                "alternates": {
                    alternate.value(0, ""): alternate.value(1, "")
                    for alternate in pin.children("alternate")
                },
            })
    return pins


# The rules of SCH_PIN::SetAlt: "keep" keeps each alternate (KiCad 8.0, 9.0.0 to
# 9.0.2); "base" keeps no empty alternate and not the base name of the pin (9.0.3:
# its check of the library alternates does not change m_alt); "library" also keeps
# no alternate that the library pin does not have (9.0.4 to 9.0.9, 10.0.0 to
# 10.0.7, branch master).
_SET_ALT_RULES = ("keep", "base", "library")


def _kicad_rules(kicad: str | None) -> tuple[int, str] | None:
    """Return the major version (the sort of the library pins) and the SetAlt rules of
    the KiCad version that reads a schematic, or None when the server does not know
    them (no version, or an other version)."""
    numbers = version_triple(kicad) if kicad else None
    if numbers is None:
        return None
    if kicad_release(kicad) is None:
        # A nightly build of branch master (10.99): the rules of master.
        return (10, "library") if numbers[:2] == (10, 99) else None
    major, _minor, patch = numbers
    if major == 8 or (major == 9 and patch <= 2):
        return major, "keep"
    if major == 9 and patch == 3:
        return 9, "base"
    if (major == 9 and patch <= 9) or (major == 10 and patch <= 7):
        return major, "library"
    return None


def _set_alt(alt: str, pin: dict[str, Any], rules: str) -> str:
    """Return the alternate that SCH_PIN::SetAlt keeps (_SET_ALT_RULES)."""
    if rules == "keep":
        return alt
    if not alt or alt == pin["file_name"]:
        return ""
    if rules == "library" and alt not in pin["alternates"]:
        return ""
    return alt


# The electrical type of an alternate that the library pin does not have:
# GetAlternates()[name] adds an ALT with zero values, and the first value of
# ELECTRICAL_PINTYPE is PT_INPUT (SCH_PIN::GetType).
_UNKNOWN_ALTERNATE_TYPE = "input"


def _library_order(definition: list[dict[str, Any]], major: int) -> list[int]:
    """Return the indexes of the library pins in the order of LIB_SYMBOL::GetPins.

    The parser sorts the draw items (GetDrawItems().sort(), operator< and compare):
    KiCad 8 (LIB_PIN::compare): unit, body style, number (wxString::Cmp), name,
    position; KiCad 9 (SCH_PIN::compare): unit, body style, position, number (Cmp);
    KiCad 10: unit, body style, number (StrNumCmp), position. KiCad 9 and 10 read
    the Y of a pin inverted (parseXY( true )). The sort is std::sort: pins that are
    equal in these keys keep the order of the file here.
    """

    def key(index: int) -> tuple:
        pin = definition[index]
        x, y = pin["at"]
        if major == 8:
            return (pin["unit"], pin["style"], pin["file_number"], pin["file_name"], x, y)
        if major == 9:
            return (pin["unit"], pin["style"], x, -y, pin["file_number"])
        return (pin["unit"], pin["style"], cmp_to_key(str_num_cmp)(pin["file_number"]), x, -y)

    return sorted(range(len(definition)), key=key)


def _update_pins(
    items: list[tuple[str, str]],
    definition: list[dict[str, Any]],
    style: int,
    order: list[int],
    rules: list[str],
) -> dict[int, tuple[float, set[str]]]:
    """Return the place in GetPins and the possible alternates of each library pin of
    the body style, by its index in the definition (SCH_SYMBOL::UpdatePins two
    times, _component_pins).

    Args:
        order: The library pins in the order of GetPins (_library_order).
        rules: The possible SetAlt rules (_SET_ALT_RULES).
    """

    def set_alt(alts: set[str], pin: dict[str, Any]) -> set[str]:
        return {_set_alt(alt, pin, rule) for alt in alts for rule in rules}

    alternates = {number: alt for number, alt in items if alt}
    places: dict[str, list[int]] = {}  # The places of the items of each number.
    for index, (number, _alt) in enumerate(items):
        places.setdefault(number, []).append(index)
    left = Counter({number: len(found) for number, found in places.items()})
    first: dict[int, tuple[float, set[str]]] = {}
    no_match = []
    for index in order:
        pin = definition[index]
        if pin["style"] and style and pin["style"] != style:
            continue
        number = pin["file_number"]
        if left[number] > 0:
            # The items of a number go to its library pins: which item a pin gets is
            # the order of memory, thus the pins get the places of the items in order.
            place = places[number][len(places[number]) - left[number]]
            left[number] -= 1
            first[index] = (place, set_alt({alternates.get(number, "")}, pin))
        else:
            no_match.append(index)
    # The (pin ...) items with no library pin: which items of a number are left is
    # not known, thus each item of a number with a left item can be one.
    spare = sum(left.values())
    candidates = {alt for number, alt in items if left[number] > 0}
    # One left item and one pin: the pin takes the place of the item (when the
    # number of the item has no other item, the place is known).
    single = [i for i, (number, _alt) in enumerate(items) if left[number] > 0]
    reused_place = single[0] if spare == 1 and len(no_match) == 1 and len(single) == 1 else None
    for position, index in enumerate(no_match):
        pin = definition[index]
        if pin["file_number"] in alternates:
            alts = set_alt({alternates[pin["file_number"]]}, pin)
        else:
            # A reused (pin ...) keeps its alternate with no SetAlt, a new pin has none.
            alts = set(candidates) or {""}
            if spare < len(no_match):
                alts.add("")
        if reused_place is not None:
            place: float = reused_place
        else:
            place = len(items) + (0 if position < spare else 1)
        first[index] = (place, alts)
    # The second UpdatePins: each pin has the number of its library pin. The last
    # alternate of a number (in the order of the pins) goes through SetAlt to each
    # pin of the number; a number with no alternate gives no alternate. The order of
    # pins with other alternates is not modelled: each one can be the last.
    by_number: dict[str, list[set[str]]] = {}
    for index, (_place, alts) in first.items():
        by_number.setdefault(definition[index]["file_number"], []).append(alts)
    result = {}
    for index, (place, _alts) in first.items():
        sets = by_number[definition[index]["file_number"]]
        outcomes = {alt for alts in sets for alt in alts if alt}
        if all("" in alts for alts in sets):
            outcomes.add("")  # Each pin can have no alternate: the map has no number.
        result[index] = (place, set_alt(outcomes, definition[index]) if outcomes else {""})
    return result


def _component_pins(
    symbol: Node,
    definition: list[dict[str, Any]],
    kicad: str | None,
    orders: dict[int, list[int]] | None = None,
) -> list[dict[str, Any]]:
    """Return the pins that the netlist gives a placed symbol, as KiCad makes them.

    The tools with a netlist take the names and the types of the netlist
    (NetlistParser): this model is for the pins of a symbol with no netlist.

    SCH_SYMBOL::UpdatePins (KiCad 8, 9 and 10, at the load of the file) takes
    each library pin of the body style of the symbol, in the order of the library
    (_library_order), and matches it to a (pin "N") of the symbol by the number of
    the file (GetNumber; the constructor of the instance pin keeps spaces). A
    library pin with no match gets an other (pin ...) of the symbol, or a new pin at
    the end; the (pin ...) items with no library pin go away. The (alternate "X")
    of a number applies to each library pin with that number (a map: the last
    (pin ...) of the number wins), through SetAlt (_set_alt). A library pin with no
    match and no alternate of its number keeps the alternate of the (pin ...) that
    it gets, with no SetAlt: the order is the order of the addresses in memory, thus
    each possible (pin ...) item counts. A second UpdatePins (measured: KiCad 8.0.9
    keeps such an alternate, 9.0.9 and 10.0.6 remove one that the library pin does
    not have) gives the last alternate of each number to each pin of the number,
    through SetAlt.

    SCH_SYMBOL::GetPins gives the pins of unit 0 and of the unit of the symbol, in
    the order of the pins of the symbol: the places of the (pin ...) items of the
    file, then the new pins. KiCad takes the library pins with no match in the order
    of their addresses (std::set; usually the order of the parse): here they are in
    the sort order, thus the place of a reused or new pin among them is only known
    when one pin and one item are left. Pins that the sort of KiCad gives as equal
    (for example "1" and "01" in KiCad 10) have no defined order (std::sort).

    Args:
        symbol: The (symbol ...) of the placed symbol.
        definition: The pins of its definition (_definition_pins).
        kicad: The version of the KiCad that reads the file, or None when it is
            not known: an alternate that differs between the rules of KiCad 8, 9
            and 10 is then not known, and the order is the order of KiCad 10.
        orders: A cache of _library_order for this definition, by major version.

    Returns:
        For each pin: "number", "name" (the alternate, else the name of the library
        pin), "base_name" (the name of the library pin), "electrical_type" (the type
        of the alternate, else of the library pin) and "alternate". A pin whose
        alternate is not known has the alternate None, and an empty name and type.
    """
    rules = _kicad_rules(kicad)
    orders = {} if orders is None else orders
    unit = symbol_unit(symbol)
    style = body_style(symbol)
    items = []  # (number of the file, alternate) of each (pin ...), in the file order.
    for item in symbol.children("pin"):
        alternate = item.first("alternate")
        items.append((item.value(0, ""), alternate.value(0, "") if alternate else ""))
    runs = []
    for major, set_rules in [rules] if rules else [(8, None), (9, None), (10, None)]:
        if major not in orders:
            orders[major] = _library_order(definition, major)
        rule_list = [set_rules] if set_rules else list(_SET_ALT_RULES)
        runs.append(_update_pins(items, definition, style, orders[major], rule_list))
    pins = []
    for index, _entry in sorted(runs[-1].items(), key=lambda entry: entry[1][0]):
        pin = definition[index]
        if unit and pin["unit"] and pin["unit"] != unit:
            continue
        found = set().union(*(run[index][1] for run in runs))
        alt = found.pop() if len(found) == 1 else None
        if alt == "~" and rules is None:
            alt = None  # KiCad 8 shows an empty name, KiCad 9 and 10 show "~".
        if alt is None:
            name, kind = "", ""
        elif alt:
            # KiCad 8 shows "~" as an empty name (GetShownName).
            name = "" if alt == "~" and rules and rules[0] == 8 else alt
            kind = pin["alternates"].get(alt, _UNKNOWN_ALTERNATE_TYPE)
        else:
            name, kind = pin["name"], pin["electrical_type"]
        pins.append({
            "number": pin["number"],
            "name": name,
            "base_name": pin["name"],
            "electrical_type": kind,
            "alternate": alt,
        })
    return pins


def _pins_with_no_definition(symbol: Node, kicad: str | None) -> list[dict[str, Any]]:
    """Return the pins of a placed symbol whose definition is not in lib_symbols.

    UpdatePins stops before the match (no m_part): the pins are the (pin ...) items
    of the file, with no library pin. KiCad 8 shows the name "??" (or the alternate)
    and the type no_connect (PT_NC); KiCad 9 and 10 show an empty name (or the
    alternate) and the type unspecified. GetShownNumber of KiCad 8 and 9 gives "~"
    as an empty number. With no known KiCad version, a name, a type or a number that
    differs between them is not known (the alternate None).
    """
    rules = _kicad_rules(kicad)
    pins = []
    for item in symbol.children("pin"):
        number = item.value(0, "")
        alternate = item.first("alternate")
        alt = alternate.value(0, "") if alternate else ""
        if rules is None:
            pins.append({
                "number": "" if number == "~" else number,
                "name": "",
                "base_name": "",
                "electrical_type": "",
                "alternate": None,
            })
            continue
        major = rules[0]
        name = alt or ("??" if major == 8 else "")
        pins.append({
            "number": "" if number == "~" and major < 10 else number,
            "name": "" if name == "~" and major == 8 else name,
            "base_name": "",
            "electrical_type": "no_connect" if major == 8 else "unspecified",
            "alternate": alt,
        })
    return pins


def unknown_alternate_note(components: list["SchematicComponent"]) -> str:
    """Return the warning about the pins whose alternate is not known, or "".

    _component_pins gives such a pin an empty name and type: KiCad gives it the
    alternate of a (pin ...) item by the order of memory, or the rules of SetAlt of
    the KiCad version (of the netlist) are not known.
    """
    pins = [
        f"{component.reference}.{pin.get('number', '')}"
        for component in components
        for pin in component.pins
        if "alternate" in pin and pin["alternate"] is None
    ]
    if not pins:
        return ""
    return (
        f"⚠️ The name and the electrical type of these pins are not known, thus they are "
        f"empty: {', '.join(pins)}. KiCad gives such a pin the alternate of a (pin ...) "
        "item of the file in the order of the memory, or the server does not know the "
        "rules of the KiCad version (for alternates, or for a symbol with no definition)."
    )


def _position(item: Node) -> tuple[float, float]:
    """Return the (at x y) position of an item, or (0, 0)."""
    at = item.first("at")
    return (at.number(0, 0.0), at.number(1, 0.0)) if at else (0.0, 0.0)


class SchematicParser:
    """Parser for KiCad schematic files (.kicad_sch)."""

    def __init__(self, file_path: str, kicad: str | None = None) -> None:
        """Initialize parser with schematic file.

        Args:
            file_path: Path to .kicad_sch file
            kicad: The version of the KiCad that reads the file: the KiCad of the
                netlist that the caller uses with the pins (NetlistParser.kicad_version).
                None: not known (_component_pins).

        Raises:
            FileNotFoundError: If file doesn't exist
            InputError: If file is not a .kicad_sch file
        """
        self.file_path = validate_kicad_file(file_path, ".kicad_sch")
        self._data: dict[str, Any] | None = None
        self._tree: Node | None = None
        self._items: dict[str | None, list[Node]] = {}
        # The pins of each symbol definition, by its key in lib_symbols.
        self._lib_pins: dict[str, list[dict[str, Any]]] | None = None
        self._power_keys: set[str] = set()
        self._power_net_keys: set[str] = set()
        self._rules: FieldRules | None = None
        self.kicad = kicad
        # The order of the library pins of each definition, by major version.
        self._lib_orders: dict[str, dict[int, list[int]]] = {}

    def _root(self) -> Node:
        """Return the parsed tree of the file.

        The parser reads the file one time. A parser made before a change of
        the file does not see the change.

        Raises:
            KicadFileError: The file version is not of KiCad 8 or later, or
                it is newer than the versions that this server knows.
        """
        if self._tree is None:
            tree = parse(read_as_kicad(self.file_path))
            try:
                self._rules = rules_for_file(tree)
            except KicadFileError as e:
                raise KicadFileError(f"{self.file_path}: {e}") from e
            self._tree = tree
            for item in tree.children():
                self._items.setdefault(item.head, []).append(item)
        return self._tree

    @property
    def field_rules(self) -> FieldRules:
        """The rules of the KiCad version that saved the file (see kicad_fields)."""
        self._root()
        assert self._rules is not None
        return self._rules

    def _rules_for_fields(self) -> FieldRules:
        """Return the field rules, and record them for the note of the tool call.

        Each read records the rules: a parser can be made outside of the tool
        call that reads its fields.
        """
        rules = self.field_rules
        record_field_rules(rules)
        return rules

    def _symbol_fields(self, symbol: Node) -> dict[str, str]:
        return symbol_fields(symbol, self._rules_for_fields())

    def _sheet_fields(self, sheet: Node) -> dict[str, str]:
        try:
            return sheet_fields(sheet, self._rules_for_fields())
        except KicadFileError as e:
            raise KicadFileError(f"{self.file_path}: {e}") from e

    def _top(self, head: str) -> list[Node]:
        """Return the top-level items of the sheet with the head."""
        self._root()
        return self._items.get(head, [])

    def _parse_file(self) -> dict[str, Any]:
        """Parse the schematic file.

        Returns:
            Parsed data structure

        Raises:
            SExprError: The file is not a valid S-expression.
        """
        if self._data is not None:
            # The data holds fields: the note of their rules applies again.
            self._rules_for_fields()
            return self._data

        root = self._root()
        # Parse lib_symbols first so _parse_components can use it
        self._read_lib_symbols()

        self._data = {
            "path": str(self.file_path),
            "title_block": self._parse_title_block(root),
            "components": self._parse_components(),
            "nets": self._parse_nets(),
            "sheets": self._parse_sheets(),
        }

        return self._data

    def _read_lib_symbols(self) -> None:
        """Read the symbol definitions of the sheet one time.

        A power symbol (power, power global or power local in its definition)
        is not a component. It gives its Value as a net name only when it has
        a pin of the type power_in (SCH_PIN::IsGlobalPower and IsLocalPower of
        KiCad). It can come from any library. PWR_FLAG has a power_out pin
        and names no net.
        """
        if self._lib_pins is not None:
            return
        self._lib_pins = {}
        for lib_symbols in self._top("lib_symbols"):
            for definition in lib_symbols.children("symbol"):
                key = definition.value(0, "")
                try:
                    pins = _definition_pins(
                        definition, Path(self.file_path), self.field_rules.file_version
                    )
                except KicadFileError as e:
                    raise KicadFileError(f"{self.file_path}: {e}") from e
                self._lib_pins[key] = pins
                if definition.first("power") is not None:
                    self._power_keys.add(key)
                    if any(pin["electrical_type"] == "power_in" for pin in pins):
                        self._power_net_keys.add(key)

    def _parse_title_block(self, root: Node) -> dict[str, str]:
        """Parse title block from schematic."""
        title_block = {
            "title": "",
            "date": "",
            "rev": "",
            "company": "",
            "comment": "",
        }
        block = root.first("title_block")
        if block:
            for key in ("title", "date", "rev", "company"):
                item = block.first(key)
                if item:
                    title_block[key] = item.value(0, "")
            # (comment 1 "text"): the first comment.
            comment = block.first("comment")
            if comment:
                title_block["comment"] = comment.value(1, "")
        return title_block

    def _parse_components(self) -> list[dict[str, Any]]:
        """Parse the symbol instances of the sheet.

        A power symbol and a symbol with a reference that starts with "#"
        are not components.
        """
        components = []
        for symbol in self._top("symbol"):
            lib_id_node = symbol.first("lib_id")
            if lib_id_node is None or _lib_key(symbol) in self._power_keys:
                continue
            lib_id = lib_id_node.value(0, "")
            fields = self._symbol_fields(symbol)
            reference = fields.get("Reference", "")
            if not reference or reference.startswith("#"):
                continue
            value = fields.get("Value", "")
            footprint = fields.get("Footprint") or None
            x, y = _position(symbol)

            # The pins of the netlist: the library pins of the unit (UpdatePins), or the
            # (pin ...) items of a symbol with no definition.
            key = _lib_key(symbol)
            if key in self._lib_pins:
                orders = self._lib_orders.setdefault(key, {})
                pins = _component_pins(symbol, self._lib_pins[key], self.kicad, orders)
            else:
                pins = _pins_with_no_definition(symbol, self.kicad)

            flags = dict(KICAD_FLAG_DEFAULTS)
            for flag in KICAD_SYMBOL_FLAGS:
                item = symbol.first(flag)
                if item is not None and item.value(0) in ("yes", "no"):
                    flags[flag] = item.value(0) == "yes"

            properties = [
                {"key": "Reference", "value": reference},
                {"key": "Value", "value": value},
            ]
            if footprint:
                properties.append({"key": "Footprint", "value": footprint})

            components.append({
                "lib_id": lib_id,
                "reference": reference,
                "value": value,
                "properties": properties,
                "at": {"x": x, "y": y},
                "pins": pins,
                "flags": flags,
            })
        return components

    def _labels(self, *kinds: str) -> list[dict[str, Any]]:
        """Return the labels of the kinds, in the order of the file.

        The kinds are "global_label", "label" and "hierarchical_label".
        """
        items = sorted(
            (item for kind in kinds for item in self._top(kind)), key=lambda item: item.start
        )
        labels = []
        for item in items:
            name = _shown_text(item.value(0, ""))
            if name:
                labels.append({"name": name, "position": _position(item)})
        return labels

    def _power_symbols(self) -> list[dict[str, Any]]:
        """Return the power symbols that name a net.

        The net name is the shown text of the Value field, as in KiCad.
        """
        self._read_lib_symbols()
        power = []
        for symbol in self._top("symbol"):
            if _lib_key(symbol) in self._power_net_keys:
                name = _shown_text(self._symbol_fields(symbol).get("Value", ""))
                if name:
                    power.append({"name": name, "position": _position(symbol)})
        return power

    def _parse_nets(self) -> list[dict[str, Any]]:
        """Parse the net names of the labels and of the power symbols.

        A name is in the list one time. The kinds come in this order: global
        labels, local labels, hierarchical labels, power symbols.
        """
        nets: dict[str, dict[str, Any]] = {}
        groups = (
            ("global", self._labels("global_label")),
            ("local", self._labels("label")),
            ("hierarchical", self._labels("hierarchical_label")),
            ("power", self._power_symbols()),
        )
        for net_type, items in groups:
            for item in items:
                if item["name"] not in nets:
                    nets[item["name"]] = {
                        "name": item["name"],
                        "code": len(nets),
                        "type": net_type,
                        "position": item["position"],
                    }
        return list(nets.values())

    def _parse_sheets(self) -> list[dict[str, str]]:
        """Parse hierarchical sheets.

        The name and the file of a sheet are the fields that the KiCad version
        of the file uses (see kicad_fields.sheet_fields).
        """
        sheets = []
        for sheet in self._top("sheet"):
            fields = self._sheet_fields(sheet)
            name = fields.get("Sheetname")
            file = fields.get("Sheetfile")
            if name and file:
                sheets.append({"name": name, "file": file})
        return sheets

    def get_components(self, recursive: bool = False) -> list[SchematicComponent]:
        """Get all components from schematic.

        Args:
            recursive: Also include components from hierarchical sub-sheets,
                following Sheetfile references (each file one time, cycle-safe).
                Real designs keep most components in sub-sheets, so callers
                that cross-reference a full-hierarchy netlist must pass True.

        Returns:
            List of components
        """
        data = self._parse_file()
        components = [SchematicComponent.from_dict(c) for c in data["components"]]
        if recursive:
            # sheet_files() resolves the sheet files as KiCad does. A missing
            # or unparsable sub-sheet is skipped: a broken reference must not
            # wipe out the components of the parent. Each sheet is read with
            # the rules of its own file version. A sheet that this server cannot
            # read (KicadFileError) is an error: the list would not be complete.
            for sheet in sheet_files(self.file_path)[1:]:
                try:
                    sub_data = SchematicParser(str(sheet), self.kicad)._parse_file()
                except (InputError, OSError):
                    continue
                components.extend(
                    SchematicComponent.from_dict(c) for c in sub_data["components"]
                )
            components = _merge_duplicate_references(components)
        return components

    def get_nets(self) -> list[SchematicNet]:
        """Get all nets from schematic.

        Returns:
            List of nets
        """
        data = self._parse_file()
        return [SchematicNet.from_dict(n) for n in data["nets"]]

    def get_title_block(self) -> dict[str, str]:
        """Get title block information.

        Returns:
            Dictionary with title, date, rev, company, comment
        """
        data = self._parse_file()
        return data["title_block"]

    def get_sheets(self) -> list[dict[str, str]]:
        """Get hierarchical sheets.

        Returns:
            List of sheet information
        """
        data = self._parse_file()
        return data["sheets"]

    def get_component_by_reference(self, reference: str) -> SchematicComponent | None:
        """Get a component by its reference designator.

        Args:
            reference: Component reference (e.g., "R1", "U1")

        Returns:
            Component if found, None otherwise
        """
        for component in self.get_components():
            if component.reference == reference:
                return component
        return None

    def search_components(self, pattern: str) -> list[SchematicComponent]:
        """Search for components by pattern.

        Args:
            pattern: Search pattern (matches reference, value, or library_id)

        Returns:
            List of matching components
        """
        regex = re.compile(pattern, re.IGNORECASE)
        results = []

        for component in self.get_components():
            if (
                regex.search(component.reference)
                or regex.search(component.value)
                or regex.search(component.library_id)
            ):
                results.append(component)

        return results

    def get_component_connections(self, reference: str) -> dict[str, Any]:
        """Get all network connections for a component.

        Args:
            reference: Component reference (e.g., "R16")

        Returns:
            Dictionary with connection information:
            {
                "nets": ["net_name1", "net_name2"],
                "labels": ["label1", "label2"],
                "connected_components": ["comp1", "comp2"]
            }
        """
        comp = self.get_component_by_reference(reference)
        if not comp:
            return {"error": f"Component {reference} not found"}

        pins = [pin["number"] for pin in comp.pins]
        cx, cy = comp.position

        # Find all labels and global labels, then the hierarchical labels
        labels = []
        for label in self._labels("label", "global_label") + self._labels("hierarchical_label"):
            lx, ly = label["position"]
            dist = ((lx - cx)**2 + (ly - cy)**2)**0.5
            labels.append({"name": label["name"], "position": (lx, ly), "distance": dist})

        # Filter to nearby labels (within 20mm)
        nearby_labels = [label for label in labels if label["distance"] < 20]

        # Find nearby components (within 15mm)
        components = self.get_components()
        nearby_comps = []
        for c in components:
            if c.reference == reference:
                continue
            dist = ((c.position[0] - cx)**2 + (c.position[1] - cy)**2)**0.5
            if dist < 15:
                nearby_comps.append({
                    "reference": c.reference,
                    "value": c.value,
                    "distance": dist,
                    "position": c.position
                })

        return {
            "component": reference,
            "position": comp.position,
            "pins": pins,
            "nearby_labels": nearby_labels[:10],
            "nearby_components": nearby_comps[:10],
        }

    def trace_net(self, reference: str, pin_number: str | None = None) -> dict[str, Any]:
        """Trace network connections from a component pin.

        Args:
            reference: Component reference (e.g., "R16")
            pin_number: Optional pin number to trace (if None, trace all pins)

        Returns:
            Network trace information
        """
        connections = self.get_component_connections(reference)

        if "error" in connections:
            return connections

        # Analyze the nearby data to infer network connections
        inferred_nets = []

        # Check for hierarchical labels that indicate function
        for label in connections["nearby_labels"]:
            label_name = label["name"]
            if any(keyword in label_name.upper() for keyword in
                   ["I2C", "SCL", "SDA", "SMBUS", "PMIC", "GPIO", "EN", "INT"]):
                inferred_nets.append({
                    "name": label_name,
                    "type": "signal",
                    "distance": label["distance"]
                })

        return {
            "component": reference,
            "position": connections["position"],
            "inferred_connections": inferred_nets,
            "nearby_components": connections["nearby_components"][:5],
        }

    def build_wire_network(self) -> dict[tuple[float, float], list[tuple[float, float]]]:
        """Build a graph of wire connections.

        Returns:
            Dictionary mapping each point to its connected neighbors
        """
        network = {}

        # (wire (pts (xy x1 y1) (xy x2 y2)) ...)
        for wire in self._top("wire"):
            pts = wire.first("pts")
            points = pts.children("xy") if pts else []
            if len(points) < 2:
                continue
            p1 = (points[0].number(0, 0.0), points[0].number(1, 0.0))
            p2 = (points[1].number(0, 0.0), points[1].number(1, 0.0))

            if p1 not in network:
                network[p1] = []
            if p2 not in network:
                network[p2] = []

            network[p1].append(p2)
            network[p2].append(p1)

        # Find junctions and merge connections
        for junction in self._top("junction"):
            jx, jy = _position(junction)
            # For a junction, all wires meeting at this point should be connected
            # Find all wire endpoints at this position (with small tolerance)
            tolerance = 0.01  # 0.01mm tolerance
            connected_points = [p for p in network if
                               abs(p[0] - jx) < tolerance and abs(p[1] - jy) < tolerance]

            # Merge all connections at junction
            all_neighbors = set()
            for p in connected_points:
                all_neighbors.update(network[p])

            for p in connected_points:
                network[p] = list(all_neighbors)

        return network

    def trace_wire_network(self, reference: str, max_depth: int = 20) -> dict[str, Any]:
        """Trace wire connections from a component.

        Args:
            reference: Component reference (e.g., "R16")
            max_depth: Maximum connection depth to trace

        Returns:
            Dictionary with traced connections and labels
        """
        # Get component position
        comp = self.get_component_by_reference(reference)
        if not comp:
            return {"error": f"Component {reference} not found"}

        cx, cy = comp.position

        # Build wire network
        network = self.build_wire_network()

        # Find the hierarchical labels and the global labels
        h_labels = self._labels("hierarchical_label")
        g_labels = self._labels("global_label")

        all_labels = h_labels + g_labels

        # Start from component position and trace
        max_tolerance = 20.0  # 20mm max tolerance (for components with pin offset)
        start_point = None
        min_dist = float('inf')

        # Find the nearest wire endpoint to component
        for point in network:
            dist = ((point[0] - cx)**2 + (point[1] - cy)**2)**0.5
            if dist < min_dist:
                min_dist = dist
                start_point = point

        # Only proceed if the nearest point is within tolerance
        if min_dist > max_tolerance:
            return {
                "component": reference,
                "position": comp.position,
                "connected_labels": [],
                "trace_path": [],
                "error": f"No wire found within {max_tolerance}mm (nearest: {min_dist:.2f}mm)",
            }

        # BFS trace through network
        visited = set()
        queue = [start_point]
        trace_path = []
        connected_labels = []

        while queue and len(visited) < max_depth:
            point = queue.pop(0)
            if point in visited:
                continue
            visited.add(point)

            trace_path.append(point)

            # Check if this point is near a label
            label_tolerance = 5.0  # 5mm tolerance for label matching
            for label in all_labels:
                lx, ly = label["position"]
                dist = ((point[0] - lx)**2 + (point[1] - ly)**2)**0.5
                if dist < label_tolerance and not any(lbl["name"] == label["name"] for lbl in connected_labels):
                    connected_labels.append({
                        "name": label["name"],
                        "position": label["position"],
                        "distance": dist,
                    })

            # Add neighbors to queue
            if point in network:
                for neighbor in network[point]:
                    if neighbor not in visited:
                        queue.append(neighbor)

        # Find nearby power symbols
        power_tolerance = 15.0  # 15mm tolerance for power symbols
        for power in self._power_symbols():
            power_name = power["name"]
            px, py = power["position"]
            dist = ((px - cx)**2 + (py - cy)**2)**0.5
            if dist < power_tolerance:
                connected_labels.append({
                    "name": f"POWER:{power_name}",
                    "position": (px, py),
                    "distance": dist,
                })

        return {
            "component": reference,
            "position": comp.position,
            "connected_labels": connected_labels,
            "trace_path": trace_path,
        }
