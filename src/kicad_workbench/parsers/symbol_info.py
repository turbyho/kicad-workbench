"""The content of a library symbol, as KiCad reads it and places it.

symbol_info reads a symbol and its parents (lib_symbols.symbol_chain) with the
rules of ``SCH_IO_KICAD_SEXPR_PARSER::parseLibSymbol`` and
``LIB_SYMBOL::Flatten`` (KiCad 8.0.9, 9.0.9 and 10.0.6):

- A derived symbol gets the fields of its chain and the flags of its parent
  (lib_symbols.flatten). All other items come from the root symbol: the units
  and their names, the unit lock, the body styles, the power flag, the pins and
  the pin settings.
- A pin outside a sub-symbol is in unit 1 with body style 1. A sub-symbol
  "<name>_<unit>_<body style>" gives the unit and the body style of its pins;
  0 is all units or all body styles. The number of units is the highest unit.
- In a file before version 20250318, "~" is an empty pin name or pin number.
  KiCad replaces each space of a pin number with "_".
- From version 20250827, (body_styles demorgan) or (body_styles "<name>" ...)
  declares the body styles. In an older file with no declaration, a symbol
  has the De Morgan body styles when a drawing has a body style after 1.
  KiCad removes the drawings of a body style after the last one.
- The positions are in the coordinates of the library file: the Y axis points
  up. The position of a pin is the end that connects.
- The pins are sorted as SCH_PIN::compare of KiCad 10 sorts them: unit, body
  style, number (numbers in it as numbers), position.
"""

from dataclasses import dataclass
from functools import cmp_to_key

from ..errors import InputError
from .c_numbers import atoi
from .kicad_fields import SYMBOL_FIELD_NAMES
from .kicad_versions import DECLARED_BODY_STYLES, KicadFileError, file_text
from .lib_symbols import (
    LibrarySymbol,
    flattened_fields,
    footprint_filters,
    pin_number,
)
from .sch_geometry import iu
from .sexpr import Atom, Node
from .sub_symbols import sub_symbols

# The electrical types of a pin. KiCad reads "unconnected" as "no_connect".
PIN_TYPES = (
    "input",
    "output",
    "bidirectional",
    "tri_state",
    "passive",
    "unspecified",
    "power_in",
    "power_out",
    "open_collector",
    "open_emitter",
    "no_connect",
    "free",
)
PIN_SHAPES = (
    "line",
    "inverted",
    "clock",
    "inverted_clock",
    "input_low",
    "clock_low",
    "output_low",
    "edge_clock_high",
    "non_logic",
)
_ORIENTATIONS = {0: "right", 90: "up", 180: "left", 270: "down"}
# The orders of the enums ELECTRICAL_PINTYPE and PIN_ORIENTATION (GRAPHIC_PINSHAPE
# has the order of PIN_SHAPES).
_TYPE_ORDER = (
    "input",
    "output",
    "bidirectional",
    "tri_state",
    "passive",
    "free",
    "unspecified",
    "power_in",
    "power_out",
    "open_collector",
    "open_emitter",
    "no_connect",
)
_ORIENTATION_ORDER = ("right", "left", "up", "down")
_TYPE_RANK = {name: rank for rank, name in enumerate(_TYPE_ORDER)}
_SHAPE_RANK = {name: rank for rank, name in enumerate(PIN_SHAPES)}
_ORIENTATION_RANK = {name: rank for rank, name in enumerate(_ORIENTATION_ORDER)}
# The drawings of a symbol (ParseSymbolDrawItem).
_DRAW_ITEMS = ("arc", "bezier", "circle", "pin", "polyline", "rectangle", "text", "text_box")
# The defaults of LIB_SYMBOL and SCH_PIN: 100 mil and 20 mil.
_PIN_LENGTH = 25400
_PIN_NAME_OFFSET = 5080


@dataclass(frozen=True)
class PinAlternate:
    """An alternate function of a pin."""

    name: str
    type: str
    shape: str


@dataclass(frozen=True)
class SymbolPin:
    """A pin of a library symbol."""

    number: str
    name: str
    type: str
    shape: str
    unit: int  # 0: all units.
    body_style: int  # 0: all body styles.
    x: int  # IU, with the Y axis of the library (up).
    y: int
    orientation: str  # "right", "up", "left" or "down".
    length: int  # IU
    hidden: bool
    alternates: tuple[PinAlternate, ...]


