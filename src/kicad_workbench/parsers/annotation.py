"""The check of the annotation, as KiCad does it (``SCH_REFERENCE_LIST::CheckAnnotation``).

The rules come from the source of KiCad 8.0.9, 9.0.9 and 10.0.6. The nightly
builds (KiCad 11) have different rules: the module refuses them.

Who runs the check:

- The ERC dialog of the schematic editor (``DIALOG_ERC::OnRunERCClick``,
  ``CheckAnnotate`` with ``ANNOTATE_ALL``): all symbols. Each error is an ERC
  marker at the symbol of the first item. ``kicad-cli sch erc`` does not run it.
- The netlist job of kicad-cli (all symbols) and the BOM job (no symbol with a
  reference that starts with "#"): they write only "Warning: schematic has
  annotation errors".

The check (``CheckAnnotation``):

1. It sorts the items (``sortByRefAndValue``): the full reference with no case,
   the Value with case, the unit, the sheet number, x, y, the UUID. It then
   splits each reference (``SCH_REFERENCE::Split``). A reference that ends with
   "?" or with no digit is not annotated.
2. It gives the first item that is not annotated, or the first unit that is
   larger than the unit count, and stops this loop.
3. It compares each pair of neighbours with the same prefix (no case) and the
   same number: the same unit, different unit counts, different Values.

The sheet number of an item is the virtual page number in KiCad 8. KiCad 9 and
10 use the page when it is an integer, else the virtual page number
(``GetPageNumberAsInt``).

KiCad changes the references and the pages after the load (see the view
"erc" and "cli" of ``check_annotation``):

- ``CheckForMissingSymbolInstances`` gives an instance to a symbol with no
  instance for a sheet path: "U?" and unit 1 in the GUI of KiCad 8 and 9. In
  KiCad 10, a copy of the first instance of the project, else of the first
  instance, else the Reference field and the unit of the symbol. kicad-cli 8
  and 9 do not run it (``SCH_SYMBOL::GetRef`` then uses the Reference field).
- ``SCH_SHEET_LIST::AnnotatePowerSymbols`` (KiCad 8 and 9) gives a number to
  each power symbol that is not annotated or has a duplicate reference. kicad-cli
  runs it at the load, the ERC dialog of KiCad 8 before the check. KiCad 9 runs
  it after the check. In KiCad 10 it has no effect.
- KiCad 10 repairs the pages (``RepairPageNumbers``). The GUI of KiCad 8 and 9
  gives pages only when all pages are empty (``SetInitialPageNumbers``).

Limits:

- KiCad gives no list of the annotation errors outside the GUI. The checks of
  ``spike/kicad-versions/annotation_check.py`` compare the count of errors > 0
  with the warning of kicad-cli. The details come from the source of KiCad.
- ``Split`` reads the UTF-8 bytes of a reference as a signed ``char`` (x86-64):
  a byte of a character that is not ASCII is like a space.
- The power symbols of one sheet go into the list in the order of an R-tree of
  KiCad. The tool uses the order of the file. The order can change only the
  numbers of new power symbols (KiCad 8 and 9).
"""

import itertools
import math
from collections.abc import Iterator
from dataclasses import dataclass, replace
from functools import cmp_to_key
from pathlib import Path

from ..utils.sch_hierarchy import (
    PRO_SUFFIX,
    SCH_SUFFIX,
    TopLevelSheet,
    text_variables,
    top_level_sheets,
)
from ..utils.schematic_settings import SubpartSettings, subpart_settings
from .kicad_fields import symbol_field_nodes
from .kicad_versions import EMPTY_TEXT_MARKER
from .lib_symbols import SUB_SYMBOL, unit_count
from .sch_geometry import Point, definition_key, node_point, placed_symbols
from .sexpr import Node
from .sheet_list import (
    Kiid,
    Screen,
    SheetList,
    SheetPath,
    build_sheet_list,
    item_kiid,
    kiid_less,
    kiid_path,
    sort_by_page_numbers,
    to_int,
    to_long,
    unknown_page_order,
)

KICAD_VERSIONS = (8, 9, 10)
VIEWS = ("erc", "cli")
SYMBOL_FILTERS = ("all", "non_power")
# The most orders of sheets with an unknown order that the check tries.
_MAX_ORDERS = 120
_INT_MAX = 0x7FFFFFFF


