"""Intelligent component search tools for KiCad Workbench."""

import asyncio
from pathlib import Path

from ..parsers.kicad_fields import with_field_rules_note
from ..server import mcp
from ..utils.file_versions import with_schematic_versions
from ..utils.tool_errors import error_answer
from .netlist import components_of_file


@mcp.tool()
@with_schematic_versions("file_path")
@with_field_rules_note
async def search_components_by_type(
    file_path: str,
    component_type: str,
    search_in_subsheets: bool = True
) -> str:
    """Find the components of a type, for example OLED, sensor or display (heuristic).

    The tool searches the value, the footprint and the net names of each
    component. It reads the netlist that kicad-cli writes now: each component of
    each sheet path, at each level of the hierarchy, with the reference of that
    path. The netlist has no power symbols and no symbol with no library symbol.
    The tool needs kicad-cli.

    Args:
        file_path: A .kicad_sch file of the project.
        component_type: The type of component, for example "OLED", "sensor",
            "display", "SPI" or "I2C".
        search_in_subsheets: True (default): the components of the file and of all
            sheets below it (for the root schematic: the full project). False: only
            the components that are placed in the file.

    Returns:
        The matching components with their sheet, footprint and nets.
    """
    try:
        path = Path(file_path)
        if not path.is_file():
            return f"❌ File not found: {path}"
        netlist, components, warnings = await asyncio.to_thread(
            components_of_file, path, search_in_subsheets
        )

        # Smart component type detection patterns
        type_patterns = {
            'OLED': ['oled', 'ssd', 'display', 'lcd', 'tft'],
            'sensor': ['sensor', 'temp', 'humidity', 'pressure', 'motion', 'accel', 'gyro'],
            'display': ['display', 'oled', 'lcd', 'tft', 'screen'],
            'SPI': ['spi', 'mosi', 'miso', 'sck', 'cs'],
            'I2C': ['i2c', 'sda', 'scl', 'twi'],
            'UART': ['uart', 'serial', 'tx', 'rx'],
            'power': ['regulator', 'ldo', 'buck', 'boost', 'pmic'],
            'memory': ['flash', 'eeprom', 'ram', 'sdram'],
            'wireless': ['wifi', 'bluetooth', 'nrf', 'esp'],
        }

        # Get search patterns for the requested type. The keys have different
        # cases ('OLED', 'sensor'): the type matches with no regard to case (an
        # upper-case look-up did not find 'sensor', 'display' and the others).
        by_name = {name.lower(): words for name, words in type_patterns.items()}
        patterns = by_name.get(component_type.lower(), [component_type.lower()])

        def matches(component) -> bool:
            texts = [component.value, component.footprint or ""]
            texts += [node.net for node in component.nodes if node.net]
            return any(pattern in text.lower() for text in texts for pattern in patterns)

        matching_components = [c for c in components if matches(c)]
        scope = "the file and the sheets below it" if search_in_subsheets else "the file"
        notes = "".join(f"\n\n{warning}" for warning in warnings)

        if not matching_components:
            return (
                f"# No {component_type} components found\n\n"
                f"Searched for patterns: {', '.join(patterns)}\n\n"
                f"Found 0 matching components in {len(components)} components of {scope}."
                f"{notes}"
            )

        lines = [
            f"# {component_type} Components Found",
            "",
            f"**File:** {file_path}",
            f"**Searched:** {len(components)} components of {scope}",
            f"**Total {component_type} components:** {len(matching_components)}",
            f"**Searched patterns:** {', '.join(patterns)}",
            "",
            "## Matching Components",
            ""
        ]

        for i, component in enumerate(matching_components, 1):
            lines.append(f"### {i}. {component.reference} - {component.value}")
            lines.append(f"**Sheet:** {component.sheet_names or '(not in the netlist)'}")
            if component.footprint:
                lines.append(f"**Footprint:** {component.footprint}")

            component_nets = [(node.number, node.net) for node in component.nodes if node.net]
            if component_nets:
                lines.append(f"**Connections ({len(component_nets)}):**")
                for pin_num, net_name in component_nets[:10]:  # Show first 10 connections
                    lines.append(f"  - Pin {pin_num}: {net_name}")
                if len(component_nets) > 10:
                    lines.append(f"  - ... and {len(component_nets) - 10} more connections")

            lines.append("")

        if note := netlist.pins_on_more_nets_note(c.reference for c in matching_components):
            notes += f"\n\n{note}"
        return "\n".join(lines) + notes

    except Exception as e:
        return error_answer(e, "Error searching components")
