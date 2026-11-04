"""Symbols of a KiCad symbol library, as KiCad copies them into a schematic.

A schematic holds a copy of each symbol that it uses, in its (lib_symbols)
section. KiCad makes the copy with ``LIB_SYMBOL::Flatten``. The copy of a
derived symbol, (extends "<parent>"), is its root symbol with these changes
(KiCad 8.0.9, 9.0.9 and 10.0.6):

- The copy has the name of the derived symbol.
- From the root down to the derived symbol, a symbol replaces a mandatory
  field (Reference, Value, Footprint, Datasheet, Description) when its text
  is not empty. KiCad removes the blanks at the start and the end of the text
  of a mandatory field, and reads "~" as an empty text in a file before
  version 20250318. A user field replaces the user field with the same name, or
  KiCad adds it. The keywords (ki_keywords) and the footprint filters
  (ki_fp_filters) replace the old ones when they are not empty.
- The flags exclude_from_sim, in_bom, on_board and in_pos_files come from the
  parent of the derived symbol. KiCad does not use the flags of the derived
  symbol.
- KiCad adds the embedded files of the derived symbol to the files of the
  root. KiCad 9 also adds the files of each symbol between the two, KiCad 10
  does not. KiCad does not add a file with the name of a file that is there.
- All other items come from the root: the units, the pins, the drawings, the
  pin settings, the unit lock and the power flag.

The text of each item stays as it is in the library. Thus KiCad reads it in
the schematic as in the library, when kicad_versions.symbol_library_problem
finds no problem for each symbol of the chain.
"""

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from ..utils.kicad_text import read_as_kicad
from .bus_names import unescape_string
from .kicad_fields import SYMBOL_FIELD_NAMES
from .kicad_versions import EMPTY_TEXT_MARKER, KicadFileError, header_version
from .sexpr import Node, edited, parse, property_atoms, quote

_FLAGS = ("exclude_from_sim", "in_bom", "on_board", "in_pos_files")
# KiCad tries these suffixes for a user field with the name of an earlier
# field (parseProperty), and skips the field when all names are used.
_RENAMES = range(1, 10)
# The name of a sub-symbol: "<symbol name>_<unit>_<body style>".
_UNIT_SUFFIX = re.compile(r"(\d+)_(\d+)")
_SUB_SYMBOL = re.compile(r"_(\d+)_(\d+)\Z")
# The characters that wxString::Strip removes (wxSafeIsspace: ASCII isspace).
_BLANKS = " \t\n\v\f\r"


@dataclass(frozen=True, eq=False)
class LibrarySymbol:
    """A (symbol ...) of a symbol library file."""

    name: str  # The name as KiCad reads it (symbol_name).
    node: Node
    file: Path
    version: int | None  # The file version of the library file.
    # The other files of a directory library with a symbol of this name.
    other_files: tuple[Path, ...] = ()


def symbol_name(text: str) -> str:
    """Return the name of a library symbol as KiCad reads it.

    "{slash}" is "/", and a library nickname before ":" is not part of the
    name (LIB_ID::Parse).
    """
    return text.replace("{slash}", "/").split(":", 1)[-1]


def parent_name(extends: Node) -> str:
    """Return the parent name of an (extends ...) as KiCad reads it: "{slash}" is "/"."""
    return extends.value(0, "").replace("{slash}", "/")


def library_files(path: Path) -> list[Path]:
    """Return the files of a symbol library, sorted by name.

    A library is a .kicad_sym file, or a directory of .kicad_sym files
    (KiCad 10). KiCad reads the files of a directory in the order of the file
    system (wxDir), thus this order is only for a result that does not change.
    """
    if path.is_dir():
        return sorted(path.glob("*.kicad_sym"))
    return [path]


def files_stamp(files: list[Path]) -> list:
    """Return the name, time and size of each file. A change makes a cache old.

    Raises:
        OSError: A file cannot be read.
    """
    stamp = []
    for file in files:
        stat = file.stat()
        stamp.append([file.name, stat.st_mtime_ns, stat.st_size])
    return stamp


