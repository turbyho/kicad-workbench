"""The BOM of kicad-cli: the options of "sch export bom", its CSV file, and the
error of the units of one package with different fields.

The tools export_bom and check_symbol_fields use this module.
"""

import csv
import io
from collections.abc import Iterable
from pathlib import Path

from ..errors import InputError
from ..parsers.kicad_versions import KICAD8, KICAD9, KICAD10, kicad_release
from ..parsers.unit_fields import UnitConflict
from .kicad_cli import CliExport, export_with_kicad_cli

BOM_TIMEOUT = 300


def other_variants(names: Iterable[str]) -> list[str]:
    """Return the variants of a project with no default variant, in order of name.

    An empty name is the default variant (SCHEMATIC::SetCurrentVariant). KiCad does
    not let the user give it ("Variant name cannot be empty.",
    DIALOG_SYMBOL_FIELDS_TABLE), but a file that a person edits can have it.
    """
    return sorted(name for name in names if name)


def variant_label(variant: str) -> str:
    """Return the name of a variant for a message: "the default variant" for ""."""
    return f"the variant {variant!r}" if variant else "the default variant"


def export_bom_file(root: Path, args: list[str]) -> tuple[CliExport, list[list[str]]]:
    """Run kicad-cli sch export bom with args for the schematic root, and return the
    export and its lines (read_bom). Run it off the event loop.

    Raises:
        KicadCliError: kicad-cli did not write the BOM.
        InputError: The BOM is not UTF-8, or it has no header line.
    """
    export = export_with_kicad_cli(["sch", "export", "bom", *args], root, "bom.csv", BOM_TIMEOUT)
    return export, read_bom(export.data)


def bom_rules(kicad: str) -> int:
    """Return the major version of the BOM rules of the installed KiCad: 8, 9 or 10.

    Raises:
        InputError: The server has no BOM rules for the version. JobExportBom of
            the nightly builds 10.99 (branch master, 2026-11-04) reads --group-by
            with other rules: it adds a field that --fields does not name.
    """
    release = kicad_release(kicad)
    for known, major in ((KICAD8, 8), (KICAD9, 9), (KICAD10, 10)):
        if release is known:
            return major
    raise InputError(
        f"The installed KiCad is {kicad}. This server knows the BOM rules of kicad-cli of "
        "KiCad 8.0, 9.0 and 10.0."
    )


def _check_names(names: list[str], what: str) -> None:
    """Refuse the field names that kicad-cli cannot get.

    kicad-cli splits the lists of --fields and --group-by at each comma
    (SCH_EXPORT_BOM_COMMAND::convertStringList).
    """
    for name in names:
        if not name:
            raise InputError(f"An item of {what} is empty.")
        if "," in name:
            raise InputError(
                f"The field name {name!r} of {what} has a comma. kicad-cli splits the "
                "list of fields at each comma, thus it cannot export this field."
            )


def bom_args(fields: list[str], group_by: list[str], exclude_dnp: bool, variant: str) -> list[str]:
    """Return the options of kicad-cli sch export bom for the tool arguments.

    The tool gives no labels: the label of a column is then the name of its
    field, and the name with no "${}" for a generated field.

    Raises:
        InputError: kicad-cli cannot do the export that the arguments ask for.
    """
    if not fields:
        raise InputError("fields is empty. Give the fields of the BOM, or no fields.")
    _check_names(fields, "fields")
    _check_names(group_by, "group_by")
    args = ["--fields", ",".join(fields)]
    if group_by:
        args += ["--group-by", ",".join(group_by)]
    if exclude_dnp:
        args.append("--exclude-dnp")
    if variant:
        args += ["--variant", variant]
    return args


def read_bom(data: bytes) -> list[list[str]]:
    """Read the CSV file of kicad-cli: the header line, then one list for each line.

    kicad-cli writes UTF-8 (wxFile::Write with wxConvAuto), a comma between
    the values and each value between double quotes
    (FIELDS_EDITOR_GRID_DATA_MODEL::Export with the default options).

    Raises:
        InputError: The file is not UTF-8, or it has no header line.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InputError(f"The BOM of kicad-cli is not UTF-8: {e}") from e
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        raise InputError("The BOM of kicad-cli has no header line.")
    return rows


def unit_check_lines(conflicts: list[UnitConflict], problems: list[str]) -> list[str]:
    """Return the error lines of the units of one package with different fields, and
    the warning when the compare did not read each sheet."""
    lines_out = []
    if conflicts:
        lines_out += [
            "❌ ERROR IN THE DESIGN: the units of one package have different fields. One "
            "package has one Value, one MPN and one Value_Confirmed. Correct the fields in "
            "KiCad. The line of the package in the BOM joins the values: KiCad 9 and 10 "
            'write "A,B", which looks like an MPN and its alternative, and KiCad 8 writes '
            '"-- mixed values --".',
            *(f"- {conflict.text()}" for conflict in conflicts),
            "",
        ]
    if problems:
        lines_out.append(
            "⚠️ The compare of the fields of the units did not read each sheet: "
            + " ".join(problems)
        )
    return lines_out