@dataclass(frozen=True)
class SymbolInfo:
    """The content of a library symbol, flattened as KiCad places it."""

    # (name, value): the mandatory fields in the order of their IDs, then the
    # user fields in the order of the file.
    fields: list[tuple[str, str]]
    keywords: str
    footprint_filters: list[str]
    power: str  # "", "global" or "local".
    unit_count: int
    unit_names: dict[int, str]  # The names of the units 1 to unit_count.
    units_locked: bool
    # "Standard" and "Alternate" for De Morgan, else the names of the
    # declared body styles. Empty: one body style.
    body_styles: list[str]
    in_bom: bool
    on_board: bool
    in_pos_files: bool
    exclude_from_sim: bool
    pin_names_shown: bool
    pin_name_offset: int  # IU
    pin_numbers_shown: bool
    duplicate_pins_are_jumpers: bool
    jumper_pin_groups: list[list[str]]
    pins: list[SymbolPin]


def _error(symbol: LibrarySymbol, text: str) -> KicadFileError:
    return KicadFileError(
        f'The symbol "{symbol.name}" of {symbol.file} {text}. KiCad cannot load the library.'
    )


def _bool(symbol: LibrarySymbol, node: Node) -> bool:
    """Read the yes or no of a list (parseBool)."""
    value = node.value(0)
    if value not in ("yes", "no"):
        raise _error(symbol, f"has ({node.head} {value or ''}), not yes or no")
    return value == "yes"


def _shown(root: LibrarySymbol, node: Node, shown: bool) -> bool:
    """Read the hide of (pin_names ...) or (pin_numbers ...) (parsePinNames).

    A bare token hide or (hide yes) hides the pin names or numbers, (hide no)
    shows them. KiCad writes the bare token before version 20241004, and reads
    it in each version.
    """
    for item in node.items[1:]:
        if isinstance(item, Atom):
            if not item.quoted and item.text == "hide":
                shown = False
        elif item.head == "hide":
            shown = not _bool(root, item)
    return shown


def letter_reference(unit: int) -> str:
    """Return the letters of a unit: 1 is "A", 26 is "Z", 27 is "AA" (LetterSubReference)."""
    suffix = ""
    while True:
        u = (unit - 1) % 26
        suffix = chr(ord("A") + u) + suffix
        unit = (unit - u) // 26
        if unit <= 0:
            return suffix


def _body_styles(root: LibrarySymbol, groups: list[tuple[int, int, Node]]) -> list[str]:
    demorgan = False
    names: list[str] = []
    for node in root.node.children("body_styles"):
        declared = []
        for item in node.items[1:]:
            if isinstance(item, Atom) and not item.quoted and item.text == "demorgan":
                demorgan = True
            elif isinstance(item, Atom):
                declared.append(item.value)
            else:
                raise _error(root, "has a list in (body_styles ...)")
        if declared:
            names = declared
    if (
        not demorgan
        and len(names) <= 1
        and (root.version is None or root.version < DECLARED_BODY_STYLES)
    ):
        # HasLegacyAlternateBodyStyle: a drawing with a body style after 1.
        demorgan = any(
            style > 1 and any(item.head in _DRAW_ITEMS for item in node.children())
            for _unit, style, node in groups
        )
    if demorgan:
        return ["Standard", "Alternate"]
    return names if len(names) > 1 else []


def _text(root: LibrarySymbol, node: Node | None) -> str:
    """Read the text of a (name ...) or (number ...) of a pin (file_text)."""
    return file_text(node.value(0, "") if node is not None else "", root.version)


def _pin_type(root: LibrarySymbol, text: str | None) -> str:
    if text == "unconnected":
        return "no_connect"
    if text not in PIN_TYPES:
        raise _error(root, f"has a pin with the electrical type {text!r}")
    return text


def _pin_shape(root: LibrarySymbol, text: str | None) -> str:
    if text not in PIN_SHAPES:
        raise _error(root, f"has a pin with the graphic shape {text!r}")
    return text


def _number(root: LibrarySymbol, node: Node, index: int) -> float:
    try:
        value = node.number(index)
    except InputError:
        value = None
    if value is None:
        raise _error(root, f"has ({node.head} ...) with an item that is not a number")
    return value