# The libraries that read_library used last, the last at the end:
# path -> (stamp, symbols).
_CACHE_SIZE = 8
_cache: dict[Path, tuple[list, dict[str, "LibrarySymbol"]]] = {}
_cache_lock = threading.Lock()


def read_library(path: Path) -> dict[str, LibrarySymbol]:
    """Read the symbols of a symbol library.

    In a file, a later symbol replaces an earlier symbol with the same name, as
    in KiCad. A cache keeps the last libraries until one of their files
    changes.

    Raises:
        OSError, KiCadEncodingError: A file cannot be read.
        SExprError: KiCad cannot read a file. KiCad then does not load the
            library, also when only one file of a directory library has an
            error (SCH_IO_KICAD_SEXPR_LIB_CACHE::Load).
    """
    files = library_files(path)
    stamp = files_stamp(files)
    with _cache_lock:
        cached = _cache.pop(path, None)
        if cached is not None and cached[0] == stamp:
            _cache[path] = cached
            return cached[1]
    found: dict[str, list[LibrarySymbol]] = {}
    for file in files:
        text = read_as_kicad(file)
        version = header_version(text, "kicad_symbol_lib")
        in_file: dict[str, LibrarySymbol] = {}
        for node in parse(text).children("symbol"):
            name = symbol_name(node.value(0, ""))
            in_file[name] = LibrarySymbol(name, node, file, version)
        for name, symbol in in_file.items():
            found.setdefault(name, []).append(symbol)
    symbols = {}
    for name, versions in found.items():
        last = versions[-1]
        others = tuple(symbol.file for symbol in versions[:-1])
        symbols[name] = LibrarySymbol(name, last.node, last.file, last.version, others)
    with _cache_lock:
        _cache[path] = (stamp, symbols)
        while len(_cache) > _CACHE_SIZE:
            del _cache[next(iter(_cache))]
    return symbols


def symbol_chain(symbols: dict[str, LibrarySymbol], name: str) -> list[LibrarySymbol]:
    """Return the symbol and its parents, from the symbol up to the root symbol.

    Raises:
        KeyError: The library has no symbol with the name.
        KicadFileError: A parent is not in the library, the symbol is derived
            from itself, or a symbol of the chain is in more than one file of a
            directory library.
    """
    chain = [symbols[name]]
    while True:
        symbol = chain[-1]
        if symbol.other_files:
            files = ", ".join(str(file) for file in (*symbol.other_files, symbol.file))
            raise KicadFileError(
                f'The symbol "{symbol.name}" is in more than one file of the directory '
                f"library: {files}. KiCad reads the files in the order of the file system, "
                "thus the symbol that KiCad uses is not known."
            )
        extends = symbol.node.first("extends")
        if extends is None:
            return chain
        parent = symbols.get(parent_name(extends))
        if parent is None:
            raise KicadFileError(
                f'The library has no parent symbol "{parent_name(extends)}" for the derived '
                f'symbol "{symbol.name}".'
            )
        if parent in chain:
            raise KicadFileError(
                f'The symbol "{name}" is derived from itself through "{parent.name}".'
            )
        chain.append(parent)


@dataclass
class _Field:
    """A field of a library symbol, as the parser of KiCad reads it.

    ``parts`` holds the texts of the (property ...) lists that make the
    field, in the order of the file. KiCad reads the same field from these
    texts in the schematic.
    """

    # A canonical mandatory name, "user:<name>", "ki_keywords", "ki_fp_filters"
    # or "ki_locked".
    key: str
    value: str
    parts: list[str]
    nodes: list[Node]  # The (property ...) lists of the parts.

    @property
    def empty(self) -> bool:
        if self.key == "ki_fp_filters":
            return not footprint_filters(self.value)
        return self.value == ""


def footprint_filters(value: str) -> list[str]:
    """Return the footprint filters of a ki_fp_filters value, as parseProperty reads them.

    KiCad splits the value at space, tab, CR and LF (wxTOKEN_STRTOK: no empty
    filter), and unescapes each filter.
    """
    return [unescape_string(text) for text in re.split(r"[ \t\r\n]+", value) if text]


def _text(node: Node) -> str:
    return node.source[node.start : node.end]


