"""Schematic analysis tools for KiCad Workbench."""

import asyncio
import copy
import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import InputError
from ..parsers.annotation import ref_des_prefix
from ..parsers.kicad_fields import with_field_rules_note
from ..parsers.netlist_parser import NetlistNode
from ..parsers.sch_instances import SchematicFile, placed_instances
from ..parsers.schematic_parser import (
    SchematicComponent,
    SchematicParser,
    merge_units,
    unknown_alternate_note,
)
from ..parsers.sheet_list import EMPTY_REASONS
from ..server import mcp
from ..utils.kicad_cli import get_kicad_version
from ..utils.sch_hierarchy import AmbiguousRootError, schematic_project_file
from ..utils.schematic_upgrade import with_current_schematics
from ..utils.tool_errors import error_answer
from .netlist import NetlistError, netlist_of

_NO_VERSION = (
    "❌ The version of KiCad is not known (kicad-cli does not run): the references of the "
    "symbols of each sheet path depend on it. Install KiCad or set KICAD_CLI."
)


@dataclass
class _Placed:
    """A component in one sheet path, with the reference and the unit of KiCad."""

    component: SchematicComponent
    sheet: str  # the sheet names of the path, for example "/Power/"
    file: Path
    # SCH_SYMBOL::GetLibSymbolRef: only a symbol with a library symbol is in the
    # netlist (spike/kicad-versions/component_refs_check.py compares the two).
    has_library_symbol: bool


def placed_components(
    sch_path: Path, kicad: str, project_wide: bool = False
) -> tuple[list[_Placed], list[str]]:
    """Return the components and the problems of placed_components_and_files."""
    placed, _screens, problems = placed_components_and_files(sch_path, kicad, project_wide)
    return placed, problems


def placed_components_and_files(
    sch_path: Path, kicad: str, project_wide: bool = False
) -> tuple[list[_Placed], dict[str, SchematicFile], list[str]]:
    """Return the components of a schematic file in each of its sheet paths (with
    project_wide: of all files of the project), the files of the hierarchy as KiCad
    loads them (sch_instances.SchematicFile, by Screen.key: one for each name of a
    file), and the problems of the hierarchy.

    The reference, the unit and the Value of each sheet path are the values that
    KiCad gives after the load (sch_instances.placed_instances with the view "cli",
    as the netlist and the BOM of kicad-cli): a sheet that the hierarchy uses more
    than one time gives each path its own reference (C201, C301), and a symbol
    with "U?" in its Reference field has the reference of its instance. The
    Reference field and the (unit N) of a symbol are the values of one path only.
    A power symbol and a reference that starts with "#" are no components. The
    function blocks: run it off the event loop.

    Raises:
        AmbiguousRootError: More than one project holds the file.
        KiCadEncodingError, SExprError, OSError, KicadFileError, InputError: A file
            cannot be read (placed_instances).
    """
    project = schematic_project_file(sch_path)
    placed, screens, problems = placed_instances(project, kicad, "cli")
    target = sch_path.resolve()
    parsers: dict[Path, SchematicParser] = {}
    result = []
    for item in placed:
        if not project_wide and item.file.resolve() != target:
            continue
        parser = parsers.get(item.file)
        if parser is None:
            parser = parsers[item.file] = SchematicParser(str(item.file), kicad)
        component = parser.placed_component(
            item.symbol.start, item.reference, item.unit, item.value
        )
        if component is not None:
            result.append(_Placed(component, item.sheet.shown, item.file, item.has_library_symbol))
    return result, screens, problems


def project_components(sch_path: Path, kicad: str) -> list[SchematicComponent]:
    """Return the components of the project, one for each reference: the units of a
    reference are one component (the pins of all units, the fields of the first
    unit: merge_units), with the references and units of KiCad
    (placed_components). The function blocks: run it off the event loop."""
    placed, _problems = placed_components(sch_path, kicad, project_wide=True)
    return merge_units([item.component for item in placed])


async def components_of_project(sch_path: Path, kicad: str | None) -> list[SchematicComponent]:
    """project_components off the event loop. kicad: the version of KiCad (of the
    netlist); None: the version of kicad-cli.

    Raises:
        InputError: The version of KiCad is not known.
    """
    kicad = kicad or await get_kicad_version()
    if kicad is None:
        raise InputError(_NO_VERSION.removeprefix("❌ "))
    return await asyncio.to_thread(project_components, sch_path, kicad)


