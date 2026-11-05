"""The tool check_symbol_fields: the check of the confirmed MPNs with no datasheet-lens.

The tool reads the BOM of kicad-cli (one line for each reference, no DNP
symbols, the default variant and each variant of KiCad 10), as project_check of
datasheet-lens reads the BOM that export_bom writes. Then it checks the fields
Value, MPN and Value_Confirmed of each line by the model of
parsers/confirmed_mpns.py. Thus the two tools give the same messages.

The units of one package with different fields (parsers/unit_fields.py) are a
conflict of their line: the BOM line joins their values, and the joined MPNs
look like an MPN and its alternative.
"""

import asyncio
import json
import os
import traceback
from pathlib import Path

from ..parsers.confirmed_mpns import FieldCheck, FieldWrite, bom_symbols, check_symbols
from ..parsers.kicad_versions import KicadFileError
from ..parsers.sexpr import SExprError
from ..parsers.unit_fields import distinct_conflicts, variant_unit_conflicts
from ..server import mcp
from ..utils.kicad_bom import (
    bom_args,
    bom_rules,
    export_bom_file,
    other_variants,
    unit_check_lines,
    variant_label,
)
from ..utils.kicad_cli import get_kicad_version
from ..utils.kicad_text import KiCadEncodingError
from ..utils.sch_hierarchy import AmbiguousRootError, schematic_project_file
from ..utils.variants import variant_names
from .library import NO_KICAD_CLI, netlist_root

# The fields of the BOM: "*" adds each field of the symbols (MPN, Value_Confirmed).
_FIELDS = ["Reference", "Value", "*"]


# The conflicts of a BOM line that joins the fields of more symbols. A proposal for
# such a line would write the joined values. The texts have no variant name: a
# variant line with the conflict of the default line gives no second message
# (check_symbols). The error of the design above the messages gives the values.
_UNITS = "the units of one package have different fields (see the error of the design above)"
_DUPLICATE = (
    "a duplicate reference: KiCad joins the fields of its symbols in one BOM line. "
    "Annotate the schematic in KiCad first"
)


def _code(item: FieldWrite) -> str:
    """The change of set_symbol_fields for a write, as inline code."""
    change: dict[str, object] = {"reference": item.reference, "fields": item.fields}
    if item.variant:
        change["variant"] = item.variant
    text = json.dumps(change, ensure_ascii=False)
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _target(item: FieldWrite) -> str:
    """Where a confirmation writes: a variant of KiCad 10 has its own fields."""
    if item.variant:
        return f"{item.reference} (variant {item.variant})"
    return item.reference


def _messages(result: FieldCheck) -> list[str]:
    """The messages of check_symbols with the next step for each symbol, as
    format_check of datasheet-lens gives them."""
    lines = []
    if result.conflicts:
        lines += ["## Conflicts", ""]
        lines += [
            f"- ❌ conflict in the CAD project: {c}. Tell the user: one of the fields is wrong."
            for c in result.conflicts
        ]
        lines.append("")
    if result.changes:
        lines += ["## Changes", ""]
        for c in result.changes:
            first = (
                " First select the new part with the user, and write its MPN into Value in "
                "parentheses ('100nF 10V (GRM155R71A104KA01D)')."
                if c.select
                else ""
            )
            lines.append(
                f"- ⚠️ change in the CAD project: {_target(c)}: {c.text} Tell the user. When the "
                f"user confirms the change:{first} Write {_code(c)}. Else the Value must change "
                "back."
            )
        lines.append("")
    if result.unconfirmed:
        lines += ["## Not confirmed", ""]
        lines += [
            f"- not confirmed: {_target(u)}:{u.text} When the user agrees: Write {_code(u)}."
            for u in result.unconfirmed
        ]
        lines.append("")
    if result.complete:
        lines += ["## Complete the fields", ""]
        lines += [
            f"- complete the fields: {_target(c)}: the MPN is not in Value (the convention: the "
            f"MPNs in parentheses in Value). The parts do not change. Write {_code(c)}."
            for c in result.complete
        ]
        lines.append("")
    if result.no_value_confirmed:
        lines.append(
            f"⚠️ no field Value_Confirmed: {result.no_value_confirmed} symbols. A change of a "
            "general value (100nF to 220nF) is not found. Tell the user. When the user agrees, "
            "write the hidden field Value_Confirmed with the text of Value into each symbol "
            "whose fields are confirmed (set_symbol_fields), then run check_symbol_fields again."
        )
    if result.no_mpn:
        lines.append(
            f"no part selected (a general value and no MPN): {', '.join(result.no_mpn)}. A part "
            "that needs a datasheet: select it with the user, then write its MPN into Value in "
            "parentheses ('10k (RC0603FR-0710KL)'), into MPN, and the new Value into "
            "Value_Confirmed. A symbol that needs no part (a test point, a generic resistor): "
            "MPN = '-'."
        )
    return lines