def _pin(root: LibrarySymbol, pin: Node, unit: int, body_style: int) -> SymbolPin:
    """Read a pin as parseSymbolPin reads it."""
    x = y = 0
    orientation = "right"
    length = _PIN_LENGTH
    hidden = False
    name = number = ""
    alternates: dict[str, PinAlternate] = {}
    for item in pin.items[3:]:
        if isinstance(item, Atom):
            if not item.quoted and item.text == "hide":
                hidden = True
            continue
        if item.head == "at":
            x, y = iu(_number(root, item, 0)), iu(_number(root, item, 1))
            # NeedNUMBER, then parseInt: (int)strtol of the number.
            _number(root, item, 2)
            angle = atoi(item.value(2) or "")
            orientation = _ORIENTATIONS.get(angle, "")
            if not orientation:
                raise _error(
                    root, f"has a pin with the angle {item.value(2)}, not 0, 90, 180 or 270"
                )
        elif item.head == "length":
            length = iu(_number(root, item, 0))
        elif item.head == "hide":
            hidden = _bool(root, item)
        elif item.head == "name":
            name = _text(root, item)
        elif item.head == "number":
            number = pin_number(_text(root, item))
        elif item.head == "alternate":
            alternate = PinAlternate(
                item.value(0, ""),
                _pin_type(root, item.value(1)),
                _pin_shape(root, item.value(2)),
            )
            alternates[alternate.name] = alternate
    return SymbolPin(
        number,
        name,
        _pin_type(root, pin.value(0)),
        _pin_shape(root, pin.value(1)),
        unit,
        body_style,
        x,
        y,
        orientation,
        length,
        hidden,
        # KiCad keeps the alternates in a map, sorted by name.
        tuple(alternates[key] for key in sorted(alternates)),
    )


def _number_key(digits: str) -> tuple[int, str]:
    """The order of the numbers of ASCII digits, as their values, with no int():
    int() refuses a text of more than 4300 digits."""
    significant = digits.lstrip("0")
    return len(significant), significant


def str_num_cmp(first: str, second: str) -> int:
    """Compare two texts with the numbers in them as numbers (StrNumCmp of KiCad).

    Returns:
        -1, 0 or 1. Two texts that are not equal can give 0, for example "01"
        and "1".
    """
    i = j = 0
    while i < len(first) and j < len(second):
        c1, c2 = first[i], second[j]
        if c1.isascii() and c1.isdigit() and c2.isascii() and c2.isdigit():
            start = i
            while i < len(first) and first[i].isascii() and first[i].isdigit():
                i += 1
            number1 = _number_key(first[start:i])
            start = j
            while j < len(second) and second[j].isascii() and second[j].isdigit():
                j += 1
            number2 = _number_key(second[start:j])
            if number1 != number2:
                return -1 if number1 < number2 else 1
            # KiCad then compares the characters after the numbers, and
            # steps over them.
            c1 = first[i] if i < len(first) else "\0"
            c2 = second[j] if j < len(second) else "\0"
        if c1 != c2:
            return -1 if c1 < c2 else 1
        i, j = min(i + 1, len(first)), min(j + 1, len(second))
    if i == len(first) and j != len(second):
        return -1
    if i != len(first) and j == len(second):
        return 1
    return 0


def _pin_cmp(a: SymbolPin, b: SymbolPin) -> int:
    """Compare two pins of a LIB_SYMBOL as SCH_PIN::compare of KiCad 10 does.

    The tool shows the pins in this order for each KiCad version. KiCad 8
    (LIB_PIN::compare) and KiCad 9 compare in a different order: KiCad 8 the
    number as text and then the name, KiCad 9 the position before the number.
    The text sizes of the name and the number come next in KiCad 10; this
    server does not read them.
    """
    if a.unit != b.unit:
        return a.unit - b.unit
    if a.body_style != b.body_style:
        return a.body_style - b.body_style
    if a.number != b.number:
        return str_num_cmp(a.number, b.number)
    keys = (
        # KiCad has the Y axis down.
        (a.x, b.x),
        (-a.y, -b.y),
        (a.length, b.length),
        (_ORIENTATION_RANK[a.orientation], _ORIENTATION_RANK[b.orientation]),
        (_SHAPE_RANK[a.shape], _SHAPE_RANK[b.shape]),
        (_TYPE_RANK[a.type], _TYPE_RANK[b.type]),
        (a.hidden, b.hidden),
    )
    for first, second in keys:
        if first != second:
            return -1 if first < second else 1
    return 0


