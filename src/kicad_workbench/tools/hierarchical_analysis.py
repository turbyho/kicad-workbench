"""Hierarchical schematic analysis tools for KiCad Workbench."""

from pathlib import Path

from ..parsers.kicad_fields import with_field_rules_note
from ..parsers.netlist_parser import NetlistParser
from ..parsers.schematic_parser import SchematicParser
from ..server import mcp
from ..utils.tool_errors import error_answer


def _find_component_location(file_path: str, reference: str, sheets: list[dict]) -> tuple[object | None, str]:
    """Find a component across the main sheet and sub-sheets.

    Returns (component_or_None, sheet_name).
    """
    parser = SchematicParser(file_path)
    for component in parser.get_components():
        if component.reference == reference:
            return component, "Main Schematic"

    project_dir = Path(file_path).parent
    for sheet in sheets:
        sheet_path = project_dir / sheet.get("file", "")
        if sheet_path.exists():
            # A sheet that the server cannot read gives an error: the search
            # would not be complete.
            for component in SchematicParser(str(sheet_path)).get_components():
                if component.reference == reference:
                    return component, sheet.get("name", sheet.get("file", ""))
    return None, ""


@mcp.tool()
@with_field_rules_note
async def trace_hierarchical_connection(
    file_path: str,
    reference: str,
    pin_number: str = ""
) -> str:
    """Trace component connections across hierarchical schematics.

    This tool traces connections through the entire schematic hierarchy,
    including sub-sheets, to show complete signal paths.

    Args:
        file_path: Path to main .kicad_sch file
        reference: Component reference designator (e.g., 'U1', 'R5')
        pin_number: Optional pin number to trace (if empty, trace all pins)

    Returns:
        Complete connection trace through hierarchy
    """
    try:
        from .pin_analysis import _ensure_netlist

        parser = SchematicParser(file_path)
        sheets = parser.get_sheets()

        # Search the main sheet AND sub-sheets (previously only the root was
        # searched, so any component placed in a sub-sheet was "Not Found").
        target_component, location = _find_component_location(file_path, reference, sheets)

        if not target_component:
            refs = [c.reference for c in parser.get_components()][:10]
            return (
                f"❌ Component '{reference}' not found in schematic hierarchy.\n\n"
                f"Available components: {', '.join(refs)}..."
            )

        lines = [
            f"# Connection Trace for {target_component.reference}",
            "",
            f"**Component:** {target_component.reference}",
            f"**Value:** {target_component.value}",
            f"**Location:** {location}",
            f"**File:** {file_path}",
            "",
        ]

        # The authoritative pin<->net map lives in the netlist. SchematicParser
        # nets carry no pin list (SchematicNet.pins is always empty), so tracing
        # via them always reported "no connections".
        netlist_path = await _ensure_netlist(Path(file_path))

        nl = NetlistParser(str(netlist_path))
        comp = nl.get_components().get(reference)
        if not comp or not comp.pins:
            lines.append("No connections found for this component.")
            return "\n".join(lines)

        pins_to_trace = [pin_number] if pin_number else list(comp.pins.keys())
        for pn in pins_to_trace:
            conn = nl.trace_connection(reference, str(pn))
            net_name = conn.get("net") if isinstance(conn, dict) else None
            connected = conn.get("connected_to", []) if isinstance(conn, dict) else []
            lines.append(f"## Pin {pn}")
            lines.append(f"**Net:** {net_name or '(unconnected)'}")
            if connected:
                lines.append(f"**Connected to ({len(connected)} pins):**")
                for ref, p in connected[:15]:
                    lines.append(f"  - {ref}:{p}")
                if len(connected) > 15:
                    lines.append(f"  - ... and {len(connected) - 15} more")
            else:
                lines.append("**No external connections**")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error tracing the connection")


@mcp.tool()
@with_field_rules_note
async def analyze_hierarchical_nets(
    file_path: str,
    filter_pattern: str = "",
    show_hierarchy: bool = True
) -> str:
    """Analyze all nets in hierarchical schematic design.

    This tool analyzes network connections across the entire hierarchy,
    showing how signals flow between main schematic and sub-sheets.

    Args:
        file_path: Path to main .kicad_sch file
        filter_pattern: Optional regex pattern to filter net names
        show_hierarchy: Whether to show hierarchical structure (default: True)

    Returns:
        Complete hierarchical net analysis
    """
    try:
        import re

        from .pin_analysis import _ensure_netlist

        parser = SchematicParser(file_path)
        sheets = parser.get_sheets()

        netlist_path = await _ensure_netlist(Path(file_path))

        nl = NetlistParser(str(netlist_path))

        # docstring promises a regex; honour it (was a plain substring check).
        try:
            flt = re.compile(filter_pattern, re.IGNORECASE) if filter_pattern else None
        except re.error as e:
            return f"❌ The filter pattern {filter_pattern!r} is not a regular expression: {e}"

        # NetlistNet.pins is a list[(ref, pin)] that is actually populated,
        # unlike SchematicNet.pins which is always empty.
        hierarchical_nets = {}
        for name, net in nl.get_nets().items():
            if flt and not flt.search(name):
                continue
            hierarchical_nets[name] = {
                "location": "Netlist (whole board)",
                "pins": net.pins,
            }

        if not hierarchical_nets:
            return f"# No Nets Found\n\nNo nets found matching pattern: {filter_pattern if filter_pattern else 'all'}"

        lines = [
            "# Hierarchical Net Analysis",
            "",
            f"**File:** {file_path}",
            f"**Total nets:** {len(hierarchical_nets)}",
            f"**Filter:** {filter_pattern if filter_pattern else 'none'}",
            f"**Hierarchical sheets:** {len(sheets)}",
            "",
            "## Network Summary",
            "",
        ]

        power_nets, signal_nets, interface_nets = [], [], []
        for net_name, net_info in hierarchical_nets.items():
            up = net_name.upper()
            if any(p in up for p in ["VDD", "VSS", "GND", "VCC", "VBAT", "VPP", "+3V", "+5V"]):
                power_nets.append((net_name, net_info))
            elif any(i in up for i in ["I2C", "SPI", "UART", "SDA", "SCL", "MOSI", "MISO", "TX", "RX"]):
                interface_nets.append((net_name, net_info))
            else:
                signal_nets.append((net_name, net_info))

        def _emit(title: str, group: list, limit: int) -> None:
            if not group:
                return
            lines.append(f"### {title}")
            for net_name, net_info in sorted(group)[:limit]:
                lines.append(f"**{net_name}** ({net_info['location']})")
                lines.append(f"  Connections: {len(net_info['pins'])} pins")
                lines.append("")
            if len(group) > limit:
                lines.append(f"*... and {len(group) - limit} more*")
            lines.append("")

        _emit("Power Nets", power_nets, 20)
        _emit("Interface Nets", interface_nets, 30)
        _emit(f"Signal Nets (showing first 20 of {len(signal_nets)})", signal_nets, 20)

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error analyzing the nets")