@dataclass(frozen=True)
class AnnotationItem:
    """A symbol in one sheet path, as the check saw it."""

    reference: str  # SCH_SYMBOL::GetRef of the sheet path (no unit letters)
    unit: int
    value: str
    file: Path
    sheet: str  # the sheet names, for example "/Power/"
    path: str  # the UUID path of the sheet
    uuid: str | None  # the UUID of the symbol
    position: Point  # IU


@dataclass(frozen=True)
class AnnotationError:
    """An error of CheckAnnotation."""

    code: str  # unannotated, extra_units, duplicate_reference, unit_value_mismatch
    message: str  # the text of KiCad (English), with no line feed at the end
    first: AnnotationItem
    second: AnnotationItem | None


@dataclass
class AnnotationCheck:
    """The result of check_annotation."""

    errors: list[AnnotationError]
    problems: list[str]  # why the result can be different from KiCad


# --- The symbols after the load ---------------------------------------------


@dataclass
class _Instance:
    project: str
    path: tuple[Kiid, ...]
    reference: str
    unit: int


def ref_des_prefix(text: str) -> str:
    """``UTIL::GetRefDesPrefix``: the text with no "?" and no digits at the end."""
    end = len(text)
    while end > 0 and (text[end - 1] == "?" or "0" <= text[end - 1] <= "9"):
        end -= 1
    return text[:end]


def ref_des_unannotated(text: str) -> str:
    """``UTIL::GetRefDesUnannotated``: the prefix and "?"."""
    return ref_des_prefix(text) + "?"


def _update_prefix(text: str) -> str | None:
    """``SCH_SYMBOL::UpdatePrefix``: the prefix of a Reference field, or None."""
    prefix = text.replace("~", " ")
    while prefix and (prefix[-1] in "?*" or "0" <= prefix[-1] <= "9"):
        prefix = prefix[:-1]
    prefix = prefix.strip(" \t\n\v\f\r")
    return prefix or None


@dataclass(eq=False)
class _Symbol:
    """A placed symbol of a screen, as KiCad keeps it after the load (SCH_SYMBOL)."""

    file: Path
    node: Node
    kiid: Kiid
    uuid: str | None
    position: Point
    lib_name: str  # the item name of the lib_id (LIB_ID::GetLibItemName)
    definition: Node | None  # None: no library symbol (GetLibSymbolRef)
    unit_count: int
    power: bool  # LIB_SYMBOL::IsPower
    unit_names: dict[int, str]  # LIB_SYMBOL::GetUnitDisplayNames
    reference_field: str
    prefix: str
    unit: int  # m_unit
    value: str  # the text of the Value field
    instances: list[_Instance]

    def instance(self, path: tuple[Kiid, ...]) -> _Instance | None:
        return next((i for i in self.instances if i.path == path), None)

    def add_instance(self, instance: _Instance) -> None:
        """``SCH_SYMBOL::AddHierarchicalReference``."""
        self.instances = [i for i in self.instances if i.path != instance.path]
        self.instances.append(instance)
        if len(self.instances) == 1:
            self.reference_field = instance.reference
            self.unit = instance.unit

    def get_ref(self, path: tuple[Kiid, ...]) -> str:
        """``SCH_SYMBOL::GetRef`` with no unit."""
        found = self.instance(path)
        ref = found.reference if found is not None else ""
        if not ref and self.reference_field:
            ref = self.reference_field
        return ref or ref_des_unannotated(self.prefix)

    def unit_selection(self, path: tuple[Kiid, ...]) -> int:
        """``SCH_SYMBOL::GetUnitSelection``."""
        found = self.instance(path)
        return found.unit if found is not None else self.unit


def _unit_names(definition: Node) -> dict[int, str]:
    """The (unit_name ...) of each unit of a library symbol, as the parser reads
    them: the last name of a unit wins."""
    names: dict[int, str] = {}
    for sub in definition.children("symbol"):
        match = SUB_SYMBOL.search(sub.value(0, ""))
        if match is None:
            continue
        for node in sub.children("unit_name"):
            if node.value(0) is not None:
                names[int(match.group(1))] = node.value(0, "")
    return names


