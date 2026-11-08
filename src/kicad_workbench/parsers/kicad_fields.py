"""Fields of schematic symbols and sheets, as the KiCad that loads the file reads them.

The parser of each KiCad reads the fields of a file of each version with its own rules:
it does not branch on the file version to find a field (rule 4 of CLAUDE.md; measured:
kicad-cli 10 reads the sheet fields of files of KiCad 8 and 9 with the rules of KiCad
10, and kicad-cli 9 the files of KiCad 8 with the rules of KiCad 9,
spike/kicad-versions/sheet_fields_check.py "10/8", "10/9", "9/8"). Thus the rules come
from the installed KiCad. Only when the KiCad is not known, they come from the KiCad
version that saved the file.

The parsers of KiCad 8 and 9, and of KiCad 10, find the mandatory fields
(Reference, Value, ... and Sheetname, Sheetfile) in different ways
(``parseSchField``, ``parseSchematicSymbol``, ``parseSheet`` and
``SCH_SHEET::SetFields`` of each branch):

- KiCad 8 and 9 find a field by its exact name ("Sheet name" and "Sheet
  file" of KiCad 6 with no difference of case). An (id N) replaces the ID
  only when N is not the ID of a mandatory field. A later mandatory field of
  a symbol replaces an earlier one. A sheet field with an ID that it read
  gets the next free ID. The sheet fields are sorted by ID, and the first two
  are the name and the file.
- KiCad 10 finds a mandatory field by its name with no difference of case and
  ignores all IDs. A later field of a symbol replaces an earlier one; a sheet
  uses its first field. The nightly builds of KiCad 11, up to the file version
  NEWEST_SCHEMATIC, read the fields in the same way.
"""

import contextvars
import functools
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import KicadParseError
from .kicad_versions import KICAD9, KicadFileError, schematic_version
from .sexpr import Node, property_field

# The canonical names of the mandatory symbol fields of KiCad 8, 9 and 10, by ID.
SYMBOL_FIELD_NAMES = ("Reference", "Value", "Footprint", "Datasheet", "Description")
_SHEET_NAMES = ("Sheetname", "Sheetfile")


@dataclass(frozen=True)
class FieldRules:
    """The field rules of the KiCad that loads a file."""

    kicad: int  # 9 (KiCad 8 and 9) or 10 (KiCad 10 and the nightly builds)
    file_version: int

    def note(self) -> str:
        """One line for the output of a tool."""
        name = "8 and 9" if self.kicad == 9 else "10"
        return f"Fields read with the rules of KiCad {name} (file version {self.file_version})."


# The field rules that the parsers used in one tool call (see with_field_rules_note).
_used_rules: contextvars.ContextVar[list[FieldRules] | None] = contextvars.ContextVar(
    "used_field_rules", default=None
)


def record_field_rules(rules: FieldRules) -> None:
    """Record that a parser reads fields with these rules in this tool call."""
    used = _used_rules.get()
    if used is not None and rules not in used:
        used.append(rules)


def with_field_rules_note(function: Callable) -> Callable:
    """Add the note of the field rules to the text output of an async tool.

    The note is added only when a parser read fields in the call. A tool that
    another decorated tool calls adds no note: the outer tool adds one note for
    both. Put the decorator below @mcp.tool().
    """

    @functools.wraps(function)
    async def wrapper(*args, **kwargs):
        if _used_rules.get() is not None:
            return await function(*args, **kwargs)
        token = _used_rules.set([])
        try:
            result = await function(*args, **kwargs)
            used = _used_rules.get()
        finally:
            _used_rules.reset(token)
        if isinstance(result, str) and used:
            result += "\n\n" + "\n".join(f"_{rules.note()}_" for rules in used)
        return result

    return wrapper


def rules_for_file(root: Node, kicad: int | None) -> FieldRules:
    """Return the field rules of the KiCad that loads the schematic.

    A file that is newer than the installed KiCad is an error before each tool
    (utils/file_versions.check_schematic_versions): that check knows the full
    version of the installed KiCad (10.0 or a nightly build 10.99), not only its major
    version.

    Args:
        kicad: The major version of the KiCad that loads the file (the installed
            KiCad). None: not known; then the KiCad version that saved the file.

    Raises:
        KicadFileError: The server cannot read the version of the file
            (kicad_versions.schematic_version), or the KiCad is older than KiCad 8.
    """
    version = schematic_version(root)
    if kicad is None:
        return rules_for_version(version)
    if kicad < 8:
        raise KicadFileError(f"KiCad {kicad} has no field rules: the minimum is KiCad 8.")
    return FieldRules(9 if kicad <= 9 else 10, version)


def rules_for_version(version: int) -> FieldRules:
    """Return the rules of the KiCad version that saves a schematic version."""
    return FieldRules(9 if version <= KICAD9.schematic else 10, version)


def _id_in_file(prop: Node) -> int | None:
    """Return the (id N) of a property, or None."""
    node = prop.first("id")
    text = node.value(0) if node else None
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _properties(item: Node) -> list[tuple[str, str, int | None, Node]]:
    """Return (name, value, the (id N) or None, the property) of each named property."""
    result = []
    for prop in item.children("property"):
        field = property_field(prop)
        if field is not None:
            result.append((*field, _id_in_file(prop), prop))
    return result


