"""The pin conflict tool of KiCad Workbench: the nets with more than one driver pin,
the nets with one pin and the pins with no connection, from the netlist of kicad-cli.
"""

import collections
from pathlib import Path

from ..server import mcp
from ..utils.file_versions import with_schematic_versions
from ..utils.tool_errors import error_answer
from .netlist import current_netlist


@mcp.tool()
@with_schematic_versions("schematic_path")
async def detect_pin_conflicts(
    schematic_path: str,
) -> str:
    """Find the nets with pin conflicts in a schematic.

    The pin types come from the netlist of kicad-cli. A net with more than one
    output or driver pin is an error. A net with one pin is a warning. A pin with no
    connection is information. Run run_erc for the full electrical check of KiCad.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.

    Returns:
        The errors, the warnings and the information, with a count of each.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"
        return _conflict_report(schematic_path, await _pin_conflicts(sch_path))
    except Exception as e:
        return error_answer(e, f"Conflict detection of {schematic_path} failed")


async def _pin_conflicts(sch_path: Path) -> list[dict]:
    """Return the pin conflicts of the netlist of a schematic.

    Each item has the type, the severity ("error": more than one driver on a
    net, "warning": a net with one pin, "info": an unconnected pin), the net
    and the description.

    Raises:
        AmbiguousRootError: More than one root schematic holds the sheet.
        NetlistError: kicad-cli did not write the netlist.
        InputError: The netlist is not valid XML.
    """
    # The netlist has the electrical type of each node as KiCad gives it (with the
    # alternate of the pin).
    netlist_parser = await current_netlist(sch_path)

    output_types = {"output", "power_out", "open_collector", "open_emitter", "tri_state"}

    # Detect conflicts
    conflicts = []

    # Check for multiple output/driver pins on the same net.
    # (Multiple power_in pins on a rail like +3V3 are normal and are NOT
    # reported — only multiple drivers are a conflict.) The type of each node is the
    # type of its pin on this net (a pin number can be on more than one net).
    all_nets = netlist_parser.get_nets()

    for net_name, net in all_nets.items():
        drivers = [node for node in net.nodes if node.type.lower() in output_types]
        # Two driver nodes of one pin number (two library pins with one number, or
        # "[1-2]" and "2" in KiCad 10) get the pin names.
        counts = collections.Counter((node.reference, node.number) for node in drivers)
        output_pins = [
            f"{node.reference}:{node.number}"
            + (f" ({node.name})" if counts[node.reference, node.number] > 1 and node.name else "")
            for node in drivers
        ]

        if len(output_pins) > 1:
            conflicts.append(
                {
                    "type": "multiple_outputs",
                    "severity": "error",
                    "net": net_name,
                    "description": f"Multiple output/driver pins on same net: {', '.join(output_pins)}",
                }
            )

    # Check for single-pin nets (potential unconnected)
    for net_name, net in all_nets.items():
        if len(net.pins) == 1 and not net_name.startswith("unconnected-"):
            ref, pin_num = net.pins[0]
            conflicts.append(
                {
                    "type": "single_pin_net",
                    "severity": "warning",
                    "net": net_name,
                    "description": f"Net '{net_name}' has only one pin: {ref}:{pin_num}",
                }
            )

    # Check for unconnected input pins
    all_components = netlist_parser.get_components()

    for ref, comp in all_components.items():
        for node in comp.nodes:
            if node.net.startswith("unconnected-"):
                conflicts.append(
                    {
                        "type": "unconnected_pin",
                        "severity": "info",
                        "net": node.net,
                        "description": f"{ref} pin {node.number} is unconnected ({node.net})",
                    }
                )
    return conflicts


def _conflict_report(schematic_path: str, conflicts: list[dict]) -> str:
    """Return the report of detect_pin_conflicts for the conflicts of _pin_conflicts."""
    # The title gives the worst severity: an error is a conflict, a warning can be
    # one, an unconnected pin is only information.
    errors = [c for c in conflicts if c["severity"] == "error"]
    warnings = [c for c in conflicts if c["severity"] == "warning"]
    infos = [c for c in conflicts if c["severity"] == "info"]
    icons = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}

    def table(items: list[dict]) -> str:
        text = "| Severity | Type | Location | Description |\n"
        text += "|----------|------|----------|-------------|\n"
        for conflict in items[:50]:  # Limit to first 50
            location = conflict.get("net", conflict.get("component", "Unknown"))
            text += (
                f"| {icons[conflict['severity']]} {conflict['severity']} "
                f"| {conflict['type']} | {location} | {conflict['description']} |\n"
            )
        if len(items) > 50:
            text += f"\n*... and {len(items) - 50} more*\n"
        return text

    if not errors and not warnings:
        unconnected = (
            f"ℹ️ {len(infos)} unconnected pin(s), listed below"
            if infos
            else "✅ No unconnected pins"
        )
        result = f"""✅ **No Pin Conflicts Detected**

**Schematic:** {schematic_path}

The schematic has been analyzed and no pin conflicts were found.

**Checked:**
- ✅ No multiple output/driver pins on the same net
- ✅ No single-pin (potentially unconnected) nets
- {unconnected}
"""
        if infos:
            result += f"\n## Unconnected Pins\n\n{table(infos)}"
        return result

    # Format conflicts
    title = "❌ **Pin Conflicts Detected**" if errors else "⚠️ **Possible Pin Conflicts**"
    result = f"""{title}

**Schematic:** {schematic_path}
- Errors: {len(errors)}
- Warnings: {len(warnings)}
- Unconnected pins (info): {len(infos)}

## Conflicts

{table(errors + warnings + infos)}"""

    result += "\n## Recommendations\n\n"
    result += "1. **Fix all errors** before proceeding to PCB layout\n"
    result += "2. Review warnings - some may be acceptable design choices\n"
    result += "3. Verify pin connections in KiCad Eeschema\n"
    result += "4. Re-run analysis after fixes\n"

    return result