def _load_symbol(screen: Screen, node: Node, kicad: int) -> _Symbol:
    """Read a placed symbol in the order of the file, as the parser of the KiCad
    version does.

    KiCad 8 and 9 add the instance of a (path ...) after each item in it: a path
    with no items gives no instance. KiCad 10 adds it after the path.

    Raises:
        ValueError: A unit is not a number.
    """
    symbol_kiid, uuid = item_kiid(screen, node)
    at = node.first("at")
    lib_id = node.first("lib_id")
    lib_text = lib_id.value(0, "") if lib_id is not None else ""
    definition = screen.library.get(definition_key(node))
    symbol = _Symbol(
        screen.file,
        node,
        symbol_kiid,
        uuid,
        node_point(at) if at is not None else (0, 0),
        lib_text.partition(":")[2] if ":" in lib_text else lib_text,
        definition,
        unit_count(definition) if definition is not None else 1,
        definition is not None and definition.first("power") is not None,
        _unit_names(definition) if definition is not None else {},
        "",
        "U",
        1,
        "",
        [],
    )

    def text(value: str) -> str:
        return "" if value == "~" and screen.version < EMPTY_TEXT_MARKER else value

    fields = symbol_field_nodes(node, screen.rules)
    events: list[tuple[int, str, object]] = []
    if "Reference" in fields:
        events.append((fields["Reference"][1].start, "reference", fields["Reference"][0]))
    if "Value" in fields:
        events.append((fields["Value"][1].start, "value", fields["Value"][0]))
    for unit in node.children("unit"):
        events.append((unit.start, "unit", int(unit.number(0, 1))))
    for instances in node.children("instances"):
        for project in instances.children("project"):
            for path in project.children("path"):
                events.append((path.start, "path", (project.value(0, ""), path)))
    for _start, kind, data in sorted(events, key=lambda e: e[0]):
        if kind == "reference":
            symbol.reference_field = text(str(data))
            symbol.prefix = _update_prefix(symbol.reference_field) or symbol.prefix
        elif kind == "value":
            symbol.value = text(str(data))
        elif kind == "unit":
            symbol.unit = int(data)  # type: ignore[call-overload]
        else:
            project_name, path = data  # type: ignore[misc]
            instance = _Instance(project_name, kiid_path(path.value(0, "")), "", 1)
            for item in path.children():
                if item.head == "reference":
                    instance.reference = item.value(0, "")
                elif item.head == "unit":
                    instance.unit = int(item.number(0, 1))
                elif item.head == "value":
                    symbol.value = text(item.value(0, ""))
                if kicad <= 9:
                    symbol.add_instance(replace(instance))
            if kicad >= 10:
                symbol.add_instance(instance)
    return symbol


def _copy(symbol: _Symbol) -> _Symbol:
    """A copy of a symbol with its own instances (the load fixes change them)."""
    return replace(symbol, instances=[replace(i) for i in symbol.instances])


# --- SCH_REFERENCE ----------------------------------------------------------


def _lower(char: str) -> str:
    lower = char.lower()
    return lower if len(lower) == 1 else char


def cmp_no_case(a: str, b: str) -> int:
    """``wxString::CmpNoCase``: the characters in lower case, then the length."""
    for char_a, char_b in zip(a, b, strict=False):
        lower_a, lower_b = _lower(char_a), _lower(char_b)
        if lower_a != lower_b:
            return -1 if lower_a < lower_b else 1
    return (len(a) > len(b)) - (len(a) < len(b))


def _cmp(a: str, b: str) -> int:
    return (a > b) - (a < b)


def _is_digit(byte: int) -> bool:
    return 0x30 <= byte <= 0x39


@dataclass(eq=False)
class _Ref:
    """An item of the reference list (SCH_REFERENCE)."""

    symbol: _Symbol
    path: SheetPath
    ref: str  # m_ref: the full reference, then the prefix after Split
    unit: int
    value: str
    sheet_num: int
    is_new: bool = False
    num: int = 0
    num_str: str = ""
    flag: int = 0
    item: AnnotationItem | None = None

    def split(self) -> None:
        """``SCH_REFERENCE::Split`` on the UTF-8 bytes, with a signed ``char``."""
        data = self.ref.encode("utf-8")
        self.num = -1
        self.num_str = ""
        last = len(data) - 1
        if last < 0:
            self.is_new = True  # KiCad reads before the text: the tool calls it new.
            return
        if data[last] == ord("?"):
            self.is_new = True
            self.ref = data[:last].decode("utf-8")
        elif not _is_digit(data[last]):
            self.is_new = True
        else:
            while last >= 0:
                byte = data[last]
                if byte >= 0x80 or byte <= 0x20 or _is_digit(byte):
                    last -= 1
                    continue
                if _is_digit(data[last + 1]):
                    digits = data[last + 1 :]
                    end = 0
                    while end < len(digits) and _is_digit(digits[end]):
                        end += 1
                    # atoi: strtol gives LONG_MAX on overflow, then the cast to int.
                    self.num = to_int(min(int(digits[:end]), 2**63 - 1))
                self.num_str = data[last + 1 :].decode("utf-8")
                self.ref = data[: last + 1].decode("utf-8")
                break