class BaseNames:
    """The names of the library pins of a component for the nodes of the netlist.

    The netlist gives the shown name (with the alternate), the schematic model gives
    the name of the library pin ("base_name", SchematicParser._component_pins).
    """

    def __init__(self, component: SchematicComponent | None) -> None:
        """component: The component of the project (project_components), or None."""
        self._pins: dict[str, list[dict]] = {}
        for pin in component.pins if component is not None else []:
            self._pins.setdefault(str(pin.get("number", "")), []).append(pin)

    def of(self, node: NetlistNode) -> str:
        """Return the name of the library pin of a node, or "" when it is not known.

        A pin number of more than one library pin: the pin whose shown name is the
        name of the node.
        """
        pins = self._pins.get(node.number, [])
        if len(pins) > 1:
            pins = [pin for pin in pins if pin.get("name", "") == node.name] or pins
        names = {pin.get("base_name", pin.get("name", "")) for pin in pins}
        return names.pop() if len(names) == 1 else ""


def _problem_lines(problems: list[str]) -> list[str]:
    return [f"⚠️ {problem}" for problem in problems]


@mcp.tool()
@with_current_schematics("file_path")
@with_field_rules_note
async def list_schematic_components(
    file_path: str,
    filter_type: str | None = None,
    filter_value: str | None = None,
    filter_dnp: bool | None = None,
) -> str:
    """List all components in a KiCad schematic file.

    Each placed symbol of the file is in the list one time for each sheet path of
    the file, with the reference and the unit that KiCad gives that path (a sheet
    that the hierarchy uses two times gives two references). Power symbols are not
    components. The tool needs kicad-cli (the rules of the KiCad version).

    Args:
        file_path: Path to .kicad_sch file
        filter_type: Optional filter by component type prefix (e.g., 'R', 'C', 'U', 'IC')
        filter_value: Optional filter by component value (partial match)
        filter_dnp: Optional filter by DNP flag (True=only DNP, False=only non-DNP)

    Returns:
        Formatted list of components with their properties
    """
    try:
        path = Path(file_path)
        if not path.is_file():
            return f"❌ File not found: {file_path}"
        kicad = await get_kicad_version()
        if kicad is None:
            return _NO_VERSION
        placed, problems = await asyncio.to_thread(placed_components, path, kicad)
        sheets = {item.sheet for item in placed}

        # Apply filters
        if filter_type:
            placed = [p for p in placed if p.component.reference.startswith(filter_type.upper())]

        if filter_value:
            placed = [p for p in placed if filter_value.lower() in p.component.value.lower()]

        if filter_dnp is not None:
            placed = [p for p in placed if p.component.flags.get("dnp", False) == filter_dnp]
        components = [p.component for p in placed]

        if not components:
            return "\n\n".join(
                ["No components found matching the specified criteria.", *_problem_lines(problems)]
            )

        # Check if any non-default flags exist
        has_dnp = any(c.flags.get("dnp", False) for c in components)
        has_not_in_bom = any(not c.flags.get("in_bom", True) for c in components)

        # Format output
        header = "| Reference | Value | Footprint | Library |"
        separator = "|-----------|-------|-----------|---------|"
        if len(sheets) > 1:
            header += " Sheet |"
            separator += "-------|"
        if has_dnp or has_not_in_bom:
            header += " DNP | In BOM |"
            separator += "-----|--------|"

        lines = [
            f"# Components in {file_path}",
            f"Total: {len(components)} component(s)",
            "",
            header,
            separator,
        ]

        for item in placed:
            comp = item.component
            footprint = comp.footprint or "-"
            library = comp.library_id.split(":")[-1] if ":" in comp.library_id else comp.library_id
            row = f"| {comp.reference} | {comp.value} | {footprint} | {library} |"
            if len(sheets) > 1:
                row += f" {item.sheet} |"
            if has_dnp or has_not_in_bom:
                dnp = "Yes" if comp.flags.get("dnp", False) else ""
                in_bom = "No" if not comp.flags.get("in_bom", True) else ""
                row += f" {dnp} | {in_bom} |"
            lines.append(row)

        return "\n".join(lines + ["", *_problem_lines(problems)] if problems else lines)

    except Exception as e:
        return error_answer(e, "Error parsing schematic")