def _renamed(prop: Node, name: str) -> str:
    """Return the text of a (property ...) with a different name."""
    atom = property_atoms(prop)[0]
    source = prop.source
    return source[prop.start : atom.start] + quote(name) + source[atom.end : prop.end]


def _set(fields: list[_Field], field: _Field) -> None:
    """Replace the field with the key of the field, at its place, or add the field."""
    for index, old in enumerate(fields):
        if old.key == field.key:
            fields[index] = field
            return
    fields.append(field)


def _mandatory_name(name: str) -> str | None:
    """The parser of a library symbol finds a mandatory field by its name with
    no difference of case (parseProperty of KiCad 8, 9 and 10)."""
    for canonical in SYMBOL_FIELD_NAMES:
        if name.lower() == canonical.lower():
            return canonical
    return None


def _fields(symbol: LibrarySymbol, kicad: int) -> list[_Field]:
    """Return the fields of a library symbol, as parseProperty of KiCad reads them.

    Args:
        kicad: 9 for the rules of KiCad 8 and 9, 10 for KiCad 10 and later.

    Raises:
        KicadFileError: An (id N) is not a number. KiCad cannot load the
            library.
    """
    fields: list[_Field] = []
    for prop in symbol.node.children("property"):
        atoms = property_atoms(prop)
        if not atoms:
            continue
        name = atoms[0].value
        value = atoms[1].value if len(atoms) > 1 else ""
        if value == "~" and symbol.version is not None and symbol.version < EMPTY_TEXT_MARKER:
            value = ""
        mandatory = _mandatory_name(name)
        id_node = prop.first("id")
        if id_node is not None:
            try:
                field_id = int(id_node.value(0, ""))
            except ValueError:
                raise KicadFileError(
                    f'The field "{name}" of the symbol "{symbol.name}" has an ID that is '
                    "not a number."
                ) from None
            # KiCad 8 and 9: an ID after the mandatory IDs makes a user field.
            if kicad == 9 and field_id >= len(SYMBOL_FIELD_NAMES):
                mandatory = None
        if mandatory is not None or name == "ki_description":
            # SCH_FIELD::SetText removes the blanks at the start and the end of
            # the text of a mandatory field (wxString::Strip).
            value = value.strip(_BLANKS)
        if mandatory is not None:
            # A later mandatory field replaces the earlier one.
            _set(fields, _Field(mandatory, value, [_text(prop)], [prop]))
        elif name == "ki_description":
            # The description of KiCad 7 and earlier: KiCad changes only the text.
            old = next((f for f in fields if f.key == "Description"), None)
            if old is None:
                fields.append(_Field("Description", value, [_text(prop)], [prop]))
            else:
                old.value = value
                old.parts.append(_text(prop))
                old.nodes.append(prop)
        elif name in ("ki_keywords", "ki_fp_filters", "ki_locked"):
            _set(fields, _Field(name, value, [_text(prop)], [prop]))
        else:
            # The mandatory fields are always there, with their canonical names.
            used = set(SYMBOL_FIELD_NAMES) | {
                f.key[5:] for f in fields if f.key.startswith("user:")
            }
            new_name = name
            if name in used:
                new_name = next((f"{name}_{i}" for i in _RENAMES if f"{name}_{i}" not in used), "")
                if not new_name:
                    continue
            text = _text(prop) if new_name == name else _renamed(prop, new_name)
            fields.append(_Field(f"user:{new_name}", value, [text], [prop]))
    return fields


@dataclass(frozen=True)
class PlacedField:
    """A field that KiCad copies from the definition into a placed symbol."""

    name: str  # The canonical name of a mandatory field, or the name of a user field.
    # The (property ...) of the definition. None: a mandatory field with the
    # default values of LIB_SYMBOL (position 0 0, Reference and Value shown).
    node: Node | None
    # The text, when it is not the text of the node (the "ki_description" of KiCad 7).
    text: str | None = None