def _sub_reference(symbol: _Symbol, unit: int, kicad: int, settings: SubpartSettings) -> str:
    """``SCH_SYMBOL::SubReference`` with the separator."""
    if kicad >= 10:
        name = symbol.unit_names.get(unit, "")
        if name:
            return (chr(settings.separator) if settings.separator else "") + name
    if unit < 1:
        return ""
    text = chr(settings.separator) if settings.separator else ""
    if ord("0") <= settings.first_id <= ord("9"):
        return text + str(unit)
    # LIB_SYMBOL::LetterSubReference
    suffix = ""
    while True:
        u = (unit - 1) % 26
        suffix = chr(settings.first_id + u) + suffix
        unit = (unit - u) // 26
        if unit <= 0:
            return text + suffix


# --- The view of KiCad after the load -----------------------------------------


@dataclass
class _State:
    sheet_list: SheetList
    pages: dict[object, str]
    symbols: dict[Path, list[_Symbol]]
    order: list[SheetPath]  # the hierarchy, sorted
    problems: list[str]

    def page(self, path: SheetPath) -> str:
        return self.pages.get(self.sheet_list.page_key(path), "")

    def set_page(self, path: SheetPath, page: str) -> None:
        self.pages[self.sheet_list.page_key(path)] = page


def _sorted(state: _State, rank: dict[int, int]) -> list[SheetPath]:
    return sort_by_page_numbers(
        state.sheet_list.paths, state.page, lambda path: rank.get(id(path), 0)
    )


def _sheet_numbers(state: _State, kicad: int) -> dict[int, int]:
    """The sheet number of each path for the sort of the items."""
    numbers = {}
    for virtual, path in enumerate(state.order, start=1):
        value = to_long(state.page(path)) if kicad >= 9 else None
        numbers[id(path)] = to_int(value) if value is not None else virtual
    return numbers


def _new_ref(symbol: _Symbol, path: SheetPath, kicad: int, sheet_num: int) -> _Ref:
    """``SCH_REFERENCE::SCH_REFERENCE``. KiCad 9 and 10 add a missing instance with
    the reference of ``GetRef`` and the unit of the symbol."""
    if kicad >= 9 and symbol.instance(path.kiids) is None:
        symbol.add_instance(_Instance("", path.kiids, symbol.get_ref(path.kiids), symbol.unit))
    ref = symbol.get_ref(path.kiids) or "DefRef?"
    return _Ref(
        symbol, path, ref, symbol.unit_selection(path.kiids), symbol.value or "~", sheet_num
    )


def _missing_instances(state: _State, kicad: int, project: str) -> None:
    """``SCH_SHEET_LIST::CheckForMissingSymbolInstances``."""
    for path in state.order:
        if path.screen is None:
            continue
        for symbol in state.symbols[path.screen.file]:
            if symbol.instance(path.kiids) is not None:
                continue
            if kicad <= 9:
                symbol.add_instance(
                    _Instance(project, path.kiids, ref_des_unannotated(symbol.prefix), 1)
                )
            elif symbol.instances:
                source = next(
                    (i for i in symbol.instances if i.project == project), symbol.instances[0]
                )
                symbol.add_instance(_Instance(project, path.kiids, source.reference, source.unit))
            else:
                symbol.add_instance(
                    _Instance(project, path.kiids, symbol.reference_field, symbol.unit)
                )