def _jumper_groups(root: LibrarySymbol, node: Node) -> list[list[str]]:
    """Read (jumper_pin_groups ("1" "2") ...): each group holds quoted pin numbers."""
    groups = []
    for item in node.items[1:]:
        names = item.items if isinstance(item, Node) else None
        if names is None or not all(isinstance(n, Atom) and n.quoted for n in names):
            raise _error(root, "has a jumper pin group that is not a list of quoted pin numbers")
        groups.append([name.value for name in names])
    return groups


def symbol_info(chain: list[LibrarySymbol], kicad: int) -> SymbolInfo:
    """Return the content of a library symbol, flattened as KiCad places it.

    Args:
        chain: The symbol and its parents (symbol_chain).
        kicad: 9 for the rules of KiCad 8 and 9, 10 for KiCad 10 and later.

    Raises:
        KicadFileError: KiCad cannot load the library: an item has a value
            that KiCad does not read.
    """
    root = chain[-1]
    # The items of the root are in unit 1 with body style 1.
    groups = [(1, 1, root.node), *sub_symbols(root.node)]
    body_styles = _body_styles(root, groups)
    style_count = max(1, len(body_styles))

    fields = flattened_fields(chain, kicad)
    values = dict(fields)
    # A LIB_SYMBOL always has the mandatory fields, empty when the file has none.
    shown_fields = [(name, values.get(name, "")) for name in SYMBOL_FIELD_NAMES] + [
        (key[len("user:") :], value) for key, value in fields if key.startswith("user:")
    ]
    filters = footprint_filters(values.get("ki_fp_filters", ""))

    power = ""
    for node in root.node.children("power"):
        power = "local" if node.value(0) == "local" else "global"

    # The flags of the parent of a derived symbol, else of the symbol.
    flags_owner = chain[1] if len(chain) > 1 else chain[0]
    flags = {"in_bom": True, "on_board": True, "in_pos_files": True, "exclude_from_sim": False}
    for head in flags:
        for node in flags_owner.node.children(head):
            flags[head] = _bool(flags_owner, node)

    pin_names_shown = pin_numbers_shown = True
    pin_name_offset = _PIN_NAME_OFFSET
    for node in root.node.children("pin_names"):
        pin_names_shown = _shown(root, node, pin_names_shown)
        if (offset := node.first("offset")) is not None:
            pin_name_offset = iu(_number(root, offset, 0))
    for node in root.node.children("pin_numbers"):
        pin_numbers_shown = _shown(root, node, pin_numbers_shown)

    jumpers = False
    for node in root.node.children("duplicate_pin_numbers_are_jumpers"):
        jumpers = _bool(root, node)
    jumper_groups: list[list[str]] = []
    for node in root.node.children("jumper_pin_groups"):
        jumper_groups += _jumper_groups(root, node)

    unit_count = max(unit for unit, _style, _node in groups)
    names: dict[int, str] = {}
    pins = []
    for unit, style, node in groups:
        for item in node.children():
            if item.head == "unit_name" and node is not root.node and item.value(0) is not None:
                names[unit] = item.value(0)
            elif item.head == "pin" and style <= style_count:
                pins.append(_pin(root, item, unit, style))

    return SymbolInfo(
        fields=shown_fields,
        keywords=values.get("ki_keywords", ""),
        footprint_filters=filters,
        power=power,
        unit_count=unit_count,
        # A unit with no (unit_name ...) shows its letters (GetUnitDisplayName).
        unit_names={
            u: names[u] if u in names else letter_reference(u) for u in range(1, unit_count + 1)
        },
        units_locked="ki_locked" in values,
        body_styles=body_styles,
        in_bom=flags["in_bom"],
        on_board=flags["on_board"],
        in_pos_files=flags["in_pos_files"],
        exclude_from_sim=flags["exclude_from_sim"],
        pin_names_shown=pin_names_shown,
        pin_name_offset=pin_name_offset,
        pin_numbers_shown=pin_numbers_shown,
        duplicate_pins_are_jumpers=jumpers,
        jumper_pin_groups=jumper_groups,
        # The order of SCH_PIN::compare of KiCad 10. A stable sort keeps the
        # order of the file for two pins that it sees as equal.
        pins=sorted(pins, key=cmp_to_key(_pin_cmp)),
    )