def _netlist_pins(file_path: Path, reference: str) -> tuple[list[dict[str, str]] | None, str]:
    """Return the pins of a reference as the netlist of kicad-cli gives them, and the
    note, or None and the reason when the netlist has no such reference.

    The netlist of the root schematic has the pins of the placed units of the
    reference, with the name and the electrical type that KiCad computes (also the
    alternate of a pin), sorted by StrNumCmp as KiCad sorts the pins.
    """
    try:
        netlist = netlist_of(file_path)
    except (AmbiguousRootError, NetlistError) as e:
        return None, f"kicad-cli gives no netlist ({e})"
    except InputError as e:  # not XML
        return None, str(e)
    component = netlist.get_components().get(reference)
    if component is None:
        return None, f"the netlist of kicad-cli has no pin of {reference}"
    # Each node: a pin number on more than one net has more rows.
    pins = [
        {"number": node.number, "name": node.name, "electrical_type": node.type}
        for node in component.sorted_nodes()
    ]
    notes = [netlist.netlist_note(), netlist.pins_on_more_nets_note([reference])]
    return pins, "\n\n".join(note for note in notes if note)


@mcp.tool()
@with_current_schematics("file_path")
@with_field_rules_note
async def get_symbol_details(file_path: str, reference: str) -> str:
    """Get detailed information about a specific symbol/component.

    Args:
        file_path: Path to .kicad_sch file
        reference: Component reference designator (e.g., 'R1', 'U1')

    Returns:
        Detailed component information including pins and properties. The pins
        are the pins of the netlist of kicad-cli (the placed units of the
        reference, with the alternate functions). A symbol that the netlist does
        not have gets the pins of the schematic file, as the installed KiCad
        loads them. A power symbol (a reference with "#") is not a component.
    """
    try:
        path = Path(file_path)
        if not path.is_file():
            return f"❌ File not found: {file_path}"
        kicad = await get_kicad_version()
        if kicad is None:
            return _NO_VERSION
        # The reference is a reference of the project: its units can be in an other
        # file or sheet path than file_path.
        placed, problems = await asyncio.to_thread(placed_components, path, kicad, True)
        if reference.endswith("?"):
            return (
                f"❌ '{reference}' is not annotated: KiCad gives this reference to each symbol "
                "with no annotation. Annotate the schematic, then give the reference."
            )
        units = [p for p in placed if p.component.reference == reference]

        if not units:
            return "\n\n".join(
                [f"❌ Component '{reference}' not found in the project.", *_problem_lines(problems)]
            )
        found = units[0]
        component = found.component

        # Format output
        lines = [
            f"# Symbol Details: {component.reference}",
            "",
            f"**Value:** {component.value}",
            f"**Library:** {component.library_id}",
            f"**Footprint:** {component.footprint or 'Not assigned'}",
            f"**Position:** ({component.position[0]:.2f}, {component.position[1]:.2f})",
            f"**Sheet:** {found.sheet} (`{found.file.name}`)",
        ]

        if component.unit:
            lines.append(f"**Unit:** {component.unit}")
        if len(units) > 1:
            places = ", ".join(f"unit {u.component.unit} in {u.sheet}" for u in units)
            lines.append(f"**Placed units:** {places}")

        # Properties
        if component.properties:
            lines.append("")
            lines.append("## Properties")
            for key, value in sorted(component.properties.items()):
                if key not in ("Value", "Footprint"):
                    lines.append(f"- {key}: {value}")

        # Pins: the netlist of kicad-cli, else the schematic model.
        pins, note = await asyncio.to_thread(_netlist_pins, Path(file_path), component.reference)
        if pins is None:
            # The pins of all units of the reference.
            merged = merge_units([copy.deepcopy(u.component) for u in units])[0]
            pins = merged.pins
            note = f"⚠️ The pins are the pins of the schematic file: {note}."
            if alternates := unknown_alternate_note([merged]):
                note += f"\n\n{alternates}"
        if pins:
            lines.append("")
            lines.append("## Pins")
            lines.append("| Pin | Name | Type |")
            lines.append("|-----|------|------|")
            for pin in pins:
                pin_num = pin.get("number", "?")
                pin_name = pin.get("name", "")
                pin_type = pin.get("electrical_type", "")
                if "alternate" in pin and pin["alternate"] is None:
                    pin_name = pin_type = "not known"
                lines.append(f"| {pin_num} | {pin_name} | {pin_type} |")
        if note:
            lines.append("")
            lines.append(note)
        lines += ["", *_problem_lines(problems)] if problems else []

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing schematic")