def _repair_pages(state: _State, kicad: int) -> None:
    """``SetInitialPageNumbers`` (all pages empty), else ``RepairPageNumbers`` (KiCad 10)."""
    pages = [state.page(path) for path in state.order]
    if all(page == "" for page in pages):
        for number, path in enumerate(state.order, start=1):
            state.set_page(path, str(number))
        return
    if kicad < 10:
        return
    reserved = {page for page in pages if page}
    assigned: set[str] = set()
    next_page = 1
    for path in state.order:
        page = state.page(path)
        if page and page not in assigned:
            assigned.add(page)
            continue
        while str(next_page) in reserved or str(next_page) in assigned:
            next_page += 1
        state.set_page(path, str(next_page))
        assigned.add(str(next_page))
        next_page += 1


def _annotate_power_symbols(
    state: _State, kicad: int, symbols_filter: str, problems: list[str]
) -> None:
    """``SCH_SHEET_LIST::AnnotatePowerSymbols`` of KiCad 8 and 9."""
    refs: list[_Ref] = []
    for path in state.order:
        if path.screen is None:
            continue
        for symbol in state.symbols[path.screen.file]:
            if symbol.definition is not None and symbol.power:
                refs.append(_new_ref(symbol, path, kicad, 0))
    seen: set[str] = set()
    for ref in refs:
        if not ref.ref:
            continue
        if ref.ref not in seen:
            seen.add(ref.ref)
            continue
        # A duplicate: KiCad removes the digits at the end (the test of "not
        # annotated" is always false).
        text = ref.ref
        while text and "0" <= text[-1] <= "9":
            text = text[:-1]
        ref.ref = text
    for ref in refs:
        ref.split()
    for ref in refs:
        if not ref.ref.startswith("#"):
            ref.ref = "#" + ref.ref
    new_numbers = _annotate(refs, start=100, kicad=kicad)
    # SetRef also sets the Reference field for the current sheet: the root at the load.
    current = state.sheet_list.paths[0] if state.sheet_list.paths else None
    for ref in refs:
        full = ref.ref + "?" if ref.num < 0 else ref.ref + ref.num_str
        symbol, path = ref.symbol, ref.path.kiids
        found = symbol.instance(path)
        if found is not None:
            found.reference = full
        else:
            symbol.add_instance(_Instance("", path, full, symbol.unit))
        if current is not None and ref.path is current:
            symbol.reference_field = full
        symbol.prefix = ref_des_prefix(full) or "U"
        symbol.unit = ref.unit
        found = symbol.instance(path)
        if found is not None:
            found.unit = ref.unit
    # A new number can be the number of a symbol that is not a power symbol.
    if new_numbers:
        power = {id(ref.symbol) for ref in refs}
        for path in state.order:
            if path.screen is None:
                continue
            for symbol in state.symbols[path.screen.file]:
                reference = symbol.get_ref(path.kiids)
                if id(symbol) in power or not _in_list(symbol, reference, symbols_filter):
                    continue
                other = _Ref(symbol, path, reference, 0, "", 0)
                other.split()
                if any(
                    cmp_no_case(other.ref, prefix) == 0 and other.num == number
                    for prefix, number in new_numbers
                ):
                    problems.append(
                        f"KiCad gives new numbers to power symbols in an order that the tool "
                        f"does not know, and {reference} uses such a number: "
                        "the errors of this reference can be different in KiCad."
                    )


def _annotate(refs: list[_Ref], start: int, kicad: int) -> list[tuple[str, int]]:
    """``SCH_REFERENCE_LIST::Annotate( false, 0, start, {}, {} )`` with no sort.
    Return the new (prefix, number) pairs."""
    new_numbers: list[tuple[str, int]] = []
    if not refs:
        return new_numbers
    first = 0
    min_ref_id = start + 1

    def number_text(ref: _Ref, number: int) -> str:
        # SCH_REFERENCE::formatRefStr: KiCad 9 writes "0" before the number of a
        # power symbol.
        return f"0{number}" if kicad >= 9 and ref.symbol.power else str(number)

    for index, ref in enumerate(refs):
        if ref.flag:
            continue
        if cmp_no_case(refs[first].ref, ref.ref) != 0:
            first = index
            min_ref_id = start + 1
        if ref.symbol.unit_count <= 1:
            if ref.is_new:
                used = sorted(
                    {
                        other.num
                        for other in refs
                        if cmp_no_case(refs[first].ref, other.ref) == 0
                        and other.num >= min_ref_id
                        and not other.is_new
                    }
                )
                number = min_ref_id
                for value in used:
                    if value == number:
                        number += 1
                    elif value > number:
                        break
                ref.num = number
                ref.num_str = number_text(ref, number)
                new_numbers.append((ref.ref, number))
            ref.flag = 1
            ref.is_new = False
            continue
        if ref.is_new:
            # FindFirstUnusedReference with the unit of the symbol.
            by_number: dict[int, list[_Ref]] = {}
            for other in refs:
                if cmp_no_case(other.ref, ref.ref) == 0 and not other.is_new:
                    by_number.setdefault(other.num, []).append(other)
            number = min_ref_id
            while by_number.get(number):
                if not any(
                    other.symbol.lib_name != ref.symbol.lib_name
                    or other.value != ref.value
                    or other.unit == ref.unit
                    for other in by_number[number]
                ):
                    break
                number += 1
            ref.num = number
            ref.num_str = number_text(ref, number)
            ref.is_new = False
            ref.flag = 1
            new_numbers.append((ref.ref, number))
    return new_numbers