def placed_fields(definition: Node, kicad: int) -> list[PlacedField]:
    """Return the fields that KiCad copies into a placed symbol (SCH_SYMBOL::UpdateFields).

    The five mandatory fields come first, in the order of their IDs, then the
    user fields in the order of the definition. The keywords, the footprint
    filters and the unit lock are not fields.

    Args:
        definition: A (symbol ...) of a lib_symbols section (flatten).
        kicad: 9 for the rules of KiCad 8 and 9, 10 for KiCad 10 and later.

    Raises:
        KicadFileError: An (id N) is not a number.
    """
    symbol = LibrarySymbol(definition.value(0, ""), definition, Path(), None)
    fields = _fields(symbol, kicad)
    by_key = {field.key: field for field in fields}
    result = []
    for name in SYMBOL_FIELD_NAMES:
        field = by_key.get(name)
        if field is None:
            result.append(PlacedField(name, None))
            continue
        # "ki_description" changes only the text of the description field.
        nodes = [n for n in field.nodes if property_atoms(n)[0].value != "ki_description"]
        text_only = property_atoms(field.nodes[-1])[0].value == "ki_description"
        result.append(
            PlacedField(name, nodes[-1] if nodes else None, field.value if text_only else None)
        )
    for field in fields:
        if field.key.startswith("user:"):
            result.append(PlacedField(field.key[len("user:") :], field.nodes[-1]))
    return result


def unit_count(definition: Node) -> int:
    """Return the number of units of a symbol: the highest unit of its sub-symbols."""
    units = [1]
    for sub in definition.children("symbol"):
        match = _SUB_SYMBOL.search(sub.value(0, ""))
        if match:
            units.append(int(match.group(1)))
    return max(units)


def _chain_fields(chain: list[LibrarySymbol], kicad: int) -> list[_Field]:
    fields = _fields(chain[-1], kicad)
    for symbol in reversed(chain[:-1]):
        for field in _fields(symbol, kicad):
            if field.key == "ki_locked":
                continue  # The unit lock of the root applies.
            if field.key.startswith("user:"):
                # KiCad removes the old field and adds the new one at the end.
                fields = [old for old in fields if old.key != field.key]
                fields.append(field)
            elif not field.empty:
                _set(fields, field)
    return fields


def flattened_fields(chain: list[LibrarySymbol], kicad: int) -> list[tuple[str, str]]:
    """Return the fields of a symbol as KiCad flattens its chain: (key, value).

    The key is the canonical name of a mandatory field, "user:<name>",
    "ki_keywords", "ki_fp_filters" or "ki_locked". The value is the text that
    KiCad reads (also "" for the "~" of an old file).

    Args:
        chain: The symbol and its parents (symbol_chain).
        kicad: 9 for the rules of KiCad 8 and 9, 10 for KiCad 10 and later.

    Raises:
        KicadFileError: An (id N) is not a number.
    """
    return [(field.key, field.value) for field in _chain_fields(chain, kicad)]


def _last(node: Node, head: str) -> Node | None:
    """Return the last child list with the head: KiCad uses the last one."""
    found = node.children(head)
    return found[-1] if found else None


def _file_name(file: Node) -> str:
    name = file.first("name")
    return name.value(0, "") if name else ""


def _embedded_files(chain: list[LibrarySymbol], kicad: int) -> list[str]:
    """Return the texts of the (file ...) lists that KiCad adds to the root."""
    root_files = chain[-1].node.first("embedded_files")
    names = {_file_name(file) for file in root_files.children("file")} if root_files else set()
    # KiCad 9 adds the files of each symbol from the root down, KiCad 10 only
    # the files of the derived symbol.
    sources = list(reversed(chain[:-1])) if kicad == 9 else chain[:1]
    added = []
    for symbol in sources:
        files = symbol.node.first("embedded_files")
        for file in files.children("file") if files else []:
            name = _file_name(file)
            if name not in names:
                names.add(name)
                added.append(_text(file))
    return added


def _unit_suffix(symbol: LibrarySymbol, sub: Node) -> str:
    """Return the "<unit>_<body style>" of a sub-symbol "<name>_<unit>_<body style>".

    Raises:
        KicadFileError: KiCad cannot read the name (parseLibSymbol).
    """
    atoms = sub.atoms()
    sub_name = atoms[0].value.replace("{slash}", "/") if atoms else ""
    prefix = symbol.name + "_"
    suffix = sub_name[len(prefix) :] if sub_name.startswith(prefix) else ""
    if not _UNIT_SUFFIX.fullmatch(suffix):
        raise KicadFileError(
            f'The symbol "{symbol.name}" has a unit with the name "{sub_name}". '
            f'The name must be "{prefix}<unit>_<body style>".'
        )
    return suffix


