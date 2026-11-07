"""The tool export_bom: the bill of materials (BOM) of a schematic, as KiCad makes it.

The tool runs "kicad-cli sch export bom" (EESCHEMA_JOBS_HANDLER::JobExportBom
and FIELDS_EDITOR_GRID_DATA_MODEL, KiCad 8.0.9, 9.0.9 and 10.0.6) and reads
its CSV file. kicad-cli runs in English (utils/kicad_cli.py). The tool does
not read the text of its messages: it gives each message to the caller.

The rules of KiCad for the lines of the BOM:

- A power symbol is never in the BOM.
- A symbol that is excluded from the BOM is not in the BOM. Only kicad-cli of
  KiCad 9 can include it (--include-excluded-from-bom); the tool does not use
  that option.
- The units of one reference are one line. The units of a reference that is
  not annotated ("R?") are different lines.
- Each instance of a sheet that the hierarchy uses more than one time gives
  its own lines, with the references of that instance.
- With no fields to group by, each reference is one line. Only a field that the
  list of fields names can group the symbols: "*" adds the other fields with no
  group.
- A cell holds the different values of the symbols of one line: KiCad 9 and
  later join them with a comma and leave out the empty values. KiCad 8
  writes INDETERMINATE_STATE ("-- mixed values --": the tool runs kicad-cli
  in English).
- kicad-cli removes the tabs and the line breaks from the values, and puts
  each value between double quotes (a double quote in a value is two double
  quotes).
"""

import asyncio
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.sexpr import SExprError
from ..parsers.unit_fields import unit_conflicts
from ..server import mcp
from ..utils.file_save import SaveFilesError, save_files
from ..utils.kicad_bom import (
    bom_args,
    bom_rules,
    export_bom_file,
    other_variants,
    unit_check_lines,
    variant_label,
)
from ..utils.kicad_cli import CliExport, KicadCliError, get_kicad_version
from ..utils.kicad_text import KiCadEncodingError
from ..utils.sch_hierarchy import AmbiguousRootError, netlist_root, schematic_project_file
from ..utils.schematic_upgrade import with_current_schematics
from ..utils.tool_errors import error_answer
from ..utils.variants import variant_names
from .library import NO_KICAD_CLI, cell

DEFAULT_FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "${QUANTITY}", "${DNP}", "*")
# IsGeneratedField of KiCad: a field name that is one text variable.
_GENERATED = re.compile(r"\$\{\w*\}")
# The generated columns of JobExportBom of KiCad 10. kicad-cli of KiCad 10
# also accepts their names with no "${}" (normalizeFieldName).
_KICAD10_GENERATED = (
    "QUANTITY",
    "ITEM_NUMBER",
    "DNP",
    "EXCLUDE_FROM_BOM",
    "EXCLUDE_FROM_BOARD",
    "EXCLUDE_FROM_SIM",
)


@dataclass(frozen=True)
class BomField:
    """One item of --fields, as JobExportBom reads it (BOM_FIELD)."""

    raw: str  # The item as the caller gave it.
    name: str  # The field name.
    shown: bool  # The BOM has a column of the field.
    group: bool  # The field groups the symbols.


@dataclass(frozen=True)
class BomFields:
    """The items of --fields, before and after the first "*"."""

    before: list[BomField]
    after: list[BomField]
    star: bool  # The items have a "*".

    def grouped(self, name: str) -> bool:
        return any(f.name == name and f.group for f in self.before + self.after)


def _field(raw: str, group_by: list[str], major: int) -> BomField:
    """Read one item of --fields as JobExportBom of the KiCad version does.

    KiCad 10 reads a generated field name with no "${}" (for example "DNP") as
    the generated field (normalizeFieldName). KiCad 9 and later do not show a
    field whose name starts with "__". A field groups the symbols when
    --group-by has its name (KiCad 10: also the item as the caller gave it).
    """
    name = raw
    if major >= 10 and raw in _KICAD10_GENERATED:
        name = "${" + raw + "}"
    shown = True
    if major >= 9 and name.startswith("__"):
        name, shown = name[2:], False
    group = name in group_by or (major >= 10 and raw in group_by)
    return BomField(raw, name, shown, group)


