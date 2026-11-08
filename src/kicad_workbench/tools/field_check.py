"""The tool check_symbol_fields: the check of the confirmed MPNs with no datasheet-lens.

The tool reads the BOM of kicad-cli (one line for each reference, also the DNP
symbols, the default variant and each variant of KiCad 10), as project_check of
datasheet-lens reads the BOM that export_bom writes. Then it checks the fields
Value, MPN, Value_Confirmed and MPN_Confirmed of each line by the model of
parsers/confirmed_mpns.py. Thus the two tools give the same messages.

A DNP symbol gets the same check as a fitted symbol (datasheet-lens, commit 65168da
of 2026-11-07; an older datasheet-lens does not check the DNP symbols):
DNP is a setting of the CAD tool that the user can change at any time, and the
symbol stays in the schematic.

The units of one package with different fields (parsers/unit_fields.py) are a
conflict of their line: the BOM line joins their values, and the joined MPNs
look like an MPN and its alternative.
"""

import asyncio
import json
import os
from pathlib import Path

from ..errors import InputError, WorkbenchError
from ..parsers.annotation import kicad_major
from ..parsers.confirmed_mpns import (
    CadSymbol,
    FieldCheck,
    FieldWrite,
    bom_symbols,
    check_symbols,
)
from ..parsers.unit_fields import (
    distinct_conflicts,
    is_confirmed_field,
    is_mpn_confirmed_field,
    mpn_order,
    variant_unit_conflicts,
)
from ..server import mcp
from ..utils.kicad_bom import (
    bom_args,
    bom_rules,
    export_bom_file,
    other_variants,
    unit_check_lines,
    variant_label,
)
from ..utils.kicad_cli import KicadCliError, get_kicad_version, no_kicad_message
from ..utils.sch_hierarchy import AmbiguousRootError, netlist_root, schematic_project_file
from ..utils.schematic_versions import with_schematic_versions
from ..utils.tool_errors import error_answer
from ..utils.variants import variant_names

# The fields of the BOM: "*" adds each field of the symbols (MPN, Value_Confirmed,
# MPN_Confirmed).
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
_NOT_ANNOTATED = (
    "a reference that is not annotated: set_symbol_fields cannot find the symbol. Annotate "
    "the schematic in KiCad first"
)


def _code(item: FieldWrite) -> str:
    """The change of set_symbol_fields for a write, as inline code. A field that the
    user selects has a name in angle brackets (see _choices)."""
    fields = dict(item.fields)
    for role, text, names in item.choices:
        fields[f"<the field for {role}: {' or '.join(names)}>"] = text
    change: dict[str, object] = {"reference": item.reference, "fields": fields}
    if item.variant:
        change["variant"] = item.variant
    text = json.dumps(change, ensure_ascii=False)
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _own_fields(symbol: CadSymbol, names: list[str]) -> None:
    """Write into the fields that the symbol has, when its BOM line has no value of
    the role: the BOM cannot tell if a symbol has an empty field. Not in datasheet-lens,
    which reads only the BOM. names: the fields of the symbol (UnitCheck.fields).
    The names of more fields of one role (for example in different units): not
    known, the user selects the field."""
    if not symbol.mpn:
        own = list(dict.fromkeys(names[i] for i in mpn_order(names)))
        if own:
            symbol.mpn_fields, symbol.mpn_known = own, len(own) == 1
    if symbol.value_confirmed == "":  # None: the BOM has no field Value_Confirmed
        own = [n for n in names if is_confirmed_field(n)]
        if own:
            symbol.confirmed_fields, symbol.confirmed_known = own, len(own) == 1
    if symbol.mpn_confirmed == "":  # None: the BOM has no field MPN_Confirmed
        own = [n for n in names if is_mpn_confirmed_field(n)]
        if own:
            symbol.mpn_confirmed_fields, symbol.mpn_confirmed_known = own, len(own) == 1


def _choices(item: FieldWrite) -> str:
    """The fields of a write that the user selects (see write_fields)."""
    return "".join(
        f" The field for {role} is not known ({', '.join(names)}): ask the user which one the "
        "symbol uses, and put its name in place of the name in angle brackets."
        for role, _text, names in item.choices
    )


def _missing(item: FieldWrite, own: dict[str, list[str]]) -> str:
    """The fields of a write into a variant that the symbol does not have: a variant
    can set only a field that the symbol has (set_symbol_fields)."""
    missing = [
        name
        for name in item.fields
        if name != "Value" and name not in own.get(item.reference.upper(), [])
    ]
    if not item.variant or not missing:
        return ""
    what = "it" if len(missing) == 1 else "them"
    return (
        f" The symbol has no field {', '.join(missing)}: first write {what} into the symbol "
        "(a change with no variant)."
    )


