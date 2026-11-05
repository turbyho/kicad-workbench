"""The fields of the units of one symbol: one package has one Value and one MPN.

The units of one reference (U301A and U301B) are one package and one line of
the BOM. KiCad gives an ERC error when the units have different values
(ERCE_DIFFERENT_UNIT_VALUE, SCH_REFERENCE_LIST::CheckAnnotation) or different
footprints (ERCE_DIFFERENT_UNIT_FP, ERC_TESTER::TestMultiunitFootprints). An
MPN field is a field of the user: KiCad does not compare it. In the BOM,
KiCad 9 and 10 join the different values of the units with a comma
("LM358NG,LM358P"), which looks like an MPN and its alternative, and KiCad 8
writes "-- mixed values --" (FIELDS_EDITOR_GRID_DATA_MODEL::getValue).

This module compares Value and the MPN fields of the units of each reference
(decision of the user, 2026-11-05: an empty field in one unit is a difference
too). It reads each schematic file of the sheet hierarchy:

- KiCad keeps the reference and the unit of a placed symbol for each sheet
  path, in (instances (project ... (path "<sheet path>" (reference ...)
  (unit ...))). A sheet that the hierarchy uses more than one time gives each
  of its symbols one reference for each sheet path. With no instance for the
  sheet path, KiCad uses the Reference field and the (unit) of the symbol
  (SCH_SYMBOL::GetRef).
- KiCad groups the units by the reference, with no difference of case
  (SCH_REFERENCE::CompareRef). A reference that is not annotated ("U?") is no
  group: its units are different lines of the BOM. A power symbol ("#PWR01")
  is not in the BOM.
- Two symbols with one reference and one unit, or with different unit
  counts, are a duplicate reference (ERCE_DUPLICATE_REFERENCE): KiCad does not
  compare their values, and this module does not compare their fields.
- KiCad compares the text of the fields as it is (SCH_REFERENCE::CompareValue).
  A field "~" is empty only in a file older than EMPTY_TEXT_MARKER
  (parseSchField); the field of a variant is never changed.
- A variant of KiCad 10 can set a field of one instance:
  (variant (name "Lite") (field (name "MPN") (value "...")))
  (SCH_SYMBOL_VARIANT). KiCad finds the field of a variant by its exact name.
  The fields of a variant are the fields of the symbol with the fields of its
  variant.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .kicad_fields import FieldRules, rules_for_version, symbol_fields
from .kicad_versions import EMPTY_TEXT_MARKER
from .sch_instances import placed_instances
from .sexpr import Node, property_field
from .symbol_info import letter_reference

# The names of the MPN fields, as datasheet-lens reads the columns of a BOM
# (MPN_COLUMNS of datasheet-lens), compared with no case, spaces, "_", "." and
# "-". Not "Part Number": it often holds a part number of the company.
MPN_FIELDS = (
    "MPN",
    "Manufacturer Part Number",
    "Manufacturer Part Number 1",
    "MPN 1",
    "MP",
    "Mfr Part Number",
    "Mfr PN",
    "Manufacturer PN",
)


def _field_key(name: str) -> str:
    return re.sub(r"[\s_.\-]", "", name).lower()


_MPN_KEYS = {_field_key(name) for name in MPN_FIELDS}


def is_mpn_field(name: str) -> bool:
    """Return True for the name of an MPN field (see MPN_FIELDS)."""
    return _field_key(name) in _MPN_KEYS


@dataclass(frozen=True)
class UnitConflict:
    """A field with different values in the units of one reference."""

    reference: str
    variant: str  # "": the default variant
    field: str
    values: tuple[tuple[str, str], ...]  # (unit letters, value); "" is an empty field

    def text(self) -> str:
        where = f" (variant {self.variant!r})" if self.variant else ""
        units = ", ".join(f"{unit} = {value!r}" for unit, value in self.values)
        return f"{self.reference}{where}: {self.field}: {units}"


@dataclass(frozen=True)
class _Unit:
    reference: str
    unit: int
    unit_count: int
    fields: dict[str, str]  # Value and the MPN fields of the symbol
    variants: dict[str, dict[str, str]]  # variant name -> the fields that it sets


def _symbol_fields(symbol: Node, rules: FieldRules, version: int) -> dict[str, str]:
    """Return Value and the MPN fields of a placed symbol, as KiCad reads them."""

    def text(value: str) -> str:
        return "" if value == "~" and version < EMPTY_TEXT_MARKER else value

    fields = {"Value": text(symbol_fields(symbol, rules).get("Value", ""))}
    for prop in symbol.children("property"):
        field = property_field(prop)
        if field is not None and is_mpn_field(field[0]):
            fields.setdefault(field[0], text(field[1]))
    return fields


def _variant_fields(path: Node | None) -> dict[str, dict[str, str]]:
    """Return the fields of the variants of an instance: Value and the MPN fields."""
    variants: dict[str, dict[str, str]] = {}
    for variant in path.children("variant") if path is not None else []:
        name = variant.first("name")
        if name is None or not name.value(0, ""):
            continue
        fields = variants.setdefault(name.value(0, ""), {})
        for field in variant.children("field"):
            field_name, value = field.first("name"), field.first("value")
            if field_name is None or value is None:
                continue
            text = field_name.value(0, "")
            if text == "Value" or is_mpn_field(text):
                fields[text] = value.value(0, "")
    return variants


def _units(project: Path) -> tuple[dict[str, list[_Unit]], list[str]]:
    """Return the units of each annotated reference of the project (the key is the
    reference in upper case), and the problems.

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
        KicadFileError: A file has a version that the server does not know.
        ValueError: A unit is not a number.
    """
    instances, files, problems = placed_instances(project)
    fields: dict[tuple[Path, int], dict[str, str]] = {}  # each symbol one time
    units: dict[str, list[_Unit]] = {}
    for instance in instances:
        text = instance.reference
        if text.startswith("#") or text.endswith("?"):
            continue
        record = files[instance.file]
        if instance.key not in fields:
            fields[instance.key] = _symbol_fields(
                instance.symbol, rules_for_version(record.version), record.version
            )
        units.setdefault(text.upper(), []).append(
            _Unit(
                text,
                instance.unit,
                instance.unit_count,
                fields[instance.key],
                _variant_fields(instance.path),
            )
        )
    return units, problems


def unit_conflicts(
    project: Path, variants: list[str] | None = None
) -> tuple[list[UnitConflict], list[str]]:
    """Return the fields with different values in the units of one reference, and
    the problems of the sheet hierarchy (a sheet that the check could not read).

    The check uses the default variant, and each name of variants (KiCad 10).
    It compares Value and each MPN field that a unit of the reference has. A
    unit with no such field, or with an empty one, has the value "". A variant
    gives only the conflicts that the default variant does not give.

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
        KicadFileError: A file has a version that the server does not know.
        ValueError: A unit is not a number.
    """
    units, problems = _units(project)
    conflicts: list[UnitConflict] = []
    default: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for key in sorted(units):
        group = units[key]
        if len(group) > 1 and (
            len({unit.unit for unit in group}) < len(group)
            or len({unit.unit_count for unit in group}) > 1
        ):
            problems.append(
                f"{group[0].reference}: a duplicate reference (the same unit two times, or "
                "different unit counts): KiCad ERC gives it, and the tool did not compare its fields."
            )
    for variant in ["", *(variants or [])]:
        for key in sorted(units):
            group = units[key]
            if (
                len(group) < 2
                or len({unit.unit for unit in group}) < len(group)
                or len({unit.unit_count for unit in group}) > 1
            ):
                continue
            reference = group[0].reference
            fields = [
                {**unit.fields, **(unit.variants.get(variant, {}) if variant else {})}
                for unit in group
            ]
            names = list(dict.fromkeys(name for f in fields for name in f))
            for name in names:
                values = tuple(
                    (letter_reference(unit.unit), f.get(name, ""))
                    for unit, f in sorted(zip(group, fields, strict=True), key=lambda u: u[0].unit)
                )
                if len({value for _unit, value in values}) < 2:
                    continue
                key = (reference, name, values)
                if not variant:
                    default.add(key)
                elif key in default:
                    continue  # the variant gives the conflict of the default variant
                conflicts.append(UnitConflict(reference, variant, name, values))
    return conflicts, problems