def sub_symbols(symbol: LibrarySymbol) -> list[tuple[int, int, Node]]:
    """Return (unit, body style, sub-symbol) for each sub-symbol of a library symbol.

    Raises:
        KicadFileError: KiCad cannot read the name of a sub-symbol.
    """
    result = []
    for sub in symbol.node.children("symbol"):
        unit, style = _unit_suffix(symbol, sub).split("_")
        result.append((int(unit), int(style), sub))
    return result


def flatten(chain: list[LibrarySymbol], lib_id: str, kicad: int) -> str:
    """Return the text of a symbol for the lib_symbols section of a schematic.

    Args:
        chain: The symbol and its parents (symbol_chain).
        lib_id: The name in the schematic, "<library>:<symbol>".
        kicad: 9 for the rules of KiCad 8 and 9, 10 for KiCad 10 and later.

    Raises:
        KicadFileError: KiCad cannot read the name of a sub-symbol of the
            root, or an (id N) of a field.
    """
    root = chain[-1].node
    item_name = lib_id.split(":", 1)[1]
    # (start, end, text) edits of the text of the root symbol.
    edits: list[tuple[int, int, str]] = []

    name_atom = root.atoms()[0]
    edits.append((name_atom.start, name_atom.end, quote(lib_id)))
    for sub in root.children("symbol"):
        suffix = _unit_suffix(chain[-1], sub)
        atom = sub.atoms()[0]
        edits.append((atom.start, atom.end, quote(f"{item_name}_{suffix}")))

    # A symbol that is not derived keeps its fields. Else the fields, and the
    # flags of the parent when it is not the root, go to the place of the
    # first field of the root.
    removed: list[Node] = []
    added: list[str] = []
    if len(chain) > 1:
        removed = list(root.children("property"))
        added = [part for field in _chain_fields(chain, kicad) for part in field.parts]
    if len(chain) > 2:
        removed += [node for head in _FLAGS for node in root.children(head)]
        flags = (_last(chain[1].node, head) for head in _FLAGS)
        added = [_text(flag) for flag in flags if flag is not None] + added
    removed.sort(key=lambda node: node.start)
    insert = "\n" + "\n".join(added) + "\n"
    if removed:
        edits.append((removed[0].start, removed[0].end, insert))
        edits += [(node.start, node.end, "") for node in removed[1:]]
    elif added:
        subs = root.children("symbol")
        at = subs[0].start if subs else root.end - 1
        edits.append((at, at, insert))

    files = _embedded_files(chain, kicad)
    if files:
        root_files = root.first("embedded_files")
        if root_files is not None:
            at, text = root_files.end - 1, "\n" + "\n".join(files) + "\n"
        else:
            at, text = root.end - 1, "\n(embedded_files\n" + "\n".join(files) + "\n)\n"
        edits.append((at, at, text))

    return edited(root, edits)


def pin_number(text: str) -> str:
    """Return a pin number as KiCad 8, 9 and 10 read it from a file.

    SCH_PIN::SetNumber (LIB_PIN::SetNumber of KiCad 8) replaces each space
    with "_": a pin number has no spaces.
    """
    return text.replace(" ", "_")


@dataclass(frozen=True)
class LibraryPin:
    """A pin of a symbol definition."""

    number: str
    type: str  # The electrical type, for example "passive" or "no_connect".
    # The position of the end that connects, in mm, with the Y axis of the library (up).
    x: float
    y: float
    hidden: bool = False  # The token hide, or (hide yes) of KiCad 9 and later.