def _target(item: FieldWrite) -> str:
    """Where a confirmation writes: a variant of KiCad 10 has its own fields."""
    if item.variant:
        return f"{item.reference} (variant {item.variant})"
    return item.reference


def _messages(result: FieldCheck, own: dict[str, list[str]]) -> list[str]:
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
            if c.select:
                lines.append(
                    f"- ⚠️ change in the CAD project: {_target(c)}: {c.text} Ask the user: a new "
                    "part for the new value, or the same part. A new part: select it with the user, "
                    "write its MPN into the field MPN, then run check_symbol_fields again. The same "
                    f"part: Write {_code(c)}.{_choices(c)}{_missing(c, own)} Else the Value must "
                    "change back."
                )
                continue
            # The datasheet of a new MPN: the agent finds it at once, before it asks the
            # user (decision 28). This tool runs with no datasheet-lens.
            new = (
                " Find the datasheet of each new MPN now (this changes no project), and give "
                "it to the user with the change."
                if c.new
                else ""
            )
            lines.append(
                f"- ⚠️ change in the CAD project: {_target(c)}: {c.text}{new} Tell the user. When "
                f"the user confirms the change: Write {_code(c)}.{_choices(c)}{_missing(c, own)} "
                "Else the Value and the MPN must change back."
            )
        lines.append("")
    if result.unconfirmed:
        lines += ["## Not confirmed", ""]
        lines += [
            f"- not confirmed: {_target(u)}:{u.text} When the user agrees: Write {_code(u)}."
            + _choices(u)
            + _missing(u, own)
            for u in result.unconfirmed
        ]
        lines.append("")
    if result.move:
        lines += ["## The old convention", ""]
        for m in result.move:
            if m.old == "Value_Confirmed":  # decision 32: only the confirmed copies change
                text = (
                    "Value_Confirmed has the MPNs in parentheses, and Value and the field MPN "
                    "follow the new convention. Write the confirmed copies by the new convention: "
                    "the confirmed part stays, and the parts do not change. Tell the user, and when "
                    "the user agrees"
                )
            else:
                text = (
                    "the MPNs are in the parentheses of Value. Move them into the field MPN (Value "
                    "holds the general value, MPN the MPNs, the hidden fields Value_Confirmed and "
                    "MPN_Confirmed the confirmed copies). A confirmed symbol stays confirmed, and "
                    "the parts do not change. Tell the user (a text in parentheses that is a part "
                    "of the general value stays), and when the user agrees"
                )
            lines.append(
                f"- the old convention: {_target(m)}: {text}: Write {_code(m)}."
                + _choices(m)
                + _missing(m, own)
                + " Then run check_symbol_fields again: it compares the fields again."
            )
        lines.append("")
    for count, name, what in (
        (result.no_value_confirmed, "Value_Confirmed", "of a general value (100nF to 220nF)"),
        (result.no_mpn_confirmed, "MPN_Confirmed", "of a part (an other MPN)"),
    ):
        if count:
            lines.append(
                f"⚠️ no field {name}: {count} symbols. A change {what} is not found. Tell the "
                f"user. When the user agrees, write the hidden field {name} into each symbol (the "
                "text of the field that it copies, for a symbol whose fields are confirmed), with "
                "set_symbol_fields, then run check_symbol_fields again."
            )
    if result.no_mpn:
        lines.append(
            f"no part selected (a general value and no MPN): {', '.join(result.no_mpn)}. A part "
            "that needs a datasheet: select it with the user, then write its MPN into the field "
            "MPN of the symbol, and run check_symbol_fields again. A symbol that needs no part (a "
            "test point, a generic resistor): MPN = '-'."
        )
    return lines


