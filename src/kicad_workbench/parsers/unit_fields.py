"""The fields of the units of one symbol: one package has one Value, MPN and
Value_Confirmed.

The units of one reference (U301A and U301B) are one package and one line of
the BOM. KiCad gives an ERC error when the units have different values
(ERCE_DIFFERENT_UNIT_VALUE, SCH_REFERENCE_LIST::CheckAnnotation) or different
footprints (ERCE_DIFFERENT_UNIT_FP, ERC_TESTER::TestMultiunitFootprints). An
MPN field is a field of the user: KiCad does not compare it. In the BOM,
KiCad 9 and 10 join the different values of the units with a comma
("LM358NG,LM358P"), which looks like an MPN and its alternative, and KiCad 8
writes "-- mixed values --" (FIELDS_EDITOR_GRID_DATA_MODEL::getValue).

This module compares Value, the MPN fields and Value_Confirmed of the units of
each reference (decision of the user, 2026-11-05: an empty field in one unit is a
difference too). Value_Confirmed is the confirmed copy of Value (the model of the
confirmed MPNs, parsers/confirmed_mpns.py): the BOM line joins its values too. It
reads each schematic file of the sheet hierarchy:

- KiCad keeps the reference and the unit of a placed symbol for each sheet
  path, in (instances (project ... (path "<sheet path>" (reference ...)
  (unit ...))). A sheet that the hierarchy uses more than one time gives each
  of its symbols one reference for each sheet path. The references are the
  references of kicad-cli (the BOM) of the installed KiCad, after the load
  (parsers/sch_instances.py).
- KiCad groups the units by the reference, with no difference of case
  (SCH_REFERENCE::CompareRef). A reference with no number after
  SCH_REFERENCE::Split ("U?", or "U" with no digit) is no group: its units are
  different lines of the BOM, as "U?" (FIELDS_EDITOR_GRID_DATA_MODEL). A power
  symbol ("#PWR01") is not in the BOM.
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

from .annotation import split_reference
from .kicad_versions import file_text
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


def field_key(name: str) -> str:
    """Return the name of a field or a BOM column with no case, spaces, "_", "." and
    "-", as datasheet-lens compares the column names (_column_key)."""
    return re.sub(r"[\s_.\-]", "", name).lower()


_MPN_KEYS = {field_key(name) for name in MPN_FIELDS}
# The hidden field of the confirmed Value (parsers/confirmed_mpns.py).
CONFIRMED_FIELD = "Value_Confirmed"


def is_mpn_field(name: str) -> bool:
    """Return True for the name of an MPN field (see MPN_FIELDS)."""
    return field_key(name) in _MPN_KEYS


def mpn_order(names: list[str]) -> list[int]:
    """Return the indexes of the MPN fields of names, in the order of MPN_FIELDS (MPN
    first, also in an other spelling), as datasheet-lens orders the MPN columns."""
    keys = [field_key(name) for name in names]
    return [i for wanted in MPN_FIELDS for i, key in enumerate(keys) if key == field_key(wanted)]


def is_confirmed_field(name: str) -> bool:
    """Return True for the name of the field Value_Confirmed (see field_key)."""
    return field_key(name) == field_key(CONFIRMED_FIELD)


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
    fields: dict[str, str]  # Value, the MPN fields and Value_Confirmed of the symbol
    variants: dict[str, dict[str, str]]  # variant name -> the fields that it sets


def _symbol_fields(symbol: Node, value: str, version: int) -> dict[str, str]:
    """Return Value, the MPN fields and Value_Confirmed of a placed symbol, as KiCad
    reads them.

    value: The Value field after the load (PlacedSymbol.value): the parser sets it
        also from the (value ...) of an instance.
    """

    fields = {"Value": value}
    for prop in symbol.children("property"):
        field = property_field(prop)
        if field is not None and (is_mpn_field(field[0]) or is_confirmed_field(field[0])):
            fields.setdefault(field[0], file_text(field[1], version))
    return fields


def _variant_fields(path: Node | None) -> dict[str, dict[str, str]]:
    """Return the fields of the variants of an instance: Value, the MPN fields and
    Value_Confirmed."""
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
            if text == "Value" or is_mpn_field(text) or is_confirmed_field(text):
                fields[text] = value.value(0, "")
    return variants


def _units(project: Path, version: str) -> tuple[dict[str, list[_Unit]], list[str]]:
    """Return the units of each annotated reference of the project (the key is the
    reference in upper case), and the problems.

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
        KicadFileError: A file has a version that the server does not know.
        KicadParseError: The parser of KiCad stops in a sub-sheet (build_sheet_list).
        InputError: A unit is not a number, KiCad has no verified rules for the
            installed version, or KiCad cannot load the root schematic.
    """
    instances, files, problems = placed_instances(project, version, "cli")
    fields: dict[tuple[Path, int], dict[str, str]] = {}  # each symbol one time
    units: dict[str, list[_Unit]] = {}
    for instance in instances:
        text = instance.reference
        if text.startswith("#") or split_reference(text).number < 0:
            continue
        record = files[instance.file]
        if instance.key not in fields:
            fields[instance.key] = _symbol_fields(instance.symbol, instance.value, record.version)
        units.setdefault(text.upper(), []).append(
            _Unit(
                text,
                instance.unit,
                instance.unit_count,
                fields[instance.key],
                # KiCad 10 copies the variants of an instance for a sheet path with
                # no instance (CheckForMissingSymbolInstances).
                _variant_fields(instance.variants),
            )
        )
    return units, problems


@dataclass
class UnitCheck:
    """The result of variant_unit_conflicts."""

    # All conflicts of the default variant ("") and of each variant.
    conflicts: dict[str, list[UnitConflict]]
    hierarchy: list[str]  # the problems of the sheet hierarchy (a sheet not read)
    duplicates: list[str]  # the duplicate references: their fields are not compared
    # The MPN fields and the fields Value_Confirmed of each reference (in upper case),
    # also the empty ones, in the order of the fields of its units.
    fields: dict[str, list[str]]

    @property
    def problems(self) -> list[str]:
        """The problems of the hierarchy and the duplicate references, as text."""
        return self.hierarchy + [
            f"{reference}: a duplicate reference (the same unit two times, or different unit "
            "counts): KiCad ERC gives it, and the tool did not compare its fields."
            for reference in self.duplicates
        ]


def variant_unit_conflicts(
    project: Path, version: str, variants: list[str] | None = None
) -> UnitCheck:
    """Return all fields with different values in the units of one reference for the
    default variant ("") and for each name of variants (KiCad 10), with the
    references of kicad-cli of the installed KiCad version, the problems of
    the sheet hierarchy (a sheet that the check could not read), and the duplicate
    references.

    It compares Value, each MPN field and Value_Confirmed that a unit of the reference
    has. A unit with no such field, or with an empty one, has the value "". The
    fields of a variant are the fields of the symbol with the fields of its variant:
    a variant can also make the fields of the units equal.

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
        KicadFileError: A file has a version that the server does not know.
        KicadParseError: The parser of KiCad stops in a sub-sheet (build_sheet_list).
        InputError: A unit is not a number, KiCad has no verified rules for the
            installed version, or KiCad cannot load the root schematic.
    """
    units, problems = _units(project, version)
    duplicates = [
        group[0].reference
        for group in (units[key] for key in sorted(units))
        if len(group) > 1
        and (
            len({unit.unit for unit in group}) < len(group)
            or len({unit.unit_count for unit in group}) > 1
        )
    ]
    conflicts: dict[str, list[UnitConflict]] = {}
    for variant in ["", *(variants or [])]:
        found = conflicts.setdefault(variant, [])
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
                if len({value for _unit, value in values}) > 1:
                    found.append(UnitConflict(reference, variant, name, values))
    fields = {
        key: list(dict.fromkeys(name for unit in group for name in unit.fields if name != "Value"))
        for key, group in units.items()
    }
    return UnitCheck(conflicts, problems, duplicates, fields)


def unit_conflicts(
    project: Path, version: str, variants: list[str] | None = None
) -> tuple[list[UnitConflict], list[str]]:
    """Return the fields with different values in the units of one reference, and
    the problems of the sheet hierarchy (see variant_unit_conflicts).

    The check uses the default variant, and each name of variants (KiCad 10). A
    variant gives only the conflicts that the default variant does not give.

    Raises:
        KiCadEncodingError, SExprError, OSError: A file cannot be read.
        KicadFileError: A file has a version that the server does not know.
        KicadParseError: The parser of KiCad stops in a sub-sheet (build_sheet_list).
        InputError: A unit is not a number, KiCad has no verified rules for the
            installed version, or KiCad cannot load the root schematic.
    """
    check = variant_unit_conflicts(project, version, variants)
    return distinct_conflicts(check.conflicts), check.problems


def distinct_conflicts(by_variant: dict[str, list[UnitConflict]]) -> list[UnitConflict]:
    """Return the conflicts of the default variant, then the conflicts of each
    variant that the default variant does not give (see variant_unit_conflicts)."""
    default = {(c.reference, c.field, c.values) for c in by_variant.get("", [])}
    conflicts = list(by_variant.get("", []))
    for variant, found in by_variant.items():
        if variant:
            conflicts += [c for c in found if (c.reference, c.field, c.values) not in default]
    return conflicts