def hidden(node: Node) -> bool:
    """Return True if an item is hidden: the token hide, or (hide yes).

    The parsers of KiCad read (hide) and (hide yes) as hidden, (hide no) as
    shown (parseMaybeAbsentBool), and a bare token hide of an older file as
    hidden: for a pin (parsePin) and for a 3D model (parse3DModel).
    """
    result = False
    for item in node.items[1:]:
        if isinstance(item, Node):
            if item.head == "hide":
                result = item.value(0, "yes") == "yes"
        elif not item.quoted and item.text == "hide":
            result = True
    return result


def _groups(symbol: Node, body_style: int, units: set[int] | None) -> list[Node]:
    """Return the symbol and its sub-symbols that a placed symbol shows.

    A sub-symbol "<name>_<unit>_<body style>" shows for its unit and for unit 0
    (all units), with its body style or body style 0. The items outside a
    sub-symbol are in unit 1 with body style 1.
    """
    groups = [(1, 1, symbol)]
    for sub in symbol.children("symbol"):
        match = _SUB_SYMBOL.search(sub.value(0, ""))
        if match:
            groups.append((int(match.group(1)), int(match.group(2)), sub))
    return [
        group
        for unit, style, group in groups
        if style in (0, body_style) and (units is None or unit in (0, *units))
    ]


# The points of a rectangle and of an arc that are on the item.
_BODY_POINTS = {"rectangle": ("start", "end"), "arc": ("start", "mid", "end")}


def body_points(symbol: Node, body_style: int, units: set[int]) -> list[tuple[float, float]]:
    """Return points (mm, library Y up) that the box of the body graphics holds.

    The points are on the graphic items of the units and the body style: the
    corners of a polyline and of a rectangle, three points of an arc, the
    ends of a bezier curve and four points of a circle. KiCad uses the box of
    the body graphics, the visible pins and the visible fields as the box of a
    placed symbol (SCH_SYMBOL::GetBoundingBox), thus these points give a box in
    that box.

    Raises:
        ValueError: A position is not a number.
    """
    points = []
    for group in _groups(symbol, body_style, units):
        for item in group.children():
            if item.head in ("polyline", "bezier"):
                pts = item.first("pts")
                xys = pts.children("xy") if pts else []
                if item.head == "bezier":
                    xys = [xys[0], xys[-1]] if xys else []
                points += [(xy.number(0, 0.0), xy.number(1, 0.0)) for xy in xys]
            elif item.head in ("rectangle", "arc"):
                for head in _BODY_POINTS[item.head]:
                    if (node := item.first(head)) is not None:
                        points.append((node.number(0, 0.0), node.number(1, 0.0)))
            elif item.head == "circle":
                center, radius = item.first("center"), item.first("radius")
                if center is not None and radius is not None:
                    x, y, r = center.number(0, 0.0), center.number(1, 0.0), radius.number(0, 0.0)
                    points += [(x - r, y), (x + r, y), (x, y - r), (x, y + r)]
    return points


def library_pins(
    symbol: Node, body_style: int = 1, units: set[int] | None = None
) -> list[LibraryPin]:
    """Return the pins that KiCad gives a placed symbol.

    A placed symbol has the pins of all units with the body style 0 (all
    body styles) or its own body style (SCH_SYMBOL::UpdatePins). A pin
    outside a sub-symbol is in unit 1 with body style 1. A unit shows its own
    pins and the pins of unit 0 (SCH_SYMBOL::GetPins).

    Args:
        symbol: The (symbol ...) of a flattened symbol.
        units: Only the pins that these units show. None: the pins of all units.

    Raises:
        ValueError: The (at ...) of a pin is not a number.
    """
    pins = []
    for group in _groups(symbol, body_style, units):
        for pin in group.children("pin"):
            number = pin.first("number")
            at = pin.first("at")
            pins.append(
                LibraryPin(
                    pin_number(number.value(0, "")) if number else "",
                    pin.value(0, ""),
                    at.number(0, 0.0) if at else 0.0,
                    at.number(1, 0.0) if at else 0.0,
                    hidden(pin),
                )
            )
    return pins


def symbol_pins(symbol: Node, body_style: int = 1) -> list[str]:
    """Return the pin numbers that KiCad gives a placed symbol (library_pins).

    Args:
        symbol: The (symbol ...) of a flattened symbol.
    """
    return [pin.number for pin in library_pins(symbol, body_style)]