# --- CheckAnnotation --------------------------------------------------------


def _item(ref: _Ref, reference: str) -> AnnotationItem:
    return AnnotationItem(
        reference,
        ref.unit,
        ref.value,
        ref.symbol.file,
        ref.path.shown,
        ref.path.path,
        ref.symbol.uuid,
        ref.symbol.position,
    )


def _sort_refs(a: _Ref, b: _Ref) -> int:
    """``SCH_REFERENCE_LIST::sortByRefAndValue``."""
    for result in (
        cmp_no_case(a.ref, b.ref),
        _cmp(a.value, b.value),
        a.unit - b.unit,
        a.sheet_num - b.sheet_num,
        a.symbol.position[0] - b.symbol.position[0],
        a.symbol.position[1] - b.symbol.position[1],
    ):
        if result:
            return -1 if result < 0 else 1
    less = kiid_less(a.symbol.kiid, b.symbol.kiid)
    if less is None:
        return 0
    return -1 if less else (1 if kiid_less(b.symbol.kiid, a.symbol.kiid) else 0)


def _check(
    refs: list[_Ref], kicad: int, settings: SubpartSettings
) -> tuple[list[AnnotationError], list[str]]:
    """``SCH_REFERENCE_LIST::CheckAnnotation``. Return the errors, and a problem
    when the order of two items of an error is not known."""
    for ref in refs:
        ref.item = _item(ref, ref.ref)
    refs = sorted(refs, key=cmp_to_key(_sort_refs))
    # Two items that the sort sees as equal (the same KIID, or a KIID that KiCad
    # makes at random): std::sort gives them in an order that is not known.
    unknown_order = {
        id(ref)
        for a, b in zip(refs, refs[1:], strict=False)
        if _sort_refs(a, b) == 0
        for ref in (a, b)
    }
    for ref in refs:
        ref.split()
    errors: list[AnnotationError] = []
    error_refs: list[_Ref] = []

    def number(ref: _Ref) -> str:
        return ref.num_str if ref.num >= 0 else "?"

    def sub(symbol: _Symbol, unit: int) -> str:
        return _sub_reference(symbol, unit, kicad, settings)

    for ref in refs:
        count = ref.symbol.unit_count
        if ref.is_new:
            if 0 < ref.unit < _INT_MAX and count > 1:
                message = f"Item not annotated: {ref.ref}{number(ref)} (unit {ref.unit})"
            else:
                message = f"Item not annotated: {ref.ref}{number(ref)}"
            errors.append(AnnotationError("unannotated", message, ref.item, None))  # type: ignore[arg-type]
            error_refs.append(ref)
            break
        if max(count, 1) < ref.unit:
            message = (
                f"Error: symbol {ref.ref}{number(ref)}{sub(ref.symbol, ref.unit)} "
                f"(unit {ref.unit}) exceeds units defined ({count})"
            )
            errors.append(AnnotationError("extra_units", message, ref.item, None))  # type: ignore[arg-type]
            error_refs.append(ref)
            break

    for first, second in zip(refs, refs[1:], strict=False):
        if cmp_no_case(first.ref, second.ref) != 0 or first.num != second.num:
            continue
        if first.unit == second.unit:
            unit = sub(first.symbol, first.unit) if first.symbol.unit_count > 1 else ""
            message = f"Duplicate items {first.ref}{number(first)}{unit}"
            errors.append(
                AnnotationError("duplicate_reference", message, first.item, second.item)  # type: ignore[arg-type]
            )
            error_refs += [first, second]
            continue
        if first.symbol.unit_count != second.symbol.unit_count:
            message = (
                f"Differing unit counts for item {first.ref}{number(first)}"
                f"{sub(first.symbol, first.unit)} and {second.ref}{number(second)}"
                f"{sub(first.symbol, second.unit)}"
            )
            errors.append(
                AnnotationError("duplicate_reference", message, first.item, second.item)  # type: ignore[arg-type]
            )
            error_refs += [first, second]
            continue
        if _cmp(first.value, second.value) != 0:
            message = (
                f"Different values for {first.ref}{first.num}{sub(first.symbol, first.unit)} "
                f"({first.value}) and {second.ref}{second.num}"
                f"{sub(first.symbol, second.unit)} ({second.value})"
            )
            errors.append(
                AnnotationError("unit_value_mismatch", message, first.item, second.item)  # type: ignore[arg-type]
            )
            error_refs += [first, second]
    problems = []
    if any(id(ref) in unknown_order for ref in error_refs):
        problems.append(
            "KiCad sorts some symbols in an order that the tool cannot know (the same "
            "reference, value, unit, sheet number and position, and the same UUID or a "
            "UUID that KiCad makes at random): the first item of an error can be different."
        )
    return errors, problems