def _name_id(name: str, names: tuple[str, ...], no_case: bool) -> int | None:
    for index, canonical in enumerate(names):
        if name == canonical or (no_case and name.lower() == canonical.lower()):
            return index
    return None


def _next_free(field_id: int, used: set[int]) -> int:
    while field_id in used:
        field_id += 1
    return field_id


def symbol_fields(symbol: Node, rules: FieldRules) -> dict[str, str]:
    """Return the mandatory fields of a placed symbol, by canonical name."""
    return {name: value for name, (value, _prop) in symbol_field_nodes(symbol, rules).items()}


def symbol_field_nodes(symbol: Node, rules: FieldRules) -> dict[str, tuple[str, Node]]:
    """Return the mandatory fields of a placed symbol, by canonical name: (the value,
    the (property ...) that sets it). The last property of a field sets it."""
    mandatory = len(SYMBOL_FIELD_NAMES)
    by_id: dict[int, tuple[str, Node]] = {}
    for name, value, id_in_file, prop in _properties(symbol):
        field_id = _name_id(name, SYMBOL_FIELD_NAMES, no_case=rules.kicad == 10)
        if field_id is None:
            continue
        # KiCad 8 and 9: the (id N) of a user field makes the field a user
        # field. KiCad 10 ignores the IDs.
        if rules.kicad == 9 and id_in_file is not None and id_in_file >= mandatory:
            continue
        by_id[field_id] = (value, prop)
    return {SYMBOL_FIELD_NAMES[i]: by_id[i] for i in range(mandatory) if i in by_id}


def _sheet_field_id_v9(name: str, id_in_file: int | None) -> int:
    mandatory = len(_SHEET_NAMES)
    field_id = _name_id(name, _SHEET_NAMES, no_case=False)
    if field_id is None:
        field_id = _name_id(name, ("Sheet name", "Sheet file"), no_case=True)
    if id_in_file is not None and id_in_file >= mandatory:
        return id_in_file
    return mandatory if field_id is None else field_id


def sheet_fields(sheet: Node, rules: FieldRules) -> dict[str, str]:
    """Return the fields "Sheetname" and "Sheetfile" of a sheet that KiCad uses.

    The file name is in the notation of Unix: SCH_SHEET::SetFields of KiCad 8, 9
    and 10 changes each "\\" to "/" (SCH_SHEET::SetFileName).

    KiCad 10: a sheet with no file field gets an empty file name, thus no
    sub-sheet (SCH_SHEET::SetFields adds the field: the check "Missing sheet file
    property" of SCH_IO_KICAD_SEXPR_PARSER::parseSheet never fails). Branch master
    of 2026-11-06 has the same code (the rules of the nightly builds).

    Raises:
        KicadParseError: KiCad 10 and the sheet has no name field: the parser
            stops ("Missing sheet name property"). KiCad does not load the
            top-level sheet, and loads only the items before the sheet of a
            sub-sheet (measured with kicad-cli 10.0.6 and 10.0.7).
        KicadFileError: KiCad 8 or 9 and the sheet has less than two fields:
            KiCad reads the name and the file by their index in the list of
            fields, past its end. That is undefined behavior of C++: kicad-cli 8.0.9
            stops with a segmentation fault, kicad-cli 9.0.9 loaded no sub-sheet of
            the sheet in the measurement, but no rule of KiCad gives that result.
    """
    properties = _properties(sheet)
    if rules.kicad == 10:
        result: dict[str, str] = {}
        for name, value, _id, _prop in properties:
            field_id = _name_id(name, _SHEET_NAMES, no_case=True)
            if field_id is None:
                field_id = _name_id(name, ("Sheet name", "Sheet file"), no_case=True)
            if field_id is not None:
                result.setdefault(_SHEET_NAMES[field_id], value)
        if _SHEET_NAMES[0] not in result:
            raise KicadParseError("Missing sheet name property: a sheet has no name field.")
        result.setdefault(_SHEET_NAMES[1], "")
        result[_SHEET_NAMES[1]] = result[_SHEET_NAMES[1]].replace("\\", "/")
        return result
    fields: list[tuple[int, str]] = []
    used: set[int] = set()
    for name, value, id_in_file, _prop in properties:
        field_id = _next_free(_sheet_field_id_v9(name, id_in_file), used)
        used.add(field_id)
        fields.append((field_id, value))
    if len(fields) < len(_SHEET_NAMES):
        raise KicadFileError(
            f"A sheet has {len(fields)} properties. KiCad 8 and 9 need two: the "
            "sheet name and the sheet file."
        )
    # SCH_SHEET::SetFields of KiCad 8 and 9 sorts the fields by ID and then
    # reads the name and the file by index.
    fields.sort(key=lambda field: field[0])
    result = {_SHEET_NAMES[i]: fields[i][1] for i in range(len(_SHEET_NAMES))}
    result[_SHEET_NAMES[1]] = result[_SHEET_NAMES[1]].replace("\\", "/")
    return result
