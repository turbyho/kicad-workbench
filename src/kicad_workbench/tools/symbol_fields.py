"""The tool set_symbol_fields: write the fields of placed symbols, as KiCad does.

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
  version (user, 2026-11-05: as KiCad). Value_Confirmed is always hidden (the
  model of confirmed MPNs of datasheet-lens, decision 5).
- The format of the file version (SCH_IO_KICAD_SEXPR::saveField): before
  SCHEMATIC_FIELD_FLAGS the hidden state is in the effects, from it in the
  property. Variants exist from SCHEMATIC_VARIANTS. A file older than
  EMPTY_TEXT_MARKER reads "~" as an empty field.
"""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from ..errors import InputError
from ..parsers.annotation import kicad_major, split_reference
from ..parsers.kicad_fields import SYMBOL_FIELD_NAMES
from ..parsers.kicad_versions import (
    EMPTY_TEXT_MARKER,
    KICAD8,
    SCHEMATIC_FIELD_FLAGS,
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
    property_atoms,
    property_field,
    quote,
)
from ..parsers.symbol_info import letter_reference
from ..server import mcp
from ..utils.kicad_cli import get_kicad_version
from ..utils.kicad_lock import (
    check_schematic_not_open,
    file_stamp,
    writable_schematic_version,
    write_schematic,
)
from ..utils.kicad_text import KiCadEncodingError, read_kicad_json
from ..utils.library_tables import kicad_environment
from ..utils.sch_hierarchy import AmbiguousRootError, schematic_project_file
from ..utils.tool_errors import error_answer
from ..utils.variants import file_variants, project_variants
from .library import cell
from .schematic_editor import _field_error, _mm

# The hidden field of the model of confirmed MPNs (datasheet-lens).
VALUE_CONFIRMED = "Value_Confirmed"
# DEFAULT_TEXT_SIZE of eeschema/default_values.h, in mils. The limits of the
# project setting (SCHEMATIC_SETTINGS, PARAM_SCALED): 5 to 1000 mils.
_DEFAULT_TEXT_SIZE_MILS = 50
_TEXT_SIZE_LIMITS_MILS = (5, 1000)
_IU_PER_MM = 10000


