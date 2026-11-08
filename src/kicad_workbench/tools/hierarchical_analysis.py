"""Hierarchical schematic analysis tools for KiCad Workbench."""

from pathlib import Path

from ..parsers.kicad_fields import with_field_rules_note
from ..server import mcp
from ..utils.file_versions import with_schematic_versions
from ..utils.tool_errors import error_answer
from .netlist import current_netlist, reference_key


@mcp.tool()
@with_schematic_versions("file_path")
@with_field_rules_note
async def trace_hierarchical_connection(
    file_path: str, reference: str, pin_number: str = ""
) -> str:
    """Trace the connections of a component across the full schematic hierarchy.

    The tool reads the netlist that kicad-cli writes now: it has each component of
    each sheet path, at each level of the hierarchy, with the reference of that
    path (a sheet that the hierarchy uses two times gives two references). The
    netlist has no power symbols and no symbol with no library symbol. The tool
    needs kicad-cli.

    Args:
        file_path: A .kicad_sch file of the project (the root schematic or a
            sub-sheet: the tool uses the netlist of the root schematic).
        reference: Component reference designator (e.g., 'U1', 'R5')
        pin_number: Optional pin number to trace (if empty, trace all pins)

    Returns:
        The sheet of the component, and the nets and the connected pins of each
        pin (a pin number can be on more than one net). A pin number that the
        netlist does not give the component is an error: the netlist has each pin
        of a component, also an unconnected pin.
    """
    try:
        if not Path(file_path).is_file():
            return f"❌ File not found: {file_path}"
        nl = await current_netlist(Path(file_path))
        components = nl.get_components()
        comp = components.get(reference)
        if comp is None:
            refs = [c.reference for c in sorted(components.values(), key=reference_key)]
            listed = ", ".join(refs[:10]) + (" ..." if len(refs) > 10 else "")
            return (
                f"❌ The netlist of kicad-cli has no component '{reference}' (the netlist has "
                "no power symbols and no symbol with no library symbol).\n\n"
                f"Components: {listed or 'none'}"
            )

        sheets = [s for s in nl.get_sheets() if s.tstamps == comp.sheet_tstamps]
        # One sheet for the path: else (KiCad 10.0.7 and later with more than one
        # top-level sheet) the file is not known.
        source = sheets[0].source if len(sheets) == 1 else ""
        notes = []
        if reference in nl.get_duplicate_references():
            notes.append(
                f"⚠️ The annotation is not complete: the netlist has more than one component "
                f"{reference}. The trace is of one of them."
            )
        if len(sheets) > 1:
            notes.append(
                f"⚠️ The netlist has {len(sheets)} sheets with the path of {reference}: the "
                "file of the component is not known."
            )
        lines = [
            f"# Connection Trace for {comp.reference}",
            "",
            f"**Component:** {comp.reference}",
            f"**Value:** {comp.value}",
            f"**Sheet:** {comp.sheet_names or '(not in the netlist)'}"
            + (f" (`{source}`)" if source else ""),
            f"**Schematic:** {file_path}",
            *notes,
            "",
        ]
        if not comp.nodes:
            lines.append("No connections found for this component.")
            return "\n".join(lines)

        # The nets of each pin number (a pin number can be on more than one net): the
        # trace of the pin, or one trace of all pins.
        nets_of: dict[str, list[tuple[str, list]]] = {}
        if pin_number:
            traced = nl.trace_connection(reference, pin_number)
            if "error" in traced:
                return f"❌ {traced['error']}"
            nets_of[pin_number] = [(n["net"], n["connected_to"]) for n in traced["nets"]]
        else:
            for net_name, pins in nl.trace_connection(reference)["nets"].items():
                for pin in pins:
                    nets_of.setdefault(pin["pin"], []).append((net_name, pin["connected_to"]))
        for pn in [pin_number] if pin_number else comp.numbers():
            lines.append(f"## Pin {pn}")
            for net_name, connected in nets_of[pn]:
                lines.append(f"**Net:** {net_name}")
                if connected:
                    lines.append(f"**Connected to ({len(connected)} pins):**")
                    for ref, p in connected[:15]:
                        lines.append(f"  - {ref}:{p}")
                    if len(connected) > 15:
                        lines.append(f"  - ... and {len(connected) - 15} more")
                else:
                    lines.append("**No external connections**")
            lines.append("")

        if note := nl.pins_on_more_nets_note([reference], pin_number or None):
            lines.append(note)
        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error tracing the connection")


@mcp.tool()
@with_schematic_versions("file_path")
@with_field_rules_note
async def analyze_hierarchical_nets(
    file_path: str,
    filter_pattern: str = "",
    show_hierarchy: bool = True
) -> str:
    """Analyze all nets of the full schematic hierarchy.

    The tool reads the netlist that kicad-cli writes now: the nets of all sheets
    at each level of the hierarchy. It groups them as power, interface and signal
    nets (by their names), and gives the sheet paths of the hierarchy.

    Args:
        file_path: A .kicad_sch file of the project (the root schematic or a
            sub-sheet: the tool uses the netlist of the root schematic).
        filter_pattern: Optional regex pattern to filter net names
        show_hierarchy: True (default): list the sheet paths of the hierarchy with
            their files.

    Returns:
        The nets by group, with the number of pins of each net.
    """
    try:
        import re

        if not Path(file_path).is_file():
            return f"❌ File not found: {file_path}"
        nl = await current_netlist(Path(file_path))
        sheets = nl.get_sheets()

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
            f"**Sheet paths:** {len(sheets)}",
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

        if show_hierarchy:
            lines += ["### Sheet Hierarchy", ""]
            lines += [
                f"- {sheet.names}" + (f" (`{sheet.source}`)" if sheet.source else "")
                for sheet in sheets
            ]

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error analyzing the nets")