@mcp.tool()
@with_current_schematics("file_path")
@with_field_rules_note
async def search_symbols(
    file_path: str,
    pattern: str,
    search_fields: str = "all",
) -> str:
    """Search for symbols/components matching a pattern.

    Args:
        file_path: Path to .kicad_sch file
        pattern: Search pattern (supports regex)
        search_fields: Fields to search: 'all', 'reference', 'value', 'library'

    Returns:
        List of matching components
    """
    try:
        path = Path(file_path)
        if not path.is_file():
            return f"❌ File not found: {file_path}"
        fields = {
            "all": ("reference", "value", "library"),
            "reference": ("reference",),
            "value": ("value",),
            "library": ("library",),
        }.get(search_fields)
        if fields is None:
            return (
                f"❌ search_fields {search_fields!r} is not known: give all, reference, value "
                "or library."
            )
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"❌ The pattern {pattern!r} is not a regular expression: {e}"
        kicad = await get_kicad_version()
        if kicad is None:
            return _NO_VERSION
        placed, problems = await asyncio.to_thread(placed_components, path, kicad)

        def texts(comp: SchematicComponent) -> list[str]:
            values = {"reference": comp.reference, "value": comp.value, "library": comp.library_id}
            return [values[name] for name in fields]

        components = [
            p.component for p in placed if any(regex.search(t) for t in texts(p.component))
        ]

        if not components:
            return "\n\n".join(
                [f"No components found matching pattern: {pattern}", *_problem_lines(problems)]
            )

        # Format output
        lines = [
            f"# Search Results: '{pattern}'",
            f"Found {len(components)} matching component(s)",
            "",
            "| Reference | Value | Library |",
            "|-----------|-------|---------|",
        ]

        for comp in components[:50]:  # Limit to 50 results
            library = comp.library_id.split(":")[-1] if ":" in comp.library_id else comp.library_id
            lines.append(f"| {comp.reference} | {comp.value} | {library} |")

        if len(components) > 50:
            lines.append(f"\n... and {len(components) - 50} more")

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing schematic")


@mcp.tool()
@with_current_schematics("file_path")
@with_field_rules_note
async def list_schematic_nets(
    file_path: str,
    filter_power: bool = False,
) -> str:
    """List all nets in a KiCad schematic.

    Args:
        file_path: Path to .kicad_sch file
        filter_power: If True, only show power nets (VCC, GND, etc.)

    Returns:
        Formatted list of nets
    """
    try:
        parser = SchematicParser(file_path)
        nets = parser.get_nets()

        # Filter for power nets if requested
        if filter_power:
            power_keywords = ["gnd", "vcc", "vdd", "vss", "+", "-"]
            nets = [n for n in nets if any(kw in n.name.lower() for kw in power_keywords)]

        # A file that is not the root schematic: the nets with no label need the
        # full hierarchy. The root is the schematic that the netlist tools use.
        sch_path = Path(file_path)
        subsheet_note = ""
        try:
            project = await asyncio.to_thread(schematic_project_file, sch_path)
            # The schematic of the project name: the netlist tools give it to kicad-cli
            # (cli_root). The note needs no kicad-cli.
            root_sch = project.with_suffix(".kicad_sch")
            if root_sch.resolve() == sch_path.resolve():
                root_sch = None
        except AmbiguousRootError as e:
            subsheet_note = (
                f"**Note:** {e}. The nets with no label need the full hierarchy: they are "
                "not here. Use `generate_netlist` on the root schematic of the project."
            )
        else:
            if root_sch:
                subsheet_note = (
                    f"**Note:** `{sch_path.name}` is not the root schematic of its project. "
                    "The nets with no label need the full hierarchy: they are not here. Use "
                    f"`generate_netlist` on `{root_sch.name}` for all nets."
                )

        if not nets:
            if subsheet_note:
                return f"No nets found.\n\n{subsheet_note}"
            return "No nets found."

        # Format output
        lines = [
            f"# Nets in {file_path}",
            f"Total: {len(nets)} net(s)",
            "",
            "| Net Name | Type | Code |",
            "|----------|------|------|",
        ]

        for net in sorted(nets, key=lambda n: n.name):
            lines.append(f"| {net.name} | {net.type} | {net.code} |")

        if subsheet_note:
            lines.append("")
            lines.append(subsheet_note)

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing schematic")


def _sheet_symbol_lines(screen: SchematicFile) -> list[str]:
    """Return a line for each sheet symbol of a screen: its name, its file, and the
    reason of an empty sheet."""
    lines = []
    for sheet in screen.sheets:
        name = sheet.name if sheet.name.strip() else f"(no name: {sheet.name!r})"
        file = sheet.raw_file or "(no file name)"
        if sheet.file is not None and ("${" in sheet.raw_file or sheet.raw_file.startswith("~")):
            # After the expansion of the text variables and of "~" (sheet_file_names).
            file += f" = {sheet.file}"
        if sheet.empty:
            # A new, empty screen (sheet_list.EMPTY_REASONS).
            file += f" ({EMPTY_REASONS[sheet.empty]}: an empty sheet in KiCad)"
        lines.append(f"- {name}: {file}")
    return lines