async def _check(schematic_path: str) -> str:
    path = Path(os.path.abspath(os.path.expanduser(schematic_path)))
    if path.suffix != ".kicad_sch" or not path.is_file():
        return f"❌ The schematic {path} does not exist or is not a .kicad_sch file."
    kicad = await get_kicad_version()
    if kicad is None:
        return f"❌ {no_kicad_message()}"
    major = bom_rules(kicad)
    try:
        kicad_major(kicad)  # the references of the symbols after the load
    except InputError as e:
        return f"❌ {e}"
    try:
        project = await asyncio.to_thread(schematic_project_file, path)
    except AmbiguousRootError as e:
        return f"❌ {e}. Give the root schematic of the project."
    root, _tops = await asyncio.to_thread(netlist_root, project, kicad, "BOM")
    try:
        variants = []
        if major >= 10:
            variants = other_variants(await asyncio.to_thread(variant_names, project))
        units = await asyncio.to_thread(variant_unit_conflicts, project, kicad, variants)
    except (OSError, WorkbenchError) as e:
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
        args = bom_args(_FIELDS, [], exclude_dnp=False, variant=variant)
        try:
            export, rows = await asyncio.to_thread(export_bom_file, root, args)
        except KicadCliError as e:
            return f"❌ kicad-cli did not write the BOM of {variant_label(variant)} of {root}: {e}"
        cli_messages += [m for m in export.messages if m not in cli_messages]
        lines = bom_symbols(rows, variant)
        joined = {conflict.reference.upper() for conflict in units.conflicts[variant]}
        for symbol in lines:
            key = symbol.reference.upper()
            symbol.unit_conflicts = [_UNITS] if key in joined else []
            symbol.unit_conflicts += [_DUPLICATE] if key in duplicates else []
            symbol.unit_conflicts += [_NOT_ANNOTATED] if key.endswith("?") else []
            _own_fields(symbol, units.fields.get(key, []))
        symbols += lines
        counts.append(f"{variant_label(variant)}: {len(rows) - 1}")

    result = check_symbols(symbols)
    unit_errors = distinct_conflicts(units.conflicts)
    out = [f"# Fields of the symbols of {root.name}", ""]
    out += unit_check_lines(unit_errors, units.problems)
    out.append(
        f"kicad-cli of KiCad {kicad} (`sch export bom`, also the DNP symbols), the lines of "
        f"{', '.join(counts)}. Symbols with a part: {result.lines}."
    )
    out += [f"⚠️ kicad-cli: {message}" for message in cli_messages]
    out.append("")
    messages = _messages(result, units.fields)
    if messages:
        out += messages
        out += [
            "",
            "Write the fields with set_symbol_fields, only after the user confirmed them. Give "
            "all changes in one call: a symbol in a sheet that the hierarchy uses more than one "
            "time needs the same fields for each of its references.",
        ]
    elif unit_errors or units.problems or cli_messages:
        # The error of the units can be in a symbol that is not in the BOM (Exclude from
        # BOM), and a message of kicad-cli can tell that the BOM is not complete.
        out.append(
            "⚠️ The BOM lines give no message, but the tool did not check the full project: "
            "see the errors and the warnings above."
        )
    else:
        out.append(
            "✅ The fields Value, MPN, Value_Confirmed and MPN_Confirmed of all BOM lines (also "
            "the DNP symbols, in each variant) follow the convention, and the user confirmed them."
        )
    out += [
        "",
        "The convention: Value holds the general value ('2N7002', '100nF 10V'), MPN the MPNs, "
        "the fitted part first ('2N7002LT1G, BSS138LT1G'), and the hidden fields "
        "Value_Confirmed and MPN_Confirmed the copies of the last confirmation.",
    ]
    return "\n".join(out)


@mcp.tool()
@with_schematic_versions("schematic_path")
async def check_symbol_fields(schematic_path: str) -> str:
    """Check the MPNs of the symbols: the fields Value, MPN, Value_Confirmed and MPN_Confirmed.

    Use this tool only when the MCP server datasheet-lens is not available: its
    tool project_check does the same check, and also checks the datasheets. The
    two tools give the same messages (a datasheet-lens with the model of the four
    fields, of 2026-11-07 or later: an older one reads the MPNs from the
    parentheses of Value).

    The convention: Value holds the general value ("2N7002", "100nF 10V"), the
    field MPN the MPNs, the fitted part first ("2N7002LT1G, BSS138LT1G"; the
    separators are "," and ";", an MPN with "," or ";" is in quotes, as in CSV),
    and the hidden fields Value_Confirmed and MPN_Confirmed the copies of the last
    confirmation. A symbol with the MPNs in the parentheses of Value (the old
    convention) gets the move into the field MPN.

    The tool reads the BOM of kicad-cli (also the DNP symbols: the user can fit
    them at any time) of the default variant and of each variant (KiCad 10).
    For each symbol it gives the conflicts, the changes that the user must
    confirm, the symbols that are not confirmed, the moves of the old convention,
    and the symbols with no part. Each message gives
    the change for set_symbol_fields. Write the fields only after the user
    confirmed them.

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
    except Exception as e:
        return error_answer(e, "Error")