class _Pages:
    """The pages after ``SCH_SHEET_LIST::UpdateSheetInstanceData`` of kicad-cli: the
    (sheet_instances) entries of the root file that are not "/" set the page of
    the path with the same KIIDs (the first entry)."""

    def __init__(self, sheet_list: SheetList):
        self.sheet_list = sheet_list
        self.pages = dict(sheet_list.pages)
        root = sheet_list.paths[0].screen if sheet_list.paths else None
        for path in sheet_list.paths:
            for key, page in root.sheet_instances if root is not None else []:
                if key == path.kiids:
                    self.pages[sheet_list.page_key(path)] = page
                    break

    def __call__(self, path: SheetPath) -> str:
        return self.pages.get(self.sheet_list.page_key(path), "")


def _in_list(symbol: _Symbol, reference: str, symbols_filter: str) -> bool:
    """``SCH_SHEET_PATH::AppendSymbol``: the filter, and a library symbol."""
    if symbols_filter == "non_power" and reference.startswith("#"):
        return False
    return symbol.definition is not None


def _run(
    sheet_list: SheetList,
    loaded: dict[Path, list[_Symbol]],
    kicad: int,
    view: str,
    symbols_filter: str,
    project: str,
    settings: SubpartSettings,
    rank: dict[int, int],
) -> tuple[list[AnnotationError], list[str]]:
    """Run the load fixes and the check for one order of the paths with an unknown order."""
    problems: list[str] = []
    symbols = {file: [_copy(symbol) for symbol in items] for file, items in loaded.items()}
    state = _State(sheet_list, dict(sheet_list.pages), symbols, [], problems)
    state.order = _sorted(state, rank)

    if view == "cli":
        state.pages = _Pages(sheet_list).pages

    if view == "erc" or kicad >= 10:
        _missing_instances(state, kicad, project)
        _repair_pages(state, kicad)
    if view == "cli" and kicad <= 9:
        _annotate_power_symbols(state, kicad, symbols_filter, problems)
    # RecalculateConnections sorts the hierarchy again (RefreshHierarchy).
    state.order = _sorted(state, rank)
    if view == "erc" and kicad == 8:
        _annotate_power_symbols(state, kicad, symbols_filter, problems)

    numbers = _sheet_numbers(state, kicad)
    refs: list[_Ref] = []
    for path in state.order:
        if path.screen is None:
            continue
        for symbol in symbols[path.screen.file]:
            if _in_list(symbol, symbol.get_ref(path.kiids), symbols_filter):
                refs.append(_new_ref(symbol, path, kicad, numbers[id(path)]))
    if not refs:
        return [], problems
    errors, check_problems = _check(refs, kicad, settings)
    return errors, problems + check_problems


