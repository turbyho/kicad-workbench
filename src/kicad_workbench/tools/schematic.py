"""Schematic analysis tools for KiCad Workbench."""

import asyncio
from functools import cmp_to_key
from pathlib import Path

from ..parsers.kicad_fields import with_field_rules_note
from ..parsers.netlist_parser import NetlistParser
from ..parsers.schematic_parser import SchematicParser, unknown_alternate_note
from ..parsers.sheet_list import str_num_cmp
from ..server import mcp
from ..utils.kicad_cli import export_with_kicad_cli, get_kicad_version
from ..utils.sch_hierarchy import AmbiguousRootError, find_root_schematic


@mcp.tool()
@with_field_rules_note
async def list_schematic_components(
    file_path: str,
    filter_type: str | None = None,
    filter_value: str | None = None,
    filter_dnp: bool | None = None,
) -> str:
    """List all components in a KiCad schematic file.

    Args:
        file_path: Path to .kicad_sch file
        filter_type: Optional filter by component type prefix (e.g., 'R', 'C', 'U', 'IC')
        filter_value: Optional filter by component value (partial match)
        filter_dnp: Optional filter by DNP flag (True=only DNP, False=only non-DNP)

    Returns:
        Formatted list of components with their properties
    """
    try:
        parser = SchematicParser(file_path)
        components = parser.get_components()

        # Apply filters
        if filter_type:
            components = [c for c in components if c.reference.startswith(filter_type.upper())]

        if filter_value:
            components = [c for c in components if filter_value.lower() in c.value.lower()]

        if filter_dnp is not None:
            components = [c for c in components if c.flags.get("dnp", False) == filter_dnp]

        if not components:
            return "No components found matching the specified criteria."

        # Check if any non-default flags exist
        has_dnp = any(c.flags.get("dnp", False) for c in components)
        has_not_in_bom = any(not c.flags.get("in_bom", True) for c in components)

        # Format output
        header = "| Reference | Value | Footprint | Library |"
        separator = "|-----------|-------|-----------|---------|"
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

        for comp in components:
            footprint = comp.footprint or "-"
            library = comp.library_id.split(":")[-1] if ":" in comp.library_id else comp.library_id
            row = f"| {comp.reference} | {comp.value} | {footprint} | {library} |"
            if has_dnp or has_not_in_bom:
                dnp = "Yes" if comp.flags.get("dnp", False) else ""
                in_bom = "No" if not comp.flags.get("in_bom", True) else ""
                row += f" {dnp} | {in_bom} |"
            lines.append(row)

        return "\n".join(lines)

    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error parsing schematic: {e}"


# The time of the netlist of get_symbol_details (a large project needs some seconds).
_NETLIST_TIMEOUT = 120.0


def _netlist_pins(file_path: Path, reference: str) -> tuple[list[dict[str, str]] | None, str]:
    """Return the pins of a reference as the netlist of kicad-cli gives them, and the
    note, or None and the reason when the netlist has no such reference.

    The netlist of the root schematic has the pins of the placed units of the
    reference, with the name and the electrical type that KiCad computes (also the
    alternate of a pin), sorted by StrNumCmp as KiCad sorts the pins.
    """
    try:
        root = find_root_schematic(file_path) or file_path
        export = export_with_kicad_cli(
            ["sch", "export", "netlist", "--format", "kicadxml"], root, "pins.xml", _NETLIST_TIMEOUT
        )
    except (AmbiguousRootError, RuntimeError, FileNotFoundError) as e:
        return None, f"kicad-cli gives no netlist ({e})"
    netlist = NetlistParser.from_bytes(export.data)
    component = netlist.get_components().get(reference)
    if component is None:
        return None, (
            f"the netlist has no {reference} (the Reference field of the symbol is not the "
            "reference of its sheet path)"
        )
    pins = [
        {
            "number": number,
            "name": component.pin_names.get(number, ""),
            "electrical_type": component.pin_types.get(number, ""),
        }
        for number in sorted(component.pins, key=cmp_to_key(str_num_cmp))
    ]
    return pins, netlist.netlist_note()


@mcp.tool()
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
        kicad = await get_kicad_version()

        def find():
            return SchematicParser(file_path, kicad).get_component_by_reference(reference)

        component = await asyncio.to_thread(find)

        if not component:
            return f"Component '{reference}' not found in schematic."

        # Format output
        lines = [
            f"# Symbol Details: {component.reference}",
            "",
            f"**Value:** {component.value}",
            f"**Library:** {component.library_id}",
            f"**Footprint:** {component.footprint or 'Not assigned'}",
            f"**Position:** ({component.position[0]:.2f}, {component.position[1]:.2f})",
        ]

        if component.unit:
            lines.append(f"**Unit:** {component.unit}")

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
            pins = component.pins
            note = f"⚠️ The pins are the pins of the schematic file: {note}."
            if alternates := unknown_alternate_note([component]):
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

        return "\n".join(lines)

    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error parsing schematic: {e}"


@mcp.tool()
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
        parser = SchematicParser(file_path)
        components = parser.search_components(pattern)

        if not components:
            return f"No components found matching pattern: {pattern}"

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

    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error parsing schematic: {e}"


@mcp.tool()
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

        # Sub-sheet detection: unnamed nets can't be resolved without hierarchy
        sch_path = Path(file_path)
        try:
            root_sch = find_root_schematic(sch_path)
            subsheet = f"`{sch_path.name}` is a sub-sheet"
            root_text = f"the root schematic `{root_sch.name}`" if root_sch else ""
        except AmbiguousRootError as e:
            subsheet = str(e)
            root_text = "one of these root schematics"
        subsheet_note = ""
        if root_text:
            subsheet_note = (
                f"**Note:** {subsheet}. "
                f"Unnamed wire-only nets cannot be resolved without the full "
                f"hierarchy context and will not appear here. "
                f"Use `generate_netlist` on {root_text} "
                f"for complete net data."
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

    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error parsing schematic: {e}"


@mcp.tool()
@with_field_rules_note
async def get_schematic_info(file_path: str) -> str:
    """Get general information about a schematic file.

    Args:
        file_path: Path to .kicad_sch file

    Returns:
        Schematic metadata and statistics
    """
    try:
        parser = SchematicParser(file_path)
        title_block = parser.get_title_block()
        components = parser.get_components()
        nets = parser.get_nets()
        sheets = parser.get_sheets()

        # Count components by type
        component_counts = {}
        for comp in components:
            prefix = "".join(c for c in comp.reference if c.isalpha())
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

        lines.extend([
            "",
            "## Statistics",
            f"**Total Components:** {len(components)}",
            f"**Total Nets:** {len(nets)}",
            f"**Hierarchical Sheets:** {len(sheets)}",
            "",
            "## Components by Type",
])

        for prefix in sorted(component_counts.keys()):
            lines.append(f"- {prefix}: {component_counts[prefix]}")

        if sheets:
            lines.extend([
                "",
                "## Hierarchical Sheets",
            ])
            for sheet in sheets:
                lines.append(f"- {sheet['name']}: {sheet['file']}")

        return "\n".join(lines)

    except FileNotFoundError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error parsing schematic: {e}"