def bom_fields(fields: list[str], group_by: list[str], major: int) -> BomFields:
    """Return the items of --fields as ApplyBomPreset uses them.

    The first item of a name counts. "*" adds each other field of the symbols
    at its place, with no group (JobExportBom), thus only an item of fields
    groups the symbols.

    Raises:
        InputError: An item of group_by does not group the symbols.
    """
    seen: set[str] = set()
    before: list[BomField] = []
    after: list[BomField] = []
    star = False
    for raw in fields:
        if raw == "*":
            star = True
            continue
        field = _field(raw, group_by, major)
        if not field.name or field.name in seen:
            # ApplyBomPreset ignores an empty name and the second item of a name.
            continue
        seen.add(field.name)
        if star and field.group and not field.shown:
            # "*" compares the item as the caller gave it, thus it adds the field
            # first, with no group, when a symbol has the field.
            raise InputError(
                f"The hidden field {raw!r} after '*' cannot group the symbols. Put it before '*'."
            )
        (after if star else before).append(field)
    used = {
        g
        for g in group_by
        for f in before + after
        if f.group and (g == f.name or (major >= 10 and g == f.raw))
    }
    for name in group_by:
        if name not in used:
            raise InputError(
                f"KiCad groups the symbols only by an item of fields: {name!r} is not one. "
                "A field that '*' adds does not group the symbols. Add the field to fields."
            )
    return BomFields(before, after, star)


def field_columns(header: list[str], fields: BomFields) -> dict[str, int] | None:
    """Return the column of each shown item of fields, or None when the header does
    not agree.

    The columns of the items before "*" are the first columns, and the columns of
    the items after "*" are the last columns. The label of a column is the field
    name, or the name with no "${}" for a generated field (the tool gives no
    labels).
    """
    before = [f for f in fields.before if f.shown]
    after = [f for f in fields.after if f.shown]
    count = len(before) + len(after)
    if count > len(header) or (not fields.star and count != len(header)):
        return None
    columns = {f.name: i for i, f in enumerate(before)}
    start = len(header) - len(after)
    columns.update({f.name: start + i for i, f in enumerate(after)})
    for name, column in columns.items():
        label = name[2:-1] if _GENERATED.fullmatch(name) else name
        if header[column] != label:
            return None
    return columns


def empty_columns(rows: list[list[str]], columns: dict[str, int]) -> list[str]:
    """Return the fields of the symbols that have no value in a line.

    A generated field (for example ${DNP}) can be empty in each line, thus the
    check applies only to the fields of the symbols.
    """
    return [
        name
        for name, column in columns.items()
        if not _GENERATED.fullmatch(name)
        and all(column >= len(row) or not row[column] for row in rows[1:])
    ]


