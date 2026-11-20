"""The tools set_symbol_fields and set_symbol_attributes: write the fields and the
attributes of placed symbols, as KiCad does.

The rules come from the KiCad source (branch 10.0, tags 8.0.9 and 9.0.9). See
plans/roadmap.md, "Phase 4", "Step 1":

- KiCad keeps the fields of a placed symbol for all instances of its sheet
  (SCH_SYMBOL_INSTANCE holds only the reference, the unit and the variants).
  Thus a field of a symbol in a sheet that the hierarchy uses more than one
  time is the field of each reference of that symbol (U201 and U301).
- One package has one Value and one MPN: the tool writes each field into each
  unit of the reference (user, 2026-11-05).
- SCH_SYMBOL::GetField finds a field by its exact name. KiCad 10 reads a
  mandatory field (Value, Footprint, ...) with no difference of case.
- A variant of KiCad 10 can set only a field that the symbol has
  (SCH_SYMBOL::SetFieldText). A text equal to the text of the field removes
  the field from the variant; an other text sets it. SCH_IO_KICAD_SEXPR
  writes the fields of a variant sorted by name, and no variant with no
  differentials.
- A new field has the angle of the Reference field, the position of the
  symbol and the default text size of the schematic
  (DIALOG_SYMBOL_PROPERTIES::OnAddField, SCH_FIELD). KiCad 9 and 10 make a
  new field hidden, KiCad 8 does not: the tool does as the KiCad of the file
  version (user, 2026-11-05: as KiCad). Value_Confirmed and MPN_Confirmed are
  always hidden, also in an other spelling (field_key): the model of confirmed
  MPNs of datasheet-lens, decisions 5 and 27.
- The attributes (DNP, Exclude from BOM, board, position files, simulation; KiCad
  10.0.6, plans/roadmap.md, "Step 8"): SCH_IO_KICAD_SEXPR::saveSymbol writes the five
  tokens of the symbol each time, and a token of a variant only when it differs from
  the symbol. The parser starts each variant with the attributes of the symbol
  (SCH_SYMBOL_VARIANT::InitializeAttributes): a change of the symbol keeps the old
  value in each variant. SyncOtherUnits of the dialog does not give the other units
  Exclude from simulation: the tool sets each unit.
- The format of the file version (SCH_IO_KICAD_SEXPR::saveField): before
  SCHEMATIC_FIELD_FLAGS the hidden state is in the effects, from it in the
  property. Variants exist from SCHEMATIC_VARIANTS. A file older than
  EMPTY_TEXT_MARKER reads "~" as an empty field.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..errors import InputError
from ..parsers.annotation import kicad_major, split_reference
from ..parsers.kicad_fields import (
    SIM_LEGACY_FIELDS,
    SYMBOL_FIELD_NAMES,
    WX_SPACE,
    is_mandatory_property,
    symbol_field_props,
)
from ..parsers.kicad_versions import (
    EMPTY_TEXT_MARKER,
    KICAD8,
    SCHEMATIC_FIELD_FLAGS,
    SCHEMATIC_VARIANT_IN_BOM,
    SCHEMATIC_VARIANTS,
    KicadFileError,
    file_text,
)
from ..parsers.sch_geometry import (
    IU_PER_MIL,
    kiround,
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
    property_atoms,
    property_field,
    quote,
    removal,
)
from ..parsers.sheet_list import screens_by_file
from ..parsers.symbol_info import letter_reference
from ..parsers.unit_fields import is_confirmed_field, is_mpn_confirmed_field
from ..server import mcp
from ..utils.file_versions import with_schematic_versions
from ..utils.footprint_filter import footprint_id_problem
from ..utils.kicad_cli import get_kicad_version, installed_kicad_problem, kicad_10_problem
from ..utils.kicad_lock import write_schematics
from ..utils.kicad_text import KiCadEncodingError, read_kicad_json
from ..utils.library_tables import kicad_environment
from ..utils.sch_hierarchy import AmbiguousRootError, schematic_project_file
from ..utils.tool_errors import error_answer
from ..utils.variants import file_variants, project_variants
from .library import cell
from .schematic_editor import _field_error, _mm

# DEFAULT_TEXT_SIZE of eeschema/default_values.h, in mils. The limits of the
# project setting (SCHEMATIC_SETTINGS, PARAM_SCALED): 5 to 1000 mils.
_DEFAULT_TEXT_SIZE_MILS = 50
_TEXT_SIZE_LIMITS_MILS = (5, 1000)
_IU_PER_MM = 10000


class FieldChange(BaseModel):
    """The fields to write into one reference."""

    what: ClassVar[str] = "fields"

    reference: str = Field(description="The reference of the symbol, for example U3.")
    fields: dict[str, str] = Field(
        description=(
            'The fields to write, by name, for example {"MPN_Confirmed": "LM358DR", '
            '"Value_Confirmed": "LM358"}. A field that the symbol does not have is a new field, hidden '
            "as KiCad 9 and 10 make it (not in a variant). Not Reference."
        )
    )
    variant: str = Field(
        default="",
        description=(
            "The name of a variant of the project (KiCad 10): the tool writes the fields of the "
            "variant. The symbol must have each field. Empty: the fields of the symbol."
        ),
    )

    def payload(self) -> object:
        """What the change writes: two references of one symbol must have the same."""
        return self.fields


class SymbolAttributes(BaseModel):
    """The attributes of the Symbol Properties dialog. A missing attribute does not change."""

    # An other name (for example the token in_bom of the file) is an error, not a change
    # that the tool does not write.
    model_config = ConfigDict(extra="forbid")

    dnp: bool | None = Field(default=None, description='"Do not populate".')
    exclude_from_bom: bool | None = Field(
        default=None, description='"Exclude from bill of materials".'
    )
    exclude_from_board: bool | None = Field(default=None, description='"Exclude from board".')
    exclude_from_position_files: bool | None = Field(
        default=None, description='"Exclude from position files".'
    )
    exclude_from_simulation: bool | None = Field(
        default=None, description='"Exclude from simulation".'
    )


class AttributeChange(BaseModel):
    """The attributes to write into one reference."""

    model_config = ConfigDict(extra="forbid")
    what: ClassVar[str] = "attributes"

    reference: str = Field(description="The reference of the symbol, for example U3.")
    attributes: SymbolAttributes = Field(description="The attributes to set.")
    variant: str = Field(
        default="",
        description=(
            "The name of a variant of the project (KiCad 10): the tool writes the attributes of "
            "the variant. Empty: the attributes of the symbol."
        ),
    )

    def payload(self) -> object:
        """What the change writes: two references of one symbol must have the same."""
        return self.attributes


class FootprintChange(BaseModel):
    """The footprint to write into the Footprint field of one reference, as the save of
    CvPcb does it (assign_footprint)."""

    model_config = ConfigDict(extra="forbid")
    what: ClassVar[str] = "footprint"
    # CvPcb writes the field of the symbol, never the field of a variant.
    variant: ClassVar[str] = ""

    reference: str
    footprint: str  # "<nickname>:<name>", or "" to remove the footprint.

    def payload(self) -> object:
        """What the change writes: two references of one symbol must have the same."""
        return self.footprint


# The attributes of a placed symbol: (the name of SymbolAttributes, the token of the
# file, True when the token has the inverted logic: "in_bom yes" is "not excluded"), in
# the order of the tokens of a variant (SCH_IO_KICAD_SEXPR::saveSymbol).
ATTRIBUTES = (
    ("dnp", "dnp", False),
    ("exclude_from_simulation", "exclude_from_sim", False),
    ("exclude_from_bom", "in_bom", True),
    ("exclude_from_board", "on_board", True),
    ("exclude_from_position_files", "in_pos_files", True),
)
_TOKENS = {token: (name, inverted) for name, token, inverted in ATTRIBUTES}


def _last(node: Node, head: str) -> Node | None:
    """The last child with the head: the parser sets the attribute from each token, thus
    the last one stays."""
    children = node.children(head)
    return children[-1] if children else None


def symbol_tokens(symbol: Node, where: str) -> dict[str, bool]:
    """The attribute tokens of a placed symbol as the parser reads them: with no token,
    the attribute is false (the SYMBOL constructor), thus an inverted token is true."""
    tokens = {token: inverted for _name, token, inverted in ATTRIBUTES}
    for token in tokens:
        node = _last(symbol, token)
        if node is not None:
            tokens[token] = parse_bool(node, where)
    return tokens


def _field_text(prop: Node, record: SchematicFile) -> str:
    """Return the text of a field: "~" is empty only in a file older than
    EMPTY_TEXT_MARKER (parseSchField). KiCad 10: SCH_FIELD::SetText removes the white
    space at the two ends of the text of a mandatory field (kicad_fields.mandatory_text).
    """
    field = property_field(prop)
    text = file_text(field[1] if field else "", record.version)
    if record.rules.kicad == 10 and is_mandatory_property(prop, record.rules):
        return text.strip(WX_SPACE)
    return text


def default_text_size(project: Path, kicad: str | None) -> float:
    """Return the default text size of a schematic in mm (SCHEMATIC_SETTINGS::m_DefaultTextSize).

    The project setting schematic.drawing.default_text_size (mils, 5 to 1000),
    else the setting drawing.default_text_size of eeschema.json of the
    installed KiCad, else 50 mils.
    """
    mils: float = _DEFAULT_TEXT_SIZE_MILS
    if kicad is not None:
        app = kicad_environment(kicad).config_dir / "eeschema.json"
        value = _json_number(app, "drawing", "default_text_size")
        if value is not None:
            mils = value
    value = _json_number(project, "schematic", "drawing", "default_text_size")
    if value is not None:
        iu = kiround(value * IU_PER_MIL)
        low, high = (limit * IU_PER_MIL for limit in _TEXT_SIZE_LIMITS_MILS)
        if low <= iu <= high:
            return iu / _IU_PER_MM
    return kiround(mils * IU_PER_MIL) / _IU_PER_MM


def _json_number(path: Path, *keys: str) -> float | None:
    if not path.is_file():
        return None
    try:
        data = read_kicad_json(path)
    except (OSError, InputError):
        return None
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    if isinstance(data, int | float) and not isinstance(data, bool):
        return float(data)
    return None


def _new_property(symbol: Node, name: str, value: str, size: float, version: int) -> str:
    """Return a new field of a placed symbol, as the symbol properties dialog of
    the KiCad of the file version adds it, and SCH_IO_KICAD_SEXPR::saveField
    writes it: one list on each line (see _insert_after). The field is hidden
    from KiCad 9 (KiCad 8 does not hide it), and Value_Confirmed and MPN_Confirmed
    always.

    Before SCHEMATIC_FIELD_FLAGS, KiCad writes the hidden state in the effects
    (EDA_TEXT::Format of KiCad 8, SCH_FIELD::Format of KiCad 9); from it, KiCad
    writes hide, show_name and do_not_autoplace in the property.
    """
    at = symbol.first("at")
    x, y = (at.number(0, 0.0), at.number(1, 0.0)) if at else (0.0, 0.0)
    angle = "0"
    for prop in symbol.children("property"):
        field = property_field(prop)
        if field is not None and field[0] == "Reference":
            reference_at = prop.first("at")
            angle = reference_at.value(2, "0") if reference_at is not None else "0"
    hidden = is_confirmed_field(name) or is_mpn_confirmed_field(name) or version > KICAD8.schematic
    text_size = _mm(size)
    font = f"(effects\n(font\n(size {text_size} {text_size})\n)"
    head = f"(property {quote(name)} {quote(value)}\n(at {_mm(x)} {_mm(y)} {angle})"
    if version >= SCHEMATIC_FIELD_FLAGS:
        hide = "\n(hide yes)" if hidden else ""
        return f"{head}{hide}\n(show_name no)\n(do_not_autoplace no)\n{font}\n)\n)"
    # KiCad writes the effects of a field also with the default size and
    # formatting: the files of KiCad 8 (40 visible fields of 1.27 mm in the demo
    # complex_hierarchy) and of KiCad 10 (spike/kicad-versions/fields_check.py).
    if hidden:
        return f"{head}\n{font}\n(hide yes)\n)\n)"
    return f"{head}\n{font}\n)\n)"


def _depth(node: Node) -> int | None:
    """Return the number of tabs before a node that starts its line, else None."""
    source = node.source
    lead = source[source.rfind("\n", 0, node.start) + 1 : node.start]
    return len(lead) if set(lead) <= {"\t"} else None


def _one_line(text: str) -> str:
    return " ".join(line.strip() for line in text.split("\n"))


def _insert_after(node: Node, text: str) -> tuple[int, int, str]:
    """Return the edit that puts a list (one list on each line) after a node: on
    a new line with the indent of the node, as KiCad writes the file. When the
    node does not start its line, the list goes on the same line."""
    depth = _depth(node)
    if depth is None:
        return (node.end, node.end, " " + _one_line(text))
    return (node.end, node.end, "\n" + indent(text, depth))


def _replacement(node: Node, text: str) -> str:
    """Return a list (one list on each line) that replaces a node."""
    depth = _depth(node)
    return _one_line(text) if depth is None else indent(text, depth).lstrip("\t")


def _variant_fields(variant: Node | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in variant.children("field") if variant is not None else []:
        name, value = item.first("name"), item.first("value")
        if name is not None and value is not None:
            fields[name.value(0, "")] = value.value(0, "")
    return fields


@dataclass
class _Row:
    reference: str
    units: str
    variant: str
    field: str
    before: str
    after: str
    file: Path
    # The reference whose symbol got the field already: this row is an other
    # reference of the same symbol (a shared sheet).
    shared: str = ""


def _check_change(change: "Change", known_variants: set[str]) -> None:
    """Refuse a change that KiCad cannot keep as the change gives it.

    Raises:
        InputError: The reason.
    """
    # SCH_REFERENCE::Split: a reference that ends with "?" or with no digit is not
    # annotated ("U?", "U"), as CheckAnnotation finds it.
    if not change.reference or split_reference(change.reference).new:
        raise InputError(
            f"The reference {change.reference!r} is not annotated. Annotate the schematic in "
            "KiCad first."
        )
    if isinstance(change, FootprintChange):
        if change.reference.startswith("#"):
            # SCH_SHEET_PATH::AppendSymbol (SYMBOL_FILTER_NON_POWER) and the netlist
            # leave out each reference that starts with "#".
            raise InputError(
                f"{change.reference} is a power symbol or an other pseudo symbol (its reference "
                'starts with "#"): CvPcb does not list it, and it gets no footprint on the board.'
            )
        if change.footprint and (problem := footprint_id_problem(change.footprint)):
            raise InputError(f"{change.reference}: {problem}")
        return
    if isinstance(change, AttributeChange):
        if all(getattr(change.attributes, name) is None for name, _t, _i in ATTRIBUTES):
            raise InputError(f"The change of {change.reference} has no attributes.")
        _check_variant(change, known_variants)
        return
    if not change.fields:
        raise InputError(f"The change of {change.reference} has no fields.")
    mandatory = {name.lower(): name for name in SYMBOL_FIELD_NAMES}
    for name, value in change.fields.items():
        if not name:
            raise InputError(f"A field of {change.reference} has no name.")
        if name.lower() == "reference":
            raise InputError(
                f"The tool does not write the Reference ({change.reference}). Annotate in KiCad."
            )
        canonical = mandatory.get(name.lower())
        if canonical is not None and name != canonical:
            # KiCad 10 reads a mandatory field with no difference of case, also in an
            # older file (parseSchField): "value" would replace the Value.
            raise InputError(f"Give the field name {canonical!r}, not {name!r}.")
        if name in SIM_LEGACY_FIELDS:
            raise InputError(
                f"KiCad removes the field {name!r} when it loads the symbol and sets "
                '"exclude from simulation" from its text. Set that flag in KiCad.'
            )
        error = _field_error(name, value) or _field_error("field name", name)
        if error:
            raise InputError(error.removeprefix("❌ "))
    _check_variant(change, known_variants)


def _check_variant(change: "Change", known_variants: set[str]) -> None:
    """Refuse a variant that the project does not have."""
    if change.variant and change.variant not in known_variants:
        listed = ", ".join(repr(n) for n in sorted(known_variants)) or "none"
        raise InputError(
            f"The project has no variant {change.variant!r}. Its variants: {listed}. Make the "
            "variant in KiCad first."
        )


# A variant: (its attribute tokens in the file, its fields).
_Variant = tuple[dict[str, bool], dict[str, str]]


@dataclass
class _Variants:
    """The variants of one instance (one (path ...)): the original and the new state."""

    label: str  # the reference, for the messages
    file: Path
    path: Node  # the (path ...) of the instance
    symbol: tuple[str, int]  # the key of the symbol (PlacedSymbol.key)
    symbol_node: Node  # the placed symbol that holds the path
    nodes: list[Node]  # the (variant ...) nodes of the file, in their order
    original: dict[str, _Variant]
    new: dict[str, _Variant]


def read_variants(
    label: str, record: SchematicFile, path: Node, symbol: tuple[str, int], symbol_node: Node
) -> _Variants:
    """The variants of a (path ...). The parser starts each variant with the attributes
    of the symbol (SCH_SYMBOL_VARIANT::InitializeAttributes): a token of the file
    changes one (the last token of a name stays); variant_edit adds the attributes of
    the symbol.

    Raises:
        InputError: A token is not yes or no, or (in_bom ...) of a variant has the
            inverted logic of a file before SCHEMATIC_VARIANT_IN_BOM (the tool writes
            only the format of the installed KiCad).
    """
    nodes = path.children("variant")
    original: dict[str, _Variant] = {}
    for node in nodes:
        name = node.first("name")
        if name is None:
            continue
        tokens = {
            item.head: parse_bool(item, f"a variant of {label}")
            for item in node.children()
            if item.head in _TOKENS
        }
        if "in_bom" in tokens and record.version < SCHEMATIC_VARIANT_IN_BOM:
            raise InputError(
                f"The variant {name.value(0, '')!r} of {label} in {record.file.name} has the "
                f"(in_bom ...) of an older format (file version {record.version}), which "
                "KiCad reads with the inverted logic. Open and save the schematic in KiCad "
                "10 first."
            )
        original[name.value(0, "")] = (tokens, _variant_fields(node))
    new = {name: (dict(tokens), dict(fields)) for name, (tokens, fields) in original.items()}
    return _Variants(label, record.file, path, symbol, symbol_node, nodes, original, new)


def _instance_variants(instance: PlacedInstance, record: SchematicFile) -> _Variants:
    assert instance.path is not None
    return read_variants(instance.reference, record, instance.path, instance.key, instance.symbol)


def _variant_text(
    name: str, variant: _Variant, before: dict[str, bool], after: dict[str, bool]
) -> str:
    """Return a (variant ...) with one list on each line, or "" when it has no
    differentials (SCH_IO_KICAD_SEXPR writes no such variant).

    Args:
        before: The attribute tokens of the symbol when KiCad loaded the file: the
            start of each variant (InitializeAttributes).
        after: The attribute tokens of the symbol after the change: KiCad writes a token
            of the variant only when it differs from it.
    """
    tokens, fields = variant
    parts = []
    for _name, token, _inverted in ATTRIBUTES:
        value = tokens.get(token, before[token])
        if value != after[token]:
            parts.append(f"({token} {'yes' if value else 'no'})")
    if not parts and not fields:
        return ""
    parts = [f"(name {quote(name)})", *parts]
    parts += [
        f"(field\n(name {quote(k)})\n(value {quote(v)})\n)" for k, v in sorted(fields.items())
    ]
    return "(variant\n" + "\n".join(parts) + "\n)"


def _variants_text(
    variants: dict[str, _Variant], before: dict[str, bool], after: dict[str, bool]
) -> str:
    """The text of all variants of an instance, sorted by name (std::map)."""
    return "\n".join(
        t for name in sorted(variants) if (t := _variant_text(name, variants[name], before, after))
    )


def variant_edit(
    state: _Variants, before: dict[str, bool], after: dict[str, bool]
) -> tuple[int, int, str] | None:
    """Return the one edit that writes all variants of an instance, sorted by name
    as SCH_IO_KICAD_SEXPR writes them (std::map), or None when they do not change.
    One edit for all variants: two edits next to each other cannot overlap.

    Args:
        before, after: The attribute tokens of the symbol (_variant_text).

    Raises:
        InputError: The variants are not next to each other in the instance.
    """
    text = _variants_text(state.new, before, after)
    # A change that gives the same variants (for example a value equal to the value that
    # the variant has from the symbol) is no edit.
    if text == _variants_text(state.original, before, before):
        return None
    path = state.path
    children = path.children()
    if state.nodes:
        first, last = children.index(state.nodes[0]), children.index(state.nodes[-1])
        if last - first + 1 != len(state.nodes):
            raise InputError(
                f"The variants of {state.label} in {state.file.name} are "
                "not next to each other. Open and save the schematic in KiCad first."
            )
        if text:
            return (state.nodes[0].start, state.nodes[-1].end, _replacement(state.nodes[0], text))
        # With the blanks before the first variant.
        return removal(path.source, state.nodes[0].start, state.nodes[-1].end)
    if not text:
        return None
    if children:
        return _insert_after(children[-1], text)
    return (path.end - 1, path.end - 1, " " + _one_line(text))


Change = FieldChange | AttributeChange | FootprintChange


@dataclass
class _Plan:
    """The state of a plan: the edits of each file, the rows of the output, the
    warnings, and the attribute tokens of each changed symbol."""

    files: dict[Path, SchematicFile]
    edits: dict[Path, list[tuple[int, int, str]]]
    rows: list[_Row]
    # The texts of the fields of each symbol after the changes of the symbols: the
    # base of the variants (a text equal to it is no differential).
    base: dict[tuple[str, int], dict[str, str]]
    variants: dict[tuple[Path, int], _Variants]  # by (file, offset of the path)
    # The attribute tokens of each symbol whose attributes change: (when KiCad loaded
    # the file, after the change).
    tokens: dict[tuple[str, int], tuple[dict[str, bool], dict[str, bool]]]


def _plan(
    project: Path, kicad: str, changes: list[Change], size: float
) -> tuple[dict[Path, str], list[_Row], dict[Path, SchematicFile], list[str]]:
    """Return the new text of each file to write, the rows of the output, the
    files, and the warnings.

    Raises:
        InputError: A change cannot be written (the message tells why).
    """
    # The references of the schematic editor of the installed KiCad (user, 2026-11-05).
    instances, screens, problems = placed_instances(project, kicad, "gui")
    if problems:
        # A sheet that the tool cannot read can hold an other reference of a symbol
        # (a shared sheet): the check of _check_shared would not be complete.
        raise InputError(
            "The tool cannot read the full sheet hierarchy, thus it cannot find each "
            "reference of a symbol: " + " ".join(problems) + " Correct it in KiCad first."
        )
    files, shared = screens_of_files(list(screens.values()))
    known_variants: set[str] = set()
    if any(c.variant for c in changes):
        known_variants = project_variants(project).union(
            *(file_variants(f.root) for f in screens.values())
        )
    by_key: dict[tuple[str, str], Change] = {}
    for change in changes:
        _check_change(change, known_variants)
        key = (change.reference.upper(), change.variant)
        if key in by_key:
            raise InputError(f"Two changes of {change.reference}{_variant_label(change.variant)}.")
        by_key[key] = change
    groups: dict[str, list[PlacedInstance]] = {}
    references: dict[tuple[str, int], set[str]] = {}  # more than one in a shared sheet
    for instance in instances:
        groups.setdefault(instance.reference.upper(), []).append(instance)
        references.setdefault(instance.key, set()).add(instance.reference)

    plan = _Plan(files, {}, [], {}, {}, {})
    warnings: list[str] = []
    done: dict[tuple[str, int], list[_Row]] = {}  # the symbols that got their changes
    for (key, variant), change in sorted(by_key.items(), key=lambda item: item[0][1] != ""):
        group = groups.get(key)
        if not group:
            raise InputError(f"The project has no symbol with the reference {change.reference}.")
        reference = group[0].reference
        if len({i.unit for i in group}) < len(group) or len({i.unit_count for i in group}) > 1:
            raise InputError(
                f"{reference} is a duplicate reference (the same unit two times, or different unit "
                "counts). Annotate the schematic in KiCad first."
            )
        for instance in sorted(group, key=lambda i: i.unit):
            # The letters of the unit of a package with more units (U1A), else empty.
            unit_text = letter_reference(instance.unit) if instance.unit_count > 1 else ""
            _check_one_screen(instance, shared, change.what)
            record = files[instance.file]
            if variant:
                if isinstance(change, AttributeChange):
                    _plan_attribute_variant(change, instance, record, plan, unit_text)
                else:
                    _plan_variant(
                        change, instance, record, plan.base, plan.variants, plan.rows, unit_text
                    )
                continue
            _check_shared(change, instance, references, by_key)
            if isinstance(change, FootprintChange):
                # The variants belong to each instance (sheet path), also of one symbol.
                warnings += _footprint_variants(instance)
            if instance.key in done:
                plan.rows += [
                    _Row(reference, unit_text, "", r.field, r.before, r.after, r.file, r.reference)
                    for r in done[instance.key]
                ]
                continue
            start = len(plan.rows)
            if isinstance(change, FootprintChange):
                _plan_footprint_symbol(change, instance, record, plan, unit_text)
            elif isinstance(change, AttributeChange):
                _plan_attribute_symbol(change, instance, record, plan, unit_text)
            else:
                _plan_symbol(
                    change, instance, record, size, plan.base, plan.edits, plan.rows, unit_text
                )
            done[instance.key] = plan.rows[start:]
            if any(r.before != r.after for r in plan.rows[start:]):
                warnings += _other_projects(instance, project.stem, change.what)
    for state in plan.variants.values():
        before, after = plan.tokens.get(state.symbol, (None, None))
        if before is None:
            before = after = symbol_tokens(state.symbol_node, state.label)
        edit = variant_edit(state, before, after)
        if edit is not None:
            plan.edits.setdefault(state.file, []).append(edit)
    texts = {}
    for path, file_edits in plan.edits.items():
        if file_edits:
            root = files[path].root
            texts[path] = (
                root.source[: root.start] + edited(root, file_edits) + root.source[root.end :]
            )
    return texts, plan.rows, files, list(dict.fromkeys(warnings))


def screens_of_files(screens: list[SchematicFile]) -> tuple[dict[Path, SchematicFile], set[str]]:
    """Return the screens by their file, and the keys (Screen.key) of the screens of a
    file of the disk with more than one screen.

    KiCad loads a file under each name that the sheets give it (sheet_file_names: for
    example a symbolic link, or "/./" in an absolute name) as a screen of its own.
    Each screen has a copy of the symbols, and the save of one screen removes the
    changes of the other in the file.
    """
    shared = {
        screen.key
        for group in screens_by_file(screens).values()
        if len(group) > 1
        for screen in group
    }
    return {screen.file: screen for screen in screens if screen.key not in shared}, shared


def _check_one_screen(instance: PlacedInstance, shared: set[str], what: str) -> None:
    """Refuse a change of a symbol of a file with more than one screen (screens_of_files).

    Raises:
        InputError: The file of the symbol has more than one screen.
    """
    if instance.screen.key in shared:
        raise InputError(
            f"KiCad loads the file {instance.file} of {instance.reference} under more than "
            "one name: KiCad keeps a copy of its symbols for each name, and the save of one "
            f"copy removes the changes of the other. The tool does not change the {what} "
            "of such a file. Give the file one name in all sheets."
        )


def _other_projects(instance: PlacedInstance, project_name: str, what: str) -> list[str]:
    """Return a warning when the symbol has instances of an other project: KiCad
    keeps the fields of the symbol for that project too (user, 2026-11-05: write
    and warn)."""
    instances = instance.symbol.first("instances")
    names = sorted(
        {
            name
            for node in (instances.children("project") if instances else [])
            if (name := node.value(0, "")) and name != project_name
        }
    )
    if not names:
        return []
    return [
        f"The file {instance.file.name} has instances of the projects {', '.join(names)}: "
        f"KiCad keeps the {what} of {instance.reference} for them too (it can be an old "
        "copy of the sheet)."
    ]


def _variant_label(variant: str) -> str:
    return f" (variant {variant!r})" if variant else ""


def _check_shared(
    change: "Change",
    instance: PlacedInstance,
    references: dict[tuple[str, int], set[str]],
    by_key: dict[tuple[str, str], "Change"],
) -> None:
    """Refuse a change of a symbol that an other reference uses too (a sheet that
    the hierarchy uses more than one time), when the changes do not give that
    reference the same fields or attributes (user, 2026-11-05)."""
    what = change.what
    others = sorted(
        ref for ref in references[instance.key] if ref.upper() != change.reference.upper()
    )
    missing = [
        ref
        for ref in others
        if (other := by_key.get((ref.upper(), ""))) is None or other.payload() != change.payload()
    ]
    if missing:
        other = (
            "or write a different footprint into a variant with set_symbol_fields"
            if isinstance(change, FootprintChange)
            else f"or write different {what} through a variant"
        )
        raise InputError(
            f"The symbol of {change.reference} is in a sheet that the hierarchy uses more than one "
            f"time: KiCad keeps its {what} for {', '.join(missing)} too. Give the same {what} for "
            f"{', '.join([change.reference, *missing])}, {other}."
        )


def _plan_symbol(
    change: FieldChange,
    instance: PlacedInstance,
    record: SchematicFile,
    size: float,
    base: dict[tuple[Path, int], dict[str, str]],
    edits: dict[Path, list[tuple[int, int, str]]],
    rows: list[_Row],
    unit_text: str,
) -> None:
    file_edits = edits.setdefault(instance.file, [])
    symbol = instance.symbol
    texts = base.setdefault(instance.key, {})
    new_props = []
    for requested, value in change.fields.items():
        # The name as KiCad loads the field: get_symbol_details gives these names (a
        # second "MPN" of the file is "MPN_1").
        name, nodes = symbol_field_props(symbol, requested, record.rules)
        if len(nodes) > 1:
            raise InputError(
                f"The symbol {instance.reference}{unit_text} has the field {name!r} more than "
                "one time. Correct it in KiCad first."
            )
        _check_text(instance, record, value)
        if nodes:
            before = _field_text(nodes[0], record)
            atoms = property_atoms(nodes[0])
            if before != value:
                if len(atoms) > 1:
                    file_edits.append((atoms[1].start, atoms[1].end, quote(value)))
                else:
                    file_edits.append((atoms[0].end, atoms[0].end, " " + quote(value)))
        else:
            before = None
            new_props.append(_new_property(symbol, requested, value, size, record.version))
        texts[name] = value
        rows.append(
            _Row(
                instance.reference,
                unit_text,
                "",
                name,
                "(no field)" if before is None else before,
                value,
                instance.file,
            )
        )
    if new_props:
        last = symbol.children("property")[-1]
        file_edits.append(_insert_after(last, "\n".join(new_props)))


def _plan_variant(
    change: FieldChange,
    instance: PlacedInstance,
    record: SchematicFile,
    base: dict[tuple[Path, int], dict[str, str]],
    variants: dict[tuple[Path, int], _Variants],
    rows: list[_Row],
    unit_text: str,
) -> None:
    label = f"{instance.reference}{unit_text}"
    if record.version < SCHEMATIC_VARIANTS:
        raise InputError(
            f"The file {instance.file.name} of {label} has a format with no variants (file "
            f"version {record.version}). Open and save it in KiCad 10 first."
        )
    if instance.path is None:
        raise InputError(
            f"The symbol {label} has no instance for its sheet path, thus it has no variants. "
            "Open and save the schematic in KiCad first."
        )
    state = variants.setdefault(
        (instance.file, instance.path.start), _instance_variants(instance, record)
    )
    tokens, fields = state.new.get(change.variant, ({}, {}))
    texts = base.get(instance.key, {})
    for requested, value in change.fields.items():
        name, nodes = symbol_field_props(instance.symbol, requested, record.rules)
        if len(nodes) > 1:
            raise InputError(
                f"The symbol {label} has the field {name!r} more than one time. Correct it in "
                "KiCad first."
            )
        if nodes:
            # KiCad finds the field of a variant by the name of the field as KiCad
            # loads it (GetName).
            symbol_text = texts.get(name, _field_text(nodes[0], record))
        elif requested in texts:  # a new field of the symbol in this call
            name, symbol_text = requested, texts[requested]
        else:
            raise InputError(
                f"The symbol {label} has no field {requested!r}. A variant can set only a field "
                "that the symbol has: write the field for the symbol first (no variant)."
            )
        before = fields.get(name, symbol_text)
        if value == symbol_text:
            fields.pop(name, None)  # no differential (SCH_SYMBOL::SetFieldText)
        else:
            fields[name] = value
        rows.append(
            _Row(instance.reference, unit_text, change.variant, name, before, value, instance.file)
        )
    state.new[change.variant] = (tokens, fields)


def _yes_no(token: str, value: bool) -> str:
    """The attribute of a token value as a text: "in_bom yes" is "exclude_from_bom no"."""
    _name, inverted = _TOKENS[token]
    return "yes" if value != inverted else "no"


def _plan_attribute_symbol(
    change: AttributeChange,
    instance: PlacedInstance,
    record: SchematicFile,
    plan: _Plan,
    unit_text: str,
) -> None:
    """The attributes of a symbol (DIALOG_SYMBOL_PROPERTIES with no variant: SetDNP and
    the others set the attribute of the symbol). Each variant of each instance of the
    symbol keeps the attribute of the load where its file has no token: variant_edit
    then writes that value as a token, as KiCad does."""
    symbol = instance.symbol
    label = f"{instance.reference}{unit_text}"
    before = symbol_tokens(symbol, label)
    after = dict(before)
    file_edits = plan.edits.setdefault(instance.file, [])
    for name, token, inverted in ATTRIBUTES:
        value = getattr(change.attributes, name)
        if value is None:
            continue
        node = _last(symbol, token)
        if node is None:
            raise InputError(
                f"The symbol {label} in {instance.file.name} has no ({token} ...): KiCad 10 "
                "writes it for each symbol. Open and save the schematic in KiCad first."
            )
        after[token] = value != inverted
        if after[token] != before[token]:
            atom = node.atoms()[0]
            file_edits.append((atom.start, atom.end, "yes" if after[token] else "no"))
        plan.rows.append(
            _Row(
                instance.reference,
                unit_text,
                "",
                name,
                _yes_no(token, before[token]),
                _yes_no(token, after[token]),
                instance.file,
            )
        )
    plan.tokens[instance.key] = (before, after)
    # All instances of the symbol (also of other projects): KiCad writes each of them.
    instances = symbol.first("instances")
    for project in instances.children("project") if instances is not None else []:
        for path in project.children("path"):
            if path.children("variant"):
                plan.variants.setdefault(
                    (instance.file, path.start),
                    read_variants(label, record, path, instance.key, symbol),
                )


def _plan_attribute_variant(
    change: AttributeChange,
    instance: PlacedInstance,
    record: SchematicFile,
    plan: _Plan,
    unit_text: str,
) -> None:
    """The attributes of a variant of the instance (SetDNP with a variant: an existing
    variant gets the value; a new variant starts with the attributes of the symbol,
    InitializeAttributes)."""
    label = f"{instance.reference}{unit_text}"
    if instance.path is None:
        raise InputError(
            f"The symbol {label} has no instance for its sheet path, thus it has no variants. "
            "Open and save the schematic in KiCad first."
        )
    state = plan.variants.setdefault(
        (instance.file, instance.path.start), _instance_variants(instance, record)
    )
    loaded, current = plan.tokens.get(instance.key, (None, None))
    if loaded is None:
        loaded = current = symbol_tokens(instance.symbol, label)
    if change.variant in state.new:
        tokens, fields = state.new[change.variant]
        start = {token: tokens.get(token, loaded[token]) for token in loaded}
    else:
        tokens, fields = dict(current), {}
        start = dict(current)
    for name, token, inverted in ATTRIBUTES:
        value = getattr(change.attributes, name)
        if value is None:
            continue
        tokens[token] = value != inverted
        plan.rows.append(
            _Row(
                instance.reference,
                unit_text,
                change.variant,
                name,
                _yes_no(token, start[token]),
                _yes_no(token, tokens[token]),
                instance.file,
            )
        )
    state.new[change.variant] = (tokens, fields)


FOOTPRINT_VISIBLE = "Footprint field visible"


def _hide_state(prop: Node, label: str) -> tuple[bool, Node | None]:
    """Return (the field is hidden, the (hide ...) that sets it, or None), as
    parseSchField reads the field: a (hide yes|no) of the property (parseBool) and a
    hide of its (effects ...) (parseEDA_TEXT: (hide), (hide yes|no) or the bare token);
    the last one stays. A bare token gives None (no value to change)."""
    hidden, token = False, None
    for item in prop.items[1:]:
        if not isinstance(item, Node):
            continue
        if item.head == "hide":
            hidden, token = parse_bool(item, f"the Footprint field of {label}"), item
        elif item.head == "effects":
            for sub in item.items[1:]:
                if isinstance(sub, Node) and sub.head == "hide":
                    # parseMaybeAbsentBool: (hide) is hidden.
                    hidden = parse_maybe_absent_bool(sub, f"the Footprint field of {label}")
                    token = sub
                elif not isinstance(sub, Node) and not sub.quoted and sub.text == "hide":
                    hidden, token = True, None
    return hidden, token


def _hide_edit(prop: Node, token: Node | None) -> tuple[int, int, str]:
    """The edit that hides a visible field: the value of its (hide no) becomes yes, else
    a new (hide yes) after the (at ...) of the property, where SCH_IO_KICAD_SEXPR::saveField
    writes it."""
    if token is not None:
        atoms = token.atoms()
        if atoms:
            return (atoms[0].start, atoms[0].end, "yes")
        return (token.end - 1, token.end - 1, " yes")
    at = prop.first("at")
    if at is not None:
        return _insert_after(at, "(hide yes)")
    atoms = property_atoms(prop)
    return (atoms[-1].end, atoms[-1].end, " (hide yes)")


def _plan_footprint_symbol(
    change: FootprintChange,
    instance: PlacedInstance,
    record: SchematicFile,
    plan: _Plan,
    unit_text: str,
) -> None:
    """Plan the footprint of one placed symbol as SCH_EDITOR_CONTROL::AssignFootprints
    (KiCad 10.0.6) sets it: the text of the Footprint field of the symbol, when it
    differs from the old text. A Footprint field that was empty and visible becomes
    hidden (SetVisible, with no commit). The save of CvPcb (MAIL_SCH_SAVE) writes it
    also with no change of a footprint: SCH_EDIT_FRAME::SaveProject of KiCad 10 starts
    at the virtual root sheet, which has no file, thus it never skips a save with no
    changes (measured in the GUI of KiCad 10.0.6).

    Raises:
        InputError: The symbol has no library symbol (CvPcb does not list it), no
            Footprint field or more than one, or a text variable in the field (KiCad
            compares the resolved text).
    """
    label = f"{instance.reference}{unit_text}"
    if not instance.has_library_symbol:
        raise InputError(
            f"The symbol {label} has no library symbol in the schematic: CvPcb does not list "
            "it (SCH_SHEET_PATH::AppendSymbol). Repair the symbol in KiCad first."
        )
    _name, nodes = symbol_field_props(instance.symbol, "Footprint", record.rules)
    if len(nodes) != 1:
        problem = "no Footprint field" if not nodes else "the Footprint field more than one time"
        raise InputError(
            f"The symbol {label} has {problem}. KiCad 10 writes one Footprint field for "
            "each symbol: open and save the schematic in KiCad first."
        )
    prop = nodes[0]
    before = _field_text(prop, record)
    if "${" in before:
        raise InputError(
            f"The Footprint field of {label} has a text variable ({before!r}). KiCad compares "
            "the footprint with the resolved text, which the tool cannot resolve. Change the "
            "footprint in KiCad."
        )
    file_edits = plan.edits.setdefault(instance.file, [])
    if before != change.footprint:
        atoms = property_atoms(prop)
        if len(atoms) > 1:
            file_edits.append((atoms[1].start, atoms[1].end, quote(change.footprint)))
        else:
            file_edits.append((atoms[0].end, atoms[0].end, " " + quote(change.footprint)))
    plan.rows.append(
        _Row(
            instance.reference, unit_text, "", "Footprint", before, change.footprint, instance.file
        )
    )
    if not before:
        hidden, token = _hide_state(prop, label)
        if not hidden:
            file_edits.append(_hide_edit(prop, token))
            plan.rows.append(
                _Row(
                    instance.reference, unit_text, "", FOOTPRINT_VISIBLE, "yes", "no", instance.file
                )
            )


def _footprint_variants(instance: PlacedInstance) -> list[str]:
    """The warning for the variants of an instance with a Footprint of their own: they
    keep it (AssignFootprints writes only the field of the symbol)."""
    own = []
    holder = instance.variants
    for variant in holder.children("variant") if holder is not None else []:
        name = variant.first("name")
        if name is not None and "Footprint" in _variant_fields(variant):
            own.append(name.value(0, ""))
    if not own:
        return []
    return [
        f"The variants {', '.join(repr(n) for n in sorted(own))} of {instance.reference} have a "
        "Footprint of their own: they keep it (CvPcb writes only the field of the symbol). "
        "set_symbol_fields with the variant changes it."
    ]


def _check_text(instance: PlacedInstance, record: SchematicFile, value: str) -> None:
    """Refuse "~" in a file older than EMPTY_TEXT_MARKER: KiCad reads it as an
    empty field (parseSchField), thus it cannot keep the text "~"."""
    if value == "~" and record.version < EMPTY_TEXT_MARKER:
        raise InputError(
            f'The file {instance.file.name} of {instance.reference} has a format that reads "~" '
            "as an empty field (file version before 20250318): give an empty text, or save the "
            "file in KiCad 10 first."
        )


@dataclass(frozen=True)
class _Kind:
    """The texts of the output of one kind of change."""

    noun: str  # "field": "The tool wrote 2 field values"
    column: str  # the column of the table
    title: str  # "# <title> of the symbols of <project>"
    note: str  # the text after the table
    # The tool for KiCad 10 only (kicad_10_problem), or None (installed_kicad_problem).
    tool: str | None = None


_KINDS = {
    FieldChange.what: _Kind(
        "field",
        "Field",
        "Fields",
        "Each field goes into each unit of the reference. A new field is hidden as KiCad 9 "
        "and 10 make it (a file of KiCad 8: visible; Value_Confirmed and MPN_Confirmed: "
        "always hidden). A variant keeps only the fields that are different from the fields "
        "of the symbol (KiCad).",
    ),
    AttributeChange.what: _Kind(
        "attribute",
        "Attribute",
        "Attributes",
        "Each attribute goes into each unit of the reference. A variant keeps only the "
        "attributes that are different from the attributes of the symbol (KiCad). A change "
        "of an attribute of the symbol does not change a variant: KiCad then writes the old "
        "value into each variant.",
        tool="set_symbol_attributes",
    ),
    FootprintChange.what: _Kind(
        "Footprint field",
        "Field",
        "Footprints",
        "Each footprint goes into the Footprint field of each unit of the reference, not into "
        "a variant (CvPcb). A Footprint field of the call that is empty and visible becomes "
        f'hidden ("{FOOTPRINT_VISIBLE}"), as the save of CvPcb does.',
        tool="assign_footprint",
    ),
}


def _output(
    project: Path,
    rows: list[_Row],
    written: list[Path],
    problems: list[str],
    kind: _Kind,
    checks: list[str] | None = None,
) -> str:
    changed = [r for r in rows if r.before != r.after and not r.shared]
    lines = [f"# {kind.title} of the symbols of {project.name}", ""]
    if not written:
        lines.append(f"✅ The {kind.noun}s have these values already. The tool wrote no file.")
    else:
        files = ", ".join(f"`{p.name}`" for p in written)
        values = "value" if len(changed) == 1 else "values"
        lines.append(f"✅ The tool wrote {len(changed)} {kind.noun} {values} into {files}.")
    lines += [
        "",
        f"| Reference | Units | Variant | {kind.column} | Before | After | File |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        state = r.after if r.before != r.after else f"{r.after} (no change)"
        cells = (
            r.reference,
            r.units or "-",
            r.variant or "-",
            r.field,
            r.before or "(empty)",
            state or "(empty)",
            f"{r.file.name} (the symbol of {r.shared})" if r.shared else r.file.name,
        )
        lines.append("| " + " | ".join(cell(c) for c in cells) + " |")
    lines += ["", kind.note]
    if checks:
        lines += ["", *checks]
    lines += [f"⚠️ {problem}" for problem in problems]
    return "\n".join(lines)


def _check_texts(texts: dict[Path, str]) -> str | None:
    """Return an error when the new text of a file is not a list that the parser
    reads, or None: a fault of an edit does not go to the disk."""
    for file, text in texts.items():
        try:
            parse(text)
        except SExprError as e:
            return f"The new text of {file.name} is not valid ({e}): this is an error of the tool."
    return None


# The check of a footprint assignment after the plan and before the write: (the project,
# the installed KiCad) -> (the lines of the output, the warnings), or the text of an error.
Check = Callable[[Path, str], Awaitable["tuple[list[str], list[str]] | str"]]


async def set_symbols(
    schematic_path: str, changes: list[Change], what: str, check: Check | None = None
) -> str:
    """Plan the changes of one kind (the what of its Change class), check the plan, then
    write all files or no file, and return the output of the tool.

    Args:
        check: An other check after the plan and before the write (assign_footprint).
    """
    kind = _KINDS[what]
    path = Path(os.path.abspath(os.path.expanduser(schematic_path)))
    if path.suffix != ".kicad_sch" or not path.is_file():
        return f"❌ The schematic {path} does not exist or is not a .kicad_sch file."
    if not changes:
        return f"❌ changes is empty. Give the {what} to write."
    try:
        project = await asyncio.to_thread(schematic_project_file, path)
    except AmbiguousRootError as e:
        return f"❌ {e}. Give the root schematic of the project."
    kicad = await get_kicad_version()
    if kind.tool is not None:
        if kicad_problem := kicad_10_problem(kicad, f"{kind.tool} has"):
            return f"❌ {kicad_problem}"
    elif kicad_problem := installed_kicad_problem(kicad):
        return f"❌ {kicad_problem}"
    try:
        kicad_major(kicad)
    except InputError as e:
        return f"❌ {e}"
    size = await asyncio.to_thread(default_text_size, project, kicad)
    try:
        texts, rows, files, problems = await asyncio.to_thread(_plan, project, kicad, changes, size)
    except (OSError, KiCadEncodingError, SExprError, KicadFileError) as e:
        return f"❌ The schematic files of {project} cannot be read: {e}"
    error = await asyncio.to_thread(_check_texts, texts)
    if error:
        return f"❌ {error} The tool wrote no file."
    checks: list[str] = []
    if check is not None:
        found = await check(project, kicad)
        if isinstance(found, str):
            return found
        checks, check_warnings = found
        problems = problems + check_warnings
    # All files or no file: the units of one package must not get different fields.
    stamps = {file: files[file].stamp for file in texts}
    result = await asyncio.to_thread(write_schematics, texts, stamps)
    if isinstance(result, str):
        return result
    written, warnings = result
    return _output(project, rows, written, problems + warnings, kind, checks)


@mcp.tool()
@with_schematic_versions("schematic_path", write=True)
async def set_symbol_fields(schematic_path: str, changes: list[FieldChange]) -> str:
    """Write the fields of placed symbols, as KiCad writes them.

    The tool writes each field into each unit of the reference (one package
    has one Value and one MPN). A field that the symbol does not have becomes
    a new field, hidden as KiCad 9 and 10 make it (a file of KiCad 8: visible;
    Value_Confirmed and MPN_Confirmed: always hidden). With a variant (KiCad 10), the
    tool writes the fields of that variant into each unit: a value equal to the
    field of the symbol removes the field from the variant. The tool checks all
    changes and all files first, and writes no file when a check fails. It writes
    all files or no file: an error of the disk also changes no file. KiCad must not
    have the files open. A schematic file of the project with an older version than
    the installed KiCad writes is an error: the user converts it in KiCad first (the
    output gives the step).

    A symbol in a sheet that the hierarchy uses more than one time has one set
    of fields for all its references (U201 and U301): give the same fields for
    each of these references, or write different fields through a variant.

    The tool refuses a reference that is not annotated (U?), a duplicate reference,
    the field Reference, a mandatory field name in an other case ("value": give
    "Value"), the fields Sim.Enable and Spice_Netlist_Enabled (KiCad removes them when
    it loads the symbol), a symbol with two fields of one name (a mandatory field
    two times, or more than ten user fields of one name), a file that KiCad loads
    under more than one name, and a project whose full sheet hierarchy the tool
    cannot read. An error gives the reason, and the step in KiCad when one is
    necessary.

    With KiCad 10, the field names are the names that KiCad gives the fields when it
    loads the symbol, as get_symbol_details gives them: a second field "MPN" of the
    file has the name "MPN_1" (to "MPN_9"). Give that name to write that field: the
    tool changes its text and keeps its name in the file (KiCad writes "MPN_1" at its
    next save). With KiCad 8 and 9, the names are the names of the file.

    A new field has the position of the symbol and the angle of the Reference field,
    as KiCad adds it. In a file of KiCad 8, a new field is visible on the body of the
    symbol: this is correct, not a defect.

    Use it for the fields that project_check of datasheet-lens or
    check_symbol_fields gives (MPN_Confirmed and Value_Confirmed for a
    confirmation; Value, MPN, MPN_Confirmed and Value_Confirmed for the move of
    the old convention), after the user confirmed them.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool finds the
            references in the full project.
        changes: One item for each reference (and variant): the reference, the
            fields by name, and the variant name (empty: the symbol).

    Returns:
        A table of the fields before and after the write, and the files.
    """
    try:
        return await set_symbols(schematic_path, changes, FieldChange.what)
    except Exception as e:
        return error_answer(e, "Error")


@mcp.tool()
@with_schematic_versions("schematic_path", write=True)
async def set_symbol_attributes(schematic_path: str, changes: list[AttributeChange]) -> str:
    """Write the attributes of placed symbols, as the Symbol Properties dialog of KiCad 10
    sets them: "Do not populate" (dnp), "Exclude from bill of materials", "Exclude from
    board", "Exclude from position files" and "Exclude from simulation".

    The tool writes each attribute into each unit of the reference. KiCad 10.0.6 gives
    the other units the same DNP and Exclude from BOM, board and position files, but
    not Exclude from simulation (it keeps that one for each unit): the tool sets each
    unit, as the dialog on each unit does. The tool changes only the attributes. The
    OK of the dialog also copies the fields of the unit (not the Reference) to the
    other units (SCH_SYMBOL::SyncOtherUnits). The tool does not copy the fields: units with
    different fields keep them (set_symbol_fields changes the fields).

    With a variant (KiCad 10), the tool writes the attributes of that variant: a value
    equal to the attribute of the symbol is no differential, and KiCad writes no
    variant with no differentials. A change of an attribute of the symbol does not
    change the variants: KiCad keeps the old value in each variant (the variant starts
    with the attributes of the symbol when KiCad loads the file), thus the file then
    has the old value as a token of each variant. This is correct, not a defect.

    A symbol in a sheet that the hierarchy uses more than one time has one set of
    attributes for all its references (U201 and U301): give the same attributes for each
    of these references, or write different attributes through a variant.

    The tool checks all changes and all files first, and writes no file when a check
    fails. It writes all files or no file. KiCad must not have the files open. A
    schematic file of the project with an older version than the installed KiCad writes
    is an error: the user converts it in KiCad first. The tool refuses a reference that
    is not annotated (U?), a duplicate reference, a variant that the project does not
    have, an attribute name that it does not know, a symbol with no token for an
    attribute to change (KiCad 10 writes the five tokens: save the schematic in KiCad
    first), a token whose value is not yes or no, the variants of an instance that are
    not next to each other, a file that KiCad loads under more than one name, and a
    project whose full sheet hierarchy the tool cannot read. The installed KiCad must be
    KiCad 10.0.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool finds the
            references in the full project.
        changes: One item for each reference (and variant): the reference, the
            attributes to set (a missing attribute does not change), and the variant
            name (empty: the symbol).

    Returns:
        A table of the attributes before and after the write, and the files.
    """
    try:
        return await set_symbols(schematic_path, changes, AttributeChange.what)
    except Exception as e:
        return error_answer(e, "Error")