def _merged(groups: list[list[SheetPath]]) -> list[list[SheetPath]]:
    """Join the groups that have a path in common."""
    merged: list[list[SheetPath]] = []
    for group in groups:
        ids = {id(path) for path in group}
        joined = [old for old in merged if ids & {id(path) for path in old}]
        for old in joined:
            merged.remove(old)
        paths = {id(path): path for old in [*joined, group] for path in old}
        merged.append(list(paths.values()))
    return merged


def _orders(groups: list[list[SheetPath]]) -> Iterator[dict[int, int]]:
    """Yield each order of the paths of each group, as the place of each path."""
    if not groups:
        yield {}
        return
    group, rest = groups[0], groups[1:]
    for permutation in itertools.permutations(range(len(group))):
        place = {id(path): index for path, index in zip(group, permutation, strict=True)}
        for other in _orders(rest):
            yield {**place, **other}


def check_annotation(
    project_file: Path, kicad: int, view: str = "erc", symbols: str = "all"
) -> AnnotationCheck:
    """Check the annotation of a project as KiCad does it (CheckAnnotation).

    Args:
        project_file: The .kicad_pro file (it can be missing).
        kicad: The major version of the KiCad that runs the check: 8, 9 or 10.
        view: "erc": the ERC dialog of the schematic editor, at the first ERC run
            after the project opens. "cli": the jobs of kicad-cli (only the root
            schematic).
        symbols: "all" (ERC, the netlist job) or "non_power" (the BOM job: no
            reference that starts with "#").

    Raises:
        ValueError: An argument is not valid, KiCad has no verified rules for the
            version, or KiCad cannot load the root schematic. A unit is not a number.
        KicadFileError: A file has a version that the server does not know.
    """
    if kicad not in KICAD_VERSIONS:
        raise ValueError(
            f"The server has no verified rules of the annotation check for KiCad {kicad}: "
            "only KiCad 8, 9 and 10."
        )
    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    if symbols not in SYMBOL_FILTERS:
        raise ValueError(f"symbols must be one of {', '.join(SYMBOL_FILTERS)}")
    project_file = Path(project_file).absolute()
    root = project_file.with_suffix(SCH_SUFFIX)
    problems: list[str] = []
    if view == "erc" and kicad >= 10:
        tops = []
        for top in top_level_sheets(project_file.with_suffix(PRO_SUFFIX)):
            if top.file.is_file():
                tops.append(top)
            else:
                problems.append(
                    f"The top-level sheet {top.file} does not exist: KiCad does not load it."
                )
        if not tops:
            raise ValueError(f"KiCad cannot load a top-level sheet of {project_file}.")
    else:
        tops = [TopLevelSheet(root, None, root.stem)]
    sheet_list = build_sheet_list(tops, project_file.parent, text_variables(root), kicad)
    problems += sheet_list.problems
    settings = subpart_settings(project_file.with_suffix(PRO_SUFFIX))
    if settings.problem:
        problems.append(settings.problem)
    project = project_file.stem
    loaded = {
        file: [_load_symbol(screen, node, kicad) for node in placed_symbols(screen.root)]
        for file, screen in sheet_list.screens.items()
    }

    # The paths whose order KiCad gives with a hash of the path, or with an
    # inconsistent compare of two texts of one integer page (KiCad 8 uses the
    # order for the sheet numbers).
    candidates: list[list[SheetPath]] = []
    for page in (sheet_list.page, _Pages(sheet_list) if view == "cli" else sheet_list.page):
        candidates += sheet_list.hash_ties(page)
        if kicad == 8:
            candidates += unknown_page_order(sheet_list.paths, page)
    groups = _merged(candidates)
    total = math.prod(math.factorial(len(group)) for group in groups)
    results = [
        _run(sheet_list, loaded, kicad, view, symbols, project, settings, rank)
        for rank in itertools.islice(_orders(groups), _MAX_ORDERS)
    ]
    errors, run_problems = results[0]
    problems += run_problems
    if total > _MAX_ORDERS or any(result[0] != errors for result in results[1:]):
        names = "; ".join(", ".join(path.shown for path in group) for group in groups)
        tried = "" if total <= _MAX_ORDERS else f" The tool tried {_MAX_ORDERS} of {total} orders."
        problems.append(
            "KiCad sorts some sheets in an order that the tool cannot know (a hash of the "
            f"sheet path, or two texts of one page number): {names}. The errors can be "
            f"different in KiCad.{tried}"
        )
    return AnnotationCheck(errors, list(dict.fromkeys(problems)))