class FieldChange(BaseModel):
    """The fields to write into one reference."""

    reference: str = Field(description="The reference of the symbol, for example U3.")
    fields: dict[str, str] = Field(
        description=(
            'The fields to write, by name, for example {"MPN": "LM358DR", "Value_Confirmed": '
            '"LM358 (LM358DR)"}. A field that the symbol does not have is a new field, hidden '
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


def _field_nodes(symbol: Node, name: str, kicad: int) -> list[Node]:
    """Return the (property ...) of a field of a symbol: the exact name
    (SCH_SYMBOL::GetField), and a mandatory field with no difference of case in
    KiCad 10 (parseSchField)."""
    mandatory = kicad == 10 and name.lower() in (n.lower() for n in SYMBOL_FIELD_NAMES)
    nodes = []
    for prop in symbol.children("property"):
        field = property_field(prop)
        if field is not None and (
            field[0] == name or (mandatory and field[0].lower() == name.lower())
        ):
            nodes.append(prop)
    return nodes


def _field_text(prop: Node, version: int) -> str:
    """Return the text of a field: "~" is empty only in a file older than
    EMPTY_TEXT_MARKER (parseSchField)."""
    field = property_field(prop)
    return file_text(field[1] if field else "", version)


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
    from KiCad 9 (KiCad 8 does not hide it), and Value_Confirmed always.

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
    hidden = name == VALUE_CONFIRMED or version > KICAD8.schematic
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


# The fields that KiCad removes at the load of a symbol: their text sets
# exclude_from_sim (SIM_LEGACY_ENABLE_FIELD_V7 and SIM_LEGACY_ENABLE_FIELD,
# SCH_IO_KICAD_SEXPR_PARSER::parseSchematicSymbol of KiCad 8, 9 and 10).
_LEGACY_SIM_FIELDS = ("Sim.Enable", "Spice_Netlist_Enabled")


def _check_change(change: FieldChange, known_variants: set[str]) -> None:
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
        if name in _LEGACY_SIM_FIELDS:
            raise InputError(
                f"KiCad removes the field {name!r} when it loads the symbol and sets "
                '"exclude from simulation" from its text. Set that flag in KiCad.'
            )
        error = _field_error(name, value) or _field_error("field name", name)
        if error:
            raise InputError(error.removeprefix("❌ "))
    if change.variant and change.variant not in known_variants:
        listed = ", ".join(repr(n) for n in sorted(known_variants)) or "none"
        raise InputError(
            f"The project has no variant {change.variant!r}. Its variants: {listed}. Make the "
            "variant in KiCad first."
        )


@dataclass
class _Variants:
    """The variants of one instance (one (path ...)): the original and the new state."""

    instance: PlacedInstance
    nodes: list[Node]  # the (variant ...) nodes of the file, in their order
    # name -> (the other items of the variant as text: dnp, in_bom, ...; the fields)
    original: dict[str, tuple[list[str], dict[str, str]]]
    new: dict[str, tuple[list[str], dict[str, str]]]


def _read_variants(instance: PlacedInstance) -> _Variants:
    assert instance.path is not None
    nodes = instance.path.children("variant")
    original: dict[str, tuple[list[str], dict[str, str]]] = {}
    for node in nodes:
        name = node.first("name")
        if name is None:
            continue
        others = [
            item.source[item.start : item.end]
            for item in node.children()
            if item.head not in ("name", "field")
        ]
        original[name.value(0, "")] = (others, _variant_fields(node))
    new = {name: (others, dict(fields)) for name, (others, fields) in original.items()}
    return _Variants(instance, nodes, original, new)


def _variant_text(name: str, others: list[str], fields: dict[str, str]) -> str:
    """Return a (variant ...) with one list on each line, or "" when it has no
    differentials (SCH_IO_KICAD_SEXPR writes no such variant)."""
    if not others and not fields:
        return ""
    parts = [f"(name {quote(name)})", *others]
    parts += [
        f"(field\n(name {quote(k)})\n(value {quote(v)})\n)" for k, v in sorted(fields.items())
    ]
    return "(variant\n" + "\n".join(parts) + "\n)"


def _variant_edit(state: _Variants) -> tuple[int, int, str] | None:
    """Return the one edit that writes all variants of an instance, sorted by name
    as SCH_IO_KICAD_SEXPR writes them (std::map), or None when they do not change.
    One edit for all variants: two edits next to each other cannot overlap.

    Raises:
        InputError: The variants are not next to each other in the instance.
    """
    if state.new == state.original:
        return None
    path = state.instance.path
    assert path is not None
    text = "\n".join(
        t for name in sorted(state.new) if (t := _variant_text(name, *state.new[name]))
    )
    children = path.children()
    if state.nodes:
        first, last = children.index(state.nodes[0]), children.index(state.nodes[-1])
        if last - first + 1 != len(state.nodes):
            raise InputError(
                f"The variants of {state.instance.reference} in {state.instance.file.name} are "
                "not next to each other. Open and save the schematic in KiCad first."
            )
        if text:
            return (state.nodes[0].start, state.nodes[-1].end, _replacement(state.nodes[0], text))
        start, source = state.nodes[0].start, path.source
        while start > 0 and source[start - 1] in " \t\n":
            start -= 1  # with the blanks before the first variant
        return (start, state.nodes[-1].end, "")
    if not text:
        return None
    if children:
        return _insert_after(children[-1], text)
    return (path.end - 1, path.end - 1, " " + _one_line(text))


def _plan(
    project: Path, kicad: str, changes: list[FieldChange], size: float
) -> tuple[dict[Path, str], list[_Row], dict[Path, SchematicFile], list[str]]:
    """Return the new text of each file to write, the rows of the output, the
    files, and the warnings.

    Raises:
        InputError: A change cannot be written (the message tells why).
    """
    # The references of the schematic editor of the installed KiCad (user, 2026-11-05).
    instances, files, problems = placed_instances(project, kicad, "gui")
    if problems:
        # A sheet that the tool cannot read can hold an other reference of a symbol
        # (a shared sheet): the check of _check_shared would not be complete.
        raise InputError(
            "The tool cannot read the full sheet hierarchy, thus it cannot find each "
            "reference of a symbol: " + " ".join(problems) + " Correct it in KiCad first."
        )
    known_variants: set[str] = set()
    if any(c.variant for c in changes):
        known_variants = project_variants(project).union(
            *(file_variants(f.root) for f in files.values())
        )
    by_key: dict[tuple[str, str], FieldChange] = {}
    for change in changes:
        _check_change(change, known_variants)
        key = (change.reference.upper(), change.variant)
        if key in by_key:
            raise InputError(f"Two changes of {change.reference}{_variant_label(change.variant)}.")
        by_key[key] = change
    groups: dict[str, list[PlacedInstance]] = {}
    references: dict[tuple[Path, int], set[str]] = {}  # more than one in a shared sheet
    for instance in instances:
        groups.setdefault(instance.reference.upper(), []).append(instance)
        references.setdefault(instance.key, set()).add(instance.reference)

    edits: dict[Path, list[tuple[int, int, str]]] = {}
    rows: list[_Row] = []
    warnings: list[str] = []
    # The texts of the fields of each symbol after the changes of the symbols: the
    # base of the variants (a text equal to it is no differential).
    base: dict[tuple[Path, int], dict[str, str]] = {}
    done: dict[tuple[Path, int], list[_Row]] = {}  # the symbols that got their fields
    variants: dict[tuple[Path, int], _Variants] = {}  # by (file, offset of the path)
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
            record = files[instance.file]
            if variant:
                _plan_variant(change, instance, record, base, variants, rows, unit_text)
                continue
            _check_shared(change, instance, references, by_key)
            if instance.key in done:
                rows += [
                    _Row(reference, unit_text, "", r.field, r.before, r.after, r.file, r.reference)
                    for r in done[instance.key]
                ]
                continue
            start = len(rows)
            _plan_symbol(change, instance, record, size, base, edits, rows, unit_text)
            done[instance.key] = rows[start:]
            if any(r.before != r.after for r in rows[start:]):
                warnings += _other_projects(instance, project.stem)
    for state in variants.values():
        edit = _variant_edit(state)
        if edit is not None:
            edits.setdefault(state.instance.file, []).append(edit)
    texts = {}
    for path, file_edits in edits.items():
        if file_edits:
            root = files[path].root
            texts[path] = (
                root.source[: root.start] + edited(root, file_edits) + root.source[root.end :]
            )
    return texts, rows, files, list(dict.fromkeys(warnings))


def _other_projects(instance: PlacedInstance, project_name: str) -> list[str]:
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
        f"KiCad keeps the fields of {instance.reference} for them too (it can be an old "
        "copy of the sheet)."
    ]


def _variant_label(variant: str) -> str:
    return f" (variant {variant!r})" if variant else ""


def _check_shared(
    change: FieldChange,
    instance: PlacedInstance,
    references: dict[tuple[Path, int], set[str]],
    by_key: dict[tuple[str, str], FieldChange],
) -> None:
    """Refuse a change of a symbol that an other reference uses too (a sheet that
    the hierarchy uses more than one time), when the changes do not give that
    reference the same fields (user, 2026-11-05)."""
    others = sorted(
        ref for ref in references[instance.key] if ref.upper() != change.reference.upper()
    )
    missing = [
        ref
        for ref in others
        if (other := by_key.get((ref.upper(), ""))) is None or other.fields != change.fields
    ]
    if missing:
        raise InputError(
            f"The symbol of {change.reference} is in a sheet that the hierarchy uses more than one "
            f"time: KiCad keeps its fields for {', '.join(missing)} too. Give the same fields for "
            f"{', '.join([change.reference, *missing])}, or write different fields through a variant."
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
    for name, value in change.fields.items():
        nodes = _field_nodes(symbol, name, record.kicad)
        if len(nodes) > 1:
            raise InputError(
                f"The symbol {instance.reference}{unit_text} has the field {name!r} more than "
                "one time. Correct it in KiCad first."
            )
        _check_text(instance, record, value)
        if nodes:
            before = _field_text(nodes[0], record.version)
            atoms = property_atoms(nodes[0])
            if before != value:
                if len(atoms) > 1:
                    file_edits.append((atoms[1].start, atoms[1].end, quote(value)))
                else:
                    file_edits.append((atoms[0].end, atoms[0].end, " " + quote(value)))
        else:
            before = None
            new_props.append(_new_property(symbol, name, value, size, record.version))
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
    state = variants.setdefault((instance.file, instance.path.start), _read_variants(instance))
    others, fields = state.new.get(change.variant, ([], {}))
    texts = base.get(instance.key, {})
    for requested, value in change.fields.items():
        nodes = _field_nodes(instance.symbol, requested, record.kicad)
        if nodes:
            # KiCad finds the field of a variant by the name of the field (GetName).
            name = property_field(nodes[0])[0]  # type: ignore[index]
            symbol_text = texts.get(name, _field_text(nodes[0], record.version))
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
    state.new[change.variant] = (others, fields)


def _check_text(instance: PlacedInstance, record: SchematicFile, value: str) -> None:
    """Refuse "~" in a file older than EMPTY_TEXT_MARKER: KiCad reads it as an
    empty field (parseSchField), thus it cannot keep the text "~"."""
    if value == "~" and record.version < EMPTY_TEXT_MARKER:
        raise InputError(
            f'The file {instance.file.name} of {instance.reference} has a format that reads "~" '
            "as an empty field (file version before 20250318): give an empty text, or save the "
            "file in KiCad 10 first."
        )


def _output(project: Path, rows: list[_Row], written: list[Path], problems: list[str]) -> str:
    changed = [r for r in rows if r.before != r.after and not r.shared]
    lines = [f"# Fields of the symbols of {project.name}", ""]
    if not written:
        lines.append("✅ The fields have these values already. The tool wrote no file.")
    else:
        files = ", ".join(f"`{p.name}`" for p in written)
        lines.append(f"✅ The tool wrote {len(changed)} field values into {files}.")
    lines += [
        "",
        "| Reference | Units | Variant | Field | Before | After | File |",
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
    lines += [
        "",
        "Each field goes into each unit of the reference. A new field is hidden as KiCad 9 "
        "and 10 make it (a file of KiCad 8: visible; Value_Confirmed: always hidden). A "
        "variant keeps only the fields that are different from the fields of the symbol "
        "(KiCad).",
    ]
    lines += [f"⚠️ {problem}" for problem in problems]
    return "\n".join(lines)


def _check_files(texts: dict[Path, str], files: dict[Path, SchematicFile]) -> str | None:
    """Return the first error of the checks of write_schematic for the files, or None.
    The new text of each file must also be a list that the parser reads: a fault of
    an edit does not go to the disk."""
    for file, text in texts.items():
        try:
            parse(text)
        except SExprError as e:
            return f"The new text of {file.name} is not valid ({e}): this is an error of the tool."
        version = writable_schematic_version(file, text)
        error = version if isinstance(version, str) else check_schematic_not_open(file)
        if not error and file_stamp(file) != files[file].stamp:
            error = f"The file {file} changed after this tool read it. Do this operation again."
        if error:
            return error
    return None


async def _set_symbol_fields(schematic_path: str, changes: list[FieldChange]) -> str:
    path = Path(os.path.abspath(os.path.expanduser(schematic_path)))
    if path.suffix != ".kicad_sch" or not path.is_file():
        return f"❌ The schematic {path} does not exist or is not a .kicad_sch file."
    if not changes:
        return "❌ changes is empty. Give the fields to write."
    try:
        project = await asyncio.to_thread(schematic_project_file, path)
    except AmbiguousRootError as e:
        return f"❌ {e}. Give the root schematic of the project."
    kicad = await get_kicad_version()
    if kicad is None:
        return (
            "❌ The version of KiCad is not known (kicad-cli does not run): the references of "
            "the symbols depend on it. Install KiCad or set KICAD_CLI."
        )
    try:
        kicad_major(kicad)
    except InputError as e:
        return f"❌ {e}"
    size = await asyncio.to_thread(default_text_size, project, kicad)
    try:
        texts, rows, files, problems = await asyncio.to_thread(_plan, project, kicad, changes, size)
    except (OSError, KiCadEncodingError, SExprError, KicadFileError) as e:
        return f"❌ The schematic files of {project} cannot be read: {e}"
    # The checks of all files first: the tool writes no file when one check fails.
    error = await asyncio.to_thread(_check_files, texts, files)
    if error:
        return f"❌ {error.removeprefix('Error: ').removeprefix('❌ ')} The tool wrote no file."
    written = await asyncio.to_thread(_write_files, texts, files)
    if isinstance(written, str):
        return f"❌ {written}"
    return _output(project, rows, written, problems)


def _write_files(texts: dict[Path, str], files: dict[Path, SchematicFile]) -> list[Path] | str:
    """Write the files; when one write fails, write the old text into the files that
    were written already (the units of one package must not get different fields).
    Return the written files, or the error."""
    written: list[Path] = []
    for file, text in texts.items():
        error = write_schematic(file, text, files[file].stamp)
        if error is None:
            written.append(file)
            continue
        kept = []
        for done in written:
            old = files[done].root.source
            if write_schematic(done, old, file_stamp(done)) is not None:
                kept.append(done.name)
        if kept:
            return (
                f"{error} The tool could not write the old text into {', '.join(kept)} again: "
                "these files have the new fields, the other files do not."
            )
        return f"{error} The tool wrote no file (it wrote the old text again)."
    return written


@mcp.tool()
async def set_symbol_fields(schematic_path: str, changes: list[FieldChange]) -> str:
    """Write the fields of placed symbols, as KiCad writes them.

    The tool writes each field into each unit of the reference (one package
    has one Value and one MPN). A field that the symbol does not have becomes
    a new field, hidden as KiCad 9 and 10 make it (a file of KiCad 8: visible;
    Value_Confirmed: always hidden). With a variant (KiCad 10), the tool writes the fields
    of that variant into each unit: a value equal to the field of the symbol
    removes the field from the variant. The tool checks all changes and all
    files first, and writes no file when a check fails. KiCad must not have
    the files open.

    A symbol in a sheet that the hierarchy uses more than one time has one set
    of fields for all its references (U201 and U301): give the same fields for
    each of these references, or write different fields through a variant.

    Use it for the fields that project_check of datasheet-lens gives (MPN,
    Value_Confirmed, and Value for "complete the fields"), after the user
    confirmed them.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool finds the
            references in the full project.
        changes: One item for each reference (and variant): the reference, the
            fields by name, and the variant name (empty: the symbol).

    Returns:
        A table of the fields before and after the write, and the files.
    """
    try:
        return await _set_symbol_fields(schematic_path, changes)
    except Exception as e:
        return error_answer(e, "Error")