async def _check(schematic_path: str) -> str:
    path = Path(os.path.abspath(os.path.expanduser(schematic_path)))
    if path.suffix != ".kicad_sch" or not path.is_file():
        return f"❌ The schematic {path} does not exist or is not a .kicad_sch file."
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {NO_KICAD_CLI}"
    major = bom_rules(kicad)
    try:
        project = await asyncio.to_thread(schematic_project_file, path)
    except AmbiguousRootError as e:
        return f"❌ {e}. Give the root schematic of the project."
    root, _tops = await asyncio.to_thread(netlist_root, project, kicad, "BOM")
    try:
        variants = []
        if major >= 10:
            variants = other_variants(await asyncio.to_thread(variant_names, project))
        units = await asyncio.to_thread(variant_unit_conflicts, project, variants)
    except (OSError, KiCadEncodingError, SExprError, KicadFileError, ValueError) as e:
        # The tool cannot check the units of one package: a proposal for such a BOM
        # line would write its joined values.
        return f"❌ The schematic files of the project {project} cannot be read: {e}"
    if units.hierarchy:
        # The symbols of a sheet that the tool did not read have no compare of their
        # units (as set_symbol_fields, which refuses such a project).
        return (
            f"❌ The tool cannot read the full sheet hierarchy of {project}, thus it cannot "
            "compare the fields of the units of one package: "
            + " ".join(units.hierarchy)
            + " Open the schematic in KiCad and save it, then run the check again."
        )
    duplicates = {reference.upper() for reference in units.duplicates}

    symbols = []
    counts = []
    cli_messages = []
    for variant in ["", *variants]:
        args = bom_args(_FIELDS, [], True, variant)
        try:
            export, rows = await asyncio.to_thread(export_bom_file, root, args)
        except RuntimeError as e:
            return f"❌ kicad-cli did not write the BOM of {variant_label(variant)} of {root}: {e}"
        cli_messages += [m for m in export.messages if m not in cli_messages]
        lines = bom_symbols(rows, variant)
        joined = {conflict.reference.upper() for conflict in units.conflicts[variant]}
        for symbol in lines:
            key = symbol.reference.upper()
            symbol.unit_conflicts = [_UNITS] if key in joined else []
            symbol.unit_conflicts += [_DUPLICATE] if key in duplicates else []
        symbols += lines
        counts.append(f"{variant_label(variant)}: {len(rows) - 1}")

    result = check_symbols(symbols)
    unit_errors = distinct_conflicts(units.conflicts)
    out = [f"# Fields of the symbols of {root.name}", ""]
    out += unit_check_lines(unit_errors, units.problems)
    out.append(
        f"kicad-cli of KiCad {kicad} (`sch export bom`, no DNP symbols), the lines of "
        f"{', '.join(counts)}. Symbols with a part: {result.lines}."
    )
    out += [f"⚠️ kicad-cli: {message}" for message in cli_messages]
    out.append("")
    messages = _messages(result)
    if messages:
        out += messages
        out += [
            "",
            "Write the fields with set_symbol_fields, only after the user confirmed them. Give "
            "all changes in one call: a symbol in a sheet that the hierarchy uses more than one "
            "time needs the same fields for each of its references.",
        ]
    elif unit_errors or units.problems or cli_messages:
        # The error of the units can be in a symbol that is not in the BOM (DNP), and a
        # message of kicad-cli can tell that the BOM is not complete.
        out.append(
            "⚠️ The BOM lines give no message, but the tool did not check the full project: "
            "see the errors and the warnings above."
        )
    else:
        out.append(
            "✅ The fields Value, MPN and Value_Confirmed of all BOM lines (all symbols that "
            "are not DNP, in each variant) follow the convention, and the user confirmed them."
        )
    out += [
        "",
        "The convention: Value holds the general value and the MPNs in one text in "
        "parentheses, the fitted part first: '2N7002 (2N7002LT1G, BSS138LT1G)'. MPN is the "
        "confirmed copy of these MPNs, and the hidden field Value_Confirmed is the Value of "
        "the last confirmation.",
    ]
    return "\n".join(out)


@mcp.tool()
async def check_symbol_fields(schematic_path: str) -> str:
    """Check the MPNs of the symbols: the fields Value, MPN and Value_Confirmed.

    Use this tool only when the MCP server datasheet-lens is not available: its
    tool project_check does the same check, and also checks the datasheets. The
    two tools give the same messages.

    The convention: Value holds the general value and the MPNs in one text in
    parentheses, the fitted part first: "2N7002 (2N7002LT1G, BSS138LT1G)". The
    separators are "," and ";". An MPN with ",", ";" or a parenthesis is in
    quotes, as in CSV. The field MPN is the confirmed copy of these MPNs. The
    hidden field Value_Confirmed is the Value of the last confirmation.

    The tool reads the BOM of kicad-cli (no DNP symbols) of the default variant
    and of each variant (KiCad 10). For each symbol it gives the conflicts, the
    changes that the user must confirm, the symbols that are not confirmed, the
    fields that complete a symbol by the convention, and the symbols with no
    part. Each message gives the change for set_symbol_fields. Write the fields
    only after the user confirmed them.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool checks the
            full project.

    Returns:
        The errors of the design (units of one package with different
        fields), the messages for each symbol with the fields to write, or a
        "✅" when all fields are confirmed.
    """
    try:
        return await _check(schematic_path)
    except ValueError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Error: {e}\n\n{traceback.format_exc()}"