def _is_kicad_file(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower().startswith(".kicad_") or name.endswith("-lib-table")


def _resolve_output(output_path: str, project: Path) -> Path:
    """Return the path of the BOM file.

    The tool writes the file itself: kicad-cli writes into a temporary
    directory. A relative path is relative to the project directory. The tool
    does not expand variables (kicad-cli expands the text and environment
    variables of its output path, JOB::ResolveOutputPath).

    Raises:
        InputError: The path has a variable, or it is a KiCad file or a directory
            (also through a symbolic link).
    """
    if "${" in output_path or "$(" in output_path:
        raise InputError(
            f"The output path {output_path!r} has a variable. The tool does not expand "
            "variables: give the path."
        )
    path = Path(os.path.expanduser(output_path))
    if not path.is_absolute():
        path = Path(os.path.abspath(project)).parent / path
    target = path.resolve()
    if _is_kicad_file(path) or _is_kicad_file(target):
        raise InputError(f"The output path {path} is a KiCad file. Give a .csv file.")
    if target.is_dir():
        raise InputError(f"The output path {path} is a directory. Give a .csv file.")
    return path


def _file_warnings(export: CliExport, rows: list[list[str]], fields: BomFields) -> list[str]:
    """Return the messages of kicad-cli and the warnings about the columns of one BOM file."""
    lines_out = [f"⚠️ kicad-cli: {message}" for message in export.messages]
    columns = field_columns(rows[0], fields)
    empty = empty_columns(rows, columns) if columns is not None else []
    if columns is None:
        lines_out.append(
            "⚠️ The columns of the BOM are not the columns of fields, thus the tool did not "
            "look for empty columns."
        )
    if empty:
        lines_out.append(
            "⚠️ No line has a value in these columns: "
            + ", ".join(f"`{name}`" for name in empty)
            + ". Make sure that the field names are the names of the symbol fields."
        )
    return lines_out


def _group_warning(fields: BomFields) -> str:
    """Return the warning about the lines of group_by."""
    warning = (
        "⚠️ The lines are groups of symbols. A cell of a field that is not in group_by "
        "joins the different values of the symbols of its line."
    )
    if not fields.grouped("${DNP}"):
        warning += " Thus a line can show DNP when only some of its symbols are DNP."
    return (
        warning + " For a list of the parts (for example for project_check of "
        "datasheet-lens), export the BOM with no group_by."
    )


_BOM_NOTE = (
    "KiCad does not put the power symbols and the symbols that are excluded from the "
    "BOM into the BOM. A cell can hold the different values of the units of one "
    "symbol: KiCad 9 and later join them with a comma, KiCad 8 writes "
    '"-- mixed values --".'
)


def _unit_check(project: Path, kicad: str, variants: list[str]) -> list[str]:
    """Return the lines about the units of one package with different fields (see
    parsers/unit_fields): an error of the design, first in the output."""
    try:
        conflicts, problems = unit_conflicts(project, kicad, variants)
    except (OSError, WorkbenchError) as e:
        # The BOM of kicad-cli is correct: only this check did not run.
        return [f"⚠️ The tool could not compare the fields of the units of the symbols: {e}"]
    return unit_check_lines(conflicts, problems)


def _bom_text(
    root: Path,
    kicad: str,
    export: CliExport,
    rows: list[list[str]],
    fields: BomFields,
    output: Path | None,
    replaced: bool,
    variant: str,
    grouped: bool,
    units: list[str],
    saved: list[str],
) -> str:
    header, lines = rows[0], rows[1:]
    lines_out = [f"# BOM of {root.name}", "", *units]
    source = f"kicad-cli of KiCad {kicad} (`sch export bom`)"
    if variant:
        source += f", variant {variant!r}"
    lines_out.append(f"{source}: {len(lines)} lines.")
    if output is not None:
        state = "replaced" if replaced else "written"
        lines_out.append(f"The CSV file of kicad-cli is {state}: `{output}`")
        lines_out += [f"⚠️ {warning}" for warning in saved]
    else:
        lines_out.append("No CSV file is kept. Give output_path to keep the CSV file of kicad-cli.")
    lines_out += _file_warnings(export, rows, fields)
    if grouped:
        lines_out.append(_group_warning(fields))
    lines_out += [
        "",
        _BOM_NOTE,
        "",
        "| " + " | ".join(cell(label) for label in header) + " |",
        "|" + "---|" * len(header),
    ]
    for line in lines:
        lines_out.append("| " + " | ".join(cell(value) for value in line) + " |")
    return "\n".join(lines_out)


# The characters that a file name cannot have on Windows (the most strict of the
# systems of KiCad), and the control characters.
_NOT_IN_FILE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def variant_outputs(output: Path, variants: list[str]) -> list[tuple[str, Path]]:
    """Return the BOM file of the default variant ("") and of each variant.

    The file of the default variant is output. The file of a variant is next to
    it: <stem>-<variant><suffix> (bom.csv, bom-Lite.csv). The tool does not change
    a variant name to make a file name.

    Raises:
        InputError: A variant name has a character that a file name cannot have,
            or two files have the same path with no regard to case and to the
            Unicode normalization (Windows and macOS).
    """
    targets = [("", output)]
    for variant in variants:
        bad = _NOT_IN_FILE_NAME.search(variant)
        if bad:
            raise InputError(
                f"The variant name {variant!r} has the character {bad.group()!r}, which a file "
                "name cannot have. Rename the variant in KiCad, or export each variant with "
                "variant and an output_path."
            )
        targets.append((variant, output.with_name(f"{output.stem}-{variant}{output.suffix}")))
    seen: dict[str, str] = {}
    for variant, path in targets:
        key = unicodedata.normalize("NFC", str(path)).casefold()
        if key in seen:
            raise InputError(
                f"The BOM files of the variants {seen[key] or '(default)'!r} and "
                f"{variant or '(default)'!r} have the same path {path} with no regard to case. "
                "Rename a variant in KiCad, or give an other output_path."
            )
        seen[key] = variant
    return targets


def _save_set(files: list[tuple[Path, bytes]]) -> tuple[list[bool], list[str]]:
    """Save the BOM files of one export: all files or no file (file_save.save_files).

    Returns:
        For each file, True when it was there before; and the warnings of the save.

    Raises:
        OSError: A folder cannot be made. No file changed (a folder that the
            function made before the error stays).
        SaveFilesError: The save failed. The message tells the state of the files.
    """
    for path, _data in files:
        path.parent.mkdir(parents=True, exist_ok=True)
    # os.path.exists gives no error (Path.exists does on Python 3.10 and 3.11).
    replaced = [os.path.exists(path) for path, _data in files]
    return replaced, save_files(files)


def _save_error(error: OSError) -> str:
    if isinstance(error, SaveFilesError):
        return f"❌ {error}"
    # Only mkdir gives an other OSError. A folder that it made before the error stays.
    return f"❌ The folder of the BOM cannot be made: {error}. The tool wrote no file."


async def _export_all_variants(
    root: Path,
    project: Path,
    kicad: str,
    names: list[str],
    group_by: list[str],
    exclude_dnp: bool,
    columns: BomFields,
    output: Path,
    variants: list[str],
) -> str:
    """Export the BOM of the default variant and of each variant of the project.

    kicad-cli exports the default variant only with no --variant
    (EESCHEMA_JOBS_HANDLER::JobExportBom), thus the tool runs kicad-cli one time
    for each variant, one after the other. It writes the files only after all
    exports, and a failure changes no file of the set (_save_set).
    """
    # The same checks as for the file of the default variant (also a symbolic link
    # to a KiCad file).
    targets = [
        (variant, _resolve_output(str(target), project))
        for variant, target in variant_outputs(output, variants)
    ]
    results = []
    for variant, target in targets:
        args = bom_args(names, group_by, exclude_dnp, variant)
        try:
            export, rows = await asyncio.to_thread(export_bom_file, root, args)
        except KicadCliError as e:
            return (
                f"❌ kicad-cli did not write the BOM of {variant_label(variant)} of {root}: "
                f"{str(e).rstrip('.')}. The tool changed no file."
            )
        results.append((variant, target, export, rows))
    # Before the write: an error of the tool in the check then changes no file.
    units = await asyncio.to_thread(_unit_check, project, kicad, variants)
    try:
        replaced, saved = await asyncio.to_thread(
            _save_set, [(target, export.data) for _variant, target, export, _rows in results]
        )
    except OSError as e:
        return _save_error(e)
    count = len(variants)
    lines_out = [
        f"# BOM of {root.name}, all variants",
        "",
        *units,
        f"kicad-cli of KiCad {kicad} (`sch export bom`), one run and one CSV file for each "
        + (
            f"variant: the default variant and {count} variant{'s' if count > 1 else ''}."
            if count
            else "variant: the project has no variants, thus one BOM file."
        ),
        *(f"⚠️ {warning}" for warning in saved),
    ]
    for (variant, target, export, rows), was in zip(results, replaced, strict=True):
        name = f"Variant {variant!r}" if variant else "Default variant"
        state = "replaced" if was else "written"
        lines_out += [
            "",
            f"## {name}",
            "",
            f"{len(rows) - 1} lines. The CSV file is {state}: `{target}`",
        ]
        lines_out += _file_warnings(export, rows, columns)
    lines_out += ["", _BOM_NOTE, ""]
    if group_by:
        lines_out.append(_group_warning(columns))
    else:
        paths = ", ".join(json.dumps(str(target)) for _variant, target, _export, _rows in results)
        lines_out.append(
            f"For project_check of datasheet-lens: bom=[{paths}] (the default variant first)."
        )
    lines_out.append(
        "The tool gives no tables for all variants. Give variant for the table of one variant."
    )
    return "\n".join(lines_out)


async def _export_bom(
    schematic_path: str,
    output_path: str,
    fields: list[str] | None,
    group_by: list[str] | None,
    exclude_dnp: bool,
    variant: str,
    all_variants: bool = False,
) -> str:
    path = Path(os.path.abspath(os.path.expanduser(schematic_path)))
    if path.suffix != ".kicad_sch" or not path.is_file():
        return f"❌ The schematic {path} does not exist or is not a .kicad_sch file."
    if variant and all_variants:
        return "❌ Give variant or all_variants, not the two."
    if all_variants and not output_path:
        return (
            "❌ all_variants writes one CSV file for each variant: give output_path (the file "
            "of the default variant)."
        )
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"
    major = bom_rules(kicad)
    if (variant or all_variants) and major < 10:
        return f"❌ kicad-cli of KiCad {kicad} has no variants. KiCad 10 and later have them."
    names = list(DEFAULT_FIELDS) if fields is None else fields
    args = bom_args(names, group_by or [], exclude_dnp, variant)
    columns = bom_fields(names, group_by or [], major)
    try:
        project = await asyncio.to_thread(schematic_project_file, path)
    except AmbiguousRootError as e:
        return f"❌ {e}. Give the root schematic of the project."
    root, _tops = await asyncio.to_thread(netlist_root, project, kicad, "BOM")
    if variant or all_variants:
        try:
            known = await asyncio.to_thread(variant_names, project)
        except (OSError, KiCadEncodingError, SExprError) as e:
            return f"❌ The variants of the project {project} cannot be read: {e}"
        if all_variants:
            output = _resolve_output(output_path, project)
            variants = other_variants(known)
            return await _export_all_variants(
                root, project, kicad, names, group_by or [], exclude_dnp, columns, output, variants
            )
        if variant not in known:
            # JobExportBom uses the default variant for an unknown name, with no message.
            listed = ", ".join(repr(name) for name in sorted(known)) or "none"
            return (
                f"❌ The project {project} has no variant {variant!r}. Its variants: {listed}. "
                "Give no variant for the default variant."
            )
    output = _resolve_output(output_path, project) if output_path else None
    try:
        export, rows = await asyncio.to_thread(export_bom_file, root, args)
    except KicadCliError as e:
        return f"❌ kicad-cli did not write the BOM of {root}: {e}"
    # Before the write: an error of the tool in the check then changes no file.
    units = await asyncio.to_thread(_unit_check, project, kicad, [variant] if variant else [])
    replaced, saved = False, []
    if output is not None:
        try:
            (replaced,), saved = await asyncio.to_thread(_save_set, [(output, export.data)])
        except OSError as e:
            return _save_error(e)
    return _bom_text(
        root, kicad, export, rows, columns, output, replaced, variant, bool(group_by), units, saved
    )


@mcp.tool()
@with_current_schematics("schematic_path")
async def export_bom(
    schematic_path: str,
    output_path: str = "",
    fields: list[str] | None = None,
    group_by: list[str] | None = None,
    exclude_dnp: bool = False,
    variant: str = "",
    all_variants: bool = False,
) -> str:
    """Give the bill of materials (BOM) of a schematic, as KiCad makes it.

    The tool runs "kicad-cli sch export bom" for the full project of the
    schematic, and gives its lines as a table. Without group_by, each
    reference is one line. KiCad does not put the power symbols and the
    symbols that are excluded from the BOM into the BOM.

    The units of one package (one reference, U1A and U1B) must have the same
    Value, MPN, Value_Confirmed and MPN_Confirmed. When they do not, the output starts
    with an error of the design: the BOM line of the package joins the values, and the
    joined MPNs look like an MPN and its alternative. Tell the user, and
    correct the fields in KiCad.

    For project_check of datasheet-lens, give output_path and no group_by.
    Then give the path of the CSV file to project_check (argument bom). For a
    project with variants (KiCad 10), also give all_variants: then give the
    list of the files that the tool names to project_check.

    Args:
        schematic_path: A .kicad_sch file of the project. The BOM is the BOM
            of the full project, also for a sub-sheet.
        output_path: The CSV file to write, as kicad-cli writes it. A relative
            path is relative to the project directory. The tool does not
            expand variables. The tool replaces an existing file. Empty: the
            tool keeps no file.
        fields: The fields of the BOM, in their order. "*" adds all other
            fields. A generated field has the form "${QUANTITY}" or "${DNP}".
            Default: Reference, Value, Footprint, Datasheet, ${QUANTITY},
            ${DNP} and "*".
        group_by: The fields whose equal values put symbols into one line.
            Each name must be an item of fields: a field that "*" adds does
            not group the symbols. Default: no groups.
        exclude_dnp: True to remove the symbols that have DNP.
        variant: The name of a variant of the project (KiCad 10 and later).
            Empty: the default variant.
        all_variants: True to export the default variant and each variant of
            the project (KiCad 10 and later), one kicad-cli run and one CSV
            file for each: the default variant into output_path, the variant X
            into <stem>-X<suffix> next to it (bom.csv: bom-X.csv). It needs
            output_path, and gives no tables. Not with variant.

    Returns:
        The errors of the design (units of one package with different
        fields), the number of lines, the path of the CSV file, the messages
        of kicad-cli, the warnings, and the table of the BOM. With all_variants:
        for each variant the number of lines, the path and the warnings, and
        the list of the files for project_check.
    """
    try:
        return await _export_bom(
            schematic_path, output_path, fields, group_by, exclude_dnp, variant, all_variants
        )
    except Exception as e:
        return error_answer(e, "Error")