@mcp.tool()
@with_current_schematics("file_path")
@with_field_rules_note
async def get_schematic_info(file_path: str) -> str:
    """Get general information about a schematic file.

    The components and the sheet symbols are the ones that KiCad loads for the
    project of the file (the references of each sheet path). The tool needs
    kicad-cli (the rules of the KiCad version).

    Args:
        file_path: Path to .kicad_sch file

    Returns:
        The title block, the number of components (by the prefix of the reference),
        nets and sheet symbols, and the list of the sheet symbols with their files.
        A sheet symbol that KiCad shows as an empty sheet has the reason (no file
        name, a file that is not there, a file that holds its own ancestor). A file
        that the hierarchy of the project does not hold gets a warning.
    """
    try:
        path = Path(file_path)
        if not path.is_file():
            return f"❌ File not found: {file_path}"
        kicad = await get_kicad_version()
        if kicad is None:
            return _NO_VERSION
        def read():
            parser = SchematicParser(file_path, kicad)
            placed, screens, problems = placed_components_and_files(path, kicad)
            # The screens of the file, as KiCad loads them (sheet_list): one for each
            # name of the file in the sheets. None: the hierarchy does not hold it.
            target = path.resolve()
            mine = [s for s in screens.values() if s.file.resolve() == target]
            return parser.get_title_block(), parser.get_nets(), placed, mine, problems

        title_block, nets, placed, mine, problems = await asyncio.to_thread(read)
        screen = mine[0] if mine else None
        # One component for each reference (the units of a package are one
        # component), in each sheet path of the file. A symbol that is not annotated
        # ("R?") is one component.
        all_references = [p.component.reference for p in placed]
        annotated = {r for r in all_references if not r.endswith("?")}
        unannotated = [r for r in all_references if r.endswith("?")]
        references = [*sorted(annotated), *unannotated]
        paths = {p.sheet for p in placed}

        # Count components by type: the prefix of KiCad (UTIL::GetRefDesPrefix).
        component_counts = {}
        for reference in references:
            prefix = ref_des_prefix(reference)
            component_counts[prefix] = component_counts.get(prefix, 0) + 1

        # Format output
        lines = [
            f"# Schematic Information: {file_path}",
            "",
            "## Project Information",
        ]

        if title_block.get("title"):
            lines.append(f"**Title:** {title_block['title']}")
        if title_block.get("company"):
            lines.append(f"**Company:** {title_block['company']}")
        if title_block.get("date"):
            lines.append(f"**Date:** {title_block['date']}")
        if title_block.get("rev"):
            lines.append(f"**Revision:** {title_block['rev']}")

        lines.extend(
            [
                "",
                "## Statistics",
                f"**Total Components:** {len(references)}"
                + (f" (in {len(paths)} sheet paths of the file)" if len(paths) > 1 else ""),
                f"**Total Nets:** {len(nets)}",
                f"**Sheet symbols:** {len(screen.sheets)}"
                if screen is not None
                else "**Sheet symbols:** not known (see the warning)",
                "",
                "## Components by Type",
            ]
        )

        for prefix in sorted(component_counts.keys()):
            lines.append(f"- {prefix}: {component_counts[prefix]}")

        # Each screen of the file has its own sheet items: the first sheet path of a
        # sheet decides its screen, thus the lists of two screens can be different.
        with_sheets = [s for s in mine if s.sheets]
        lists = [_sheet_symbol_lines(s) for s in with_sheets]
        if lists and all(listing == lists[0] for listing in lists):
            lines.extend(["", "## Sheet symbols", *lists[0]])
        else:
            for s, listing in zip(with_sheets, lists, strict=True):
                lines.extend(["", f"## Sheet symbols of the screen {s.key}", *listing])
        if len(mine) > 1:
            names = ", ".join(s.key for s in mine)
            problems = [
                f"KiCad loads this file under {len(mine)} names ({names}): each name is a "
                "screen of its own, with its own copy of the items of the file.",
                *problems,
            ]
        if screen is None:
            problems = [
                "The hierarchy of the project does not hold this file: KiCad does not "
                "load it, thus it has no components and no sheets in the project.",
                *problems,
            ]
        lines += ["", *_problem_lines(problems)] if problems else []

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error parsing schematic")
