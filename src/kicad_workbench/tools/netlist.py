"""Netlist generation and analysis tools for KiCad Workbench."""

import asyncio
import hashlib
import tempfile
from pathlib import Path

from ..parsers.netlist_parser import NetlistParser
from ..server import mcp
from ..utils.kicad_cli import export_with_kicad_cli
from ..utils.sch_hierarchy import AmbiguousRootError, find_root_schematic
from ..utils.tool_errors import error_answer
from ..utils.user_files import write_atomic

# The time of kicad-cli for the netlist (a large project needs some seconds).
NETLIST_TIMEOUT = 120.0


class NetlistError(ValueError):
    """The server cannot make the netlist of a schematic. The message tells why."""


def netlist_cache_path(sch_path: Path) -> Path:
    """Deterministic per-schematic netlist path in the temp directory.

    Keying only on the file stem made two different projects that both have
    e.g. a ``main.kicad_sch`` overwrite each other's exported netlist, so a
    short digest of the resolved schematic path is mixed into the name.
    """
    digest = hashlib.sha256(str(sch_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return Path(tempfile.gettempdir()) / f"{sch_path.stem}_{digest}.xml"


def export_netlist(sch_path: Path) -> tuple[bytes, Path | None]:
    """Return the XML netlist of a schematic as kicad-cli writes it.

    A sub-sheet does not have the hierarchy that gives the names of the nets with
    no label. Thus, for a sub-sheet, kicad-cli writes the netlist of the root
    schematic. kicad-cli writes into its own temporary folder
    (export_with_kicad_cli). The function blocks: run it off the event loop.

    Args:
        sch_path: The schematic file.

    Returns:
        The netlist, and the root schematic for a sub-sheet (None for a root
        schematic).

    Raises:
        AmbiguousRootError: More than one root schematic holds the sheet.
        NetlistError: kicad-cli is not there or cannot run, did not stop in
            time, or did not write the netlist.
    """
    root = find_root_schematic(sch_path)
    source = root or sch_path
    try:
        export = export_with_kicad_cli(
            ["sch", "export", "netlist", "--format", "kicadxml"],
            source,
            "netlist.xml",
            NETLIST_TIMEOUT,
        )
    except (FileNotFoundError, RuntimeError) as e:  # not there, cannot run, no file
        raise NetlistError(f"The server cannot make the netlist of {source}: {e}") from e
    return export.data, root


def _write_netlist_sync(sch_path: Path) -> tuple[Path, Path | None]:
    data, root = export_netlist(sch_path)
    netlist_path = netlist_cache_path(root or sch_path)
    # In one step: an other tool call can read the file at the same time.
    write_atomic(netlist_path, data)
    return netlist_path, root


async def write_netlist(sch_path: Path) -> tuple[Path, Path | None]:
    """Write the netlist of a schematic (export_netlist) into netlist_cache_path().

    The function replaces the file of the cache in one step.

    Args:
        sch_path: The schematic file.

    Returns:
        The netlist file, and the root schematic for a sub-sheet (None for a
        root schematic).

    Raises:
        AmbiguousRootError: More than one root schematic holds the sheet.
        NetlistError: kicad-cli is not there or cannot run, did not stop in
            time, or did not write the netlist.
        OSError: The file of the cache cannot be written.
    """
    return await asyncio.to_thread(_write_netlist_sync, sch_path)


@mcp.tool()
async def generate_netlist(
    schematic_path: str,
) -> str:
    """Generate KiCad netlist from schematic file.

    Args:
        schematic_path: Path to .kicad_sch file

    Returns:
        Path to generated .xml netlist file
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ Schematic file not found: {schematic_path}"
        try:
            netlist_path, root_sch = await write_netlist(sch_path)
        except AmbiguousRootError as e:
            return f"❌ {e}. Generate the netlist of the root schematic that you want."
        subsheet_note = ""
        if root_sch:
            subsheet_note = (
                f"\n\n⚠️ **Note:** `{sch_path.name}` is a hierarchical sub-sheet. "
                f"Switched to root schematic `{root_sch.name}` for complete netlist "
                f"(unnamed wire-only nets require full hierarchy context to resolve)."
            )
        return f"""✅ Netlist generated successfully

**Output:** {netlist_path}

You can now use netlist-based tools:
- `trace_netlist_connection()` - Trace component connections via netlist
- `get_netlist_nets()` - List all nets
- `get_netlist_components()` - List all components with their nets
{subsheet_note}"""

    except Exception as e:
        return error_answer(e, "Error generating netlist")


@mcp.tool()
async def trace_netlist_connection(
    netlist_path: str,
    reference: str,
    pin_number: str = "",
) -> str:
    """Trace component connections using netlist file (most accurate).

    Args:
        netlist_path: Path to .xml netlist file
        reference: Component reference (e.g., 'R16')
        pin_number: Optional pin number (e.g., '1'). If empty, trace all pins.

    Returns:
        Detailed connection information from netlist
    """
    try:
        parser = NetlistParser(netlist_path)

        if pin_number:
            result = parser.trace_connection(reference, pin_number)

            if "error" in result:
                return f"❌ {result['error']}"

            lines = [
                f"## Netlist Connection Trace: {reference}",
                "",
                f"**Pin:** {result['pin']}",
                f"**Net:** {result['net']}",
                "",
                "### Connected Components:",
                "",
            ]

            if result['connected_to']:
                lines.append("| Reference | Pin |")
                lines.append("|-----------|-----|")
                for ref, pin in result['connected_to']:
                    lines.append(f"| {ref} | {pin} |")
            else:
                lines.append("No other components on this net.")

            return "\n".join(lines)

        else:
            result = parser.trace_connection(reference)

            if "error" in result:
                return f"❌ {result['error']}"

            lines = [
                f"## Netlist Connection Trace: {reference}",
                "",
                f"**Total Nets:** {len(result['nets'])}",
                "",
            ]

            for net_name, net_info in result['nets'].items():
                lines.append(f"### Net: {net_name}")
                lines.append(f"**Pin:** {net_info['pin']}")
                lines.append("")

                if net_info['connected_to']:
                    lines.append("| Reference | Pin |")
                    lines.append("|-----------|-----|")
                    for ref, pin in net_info['connected_to']:
                        lines.append(f"| {ref} | {pin} |")
                else:
                    lines.append("No other components on this net.")

                lines.append("")

            return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error tracing netlist")


@mcp.tool()
async def get_netlist_nets(
    netlist_path: str,
    filter_pattern: str = "",
) -> str:
    """Get all nets from netlist file.

    Args:
        netlist_path: Path to .xml netlist file
        filter_pattern: Optional regex pattern to filter net names

    Returns:
        List of all nets with their connections
    """
    try:
        parser = NetlistParser(netlist_path)
        nets = parser.get_nets()

        lines = [
            f"## Nets from Netlist: {netlist_path}",
            "",
            f"**Total Nets:** {len(nets)}",
            "",
        ]

        for net_name, net in sorted(nets.items()):
            if filter_pattern:
                import re
                if not re.search(filter_pattern, net_name, re.IGNORECASE):
                    continue

            lines.append(f"### {net_name}")
            lines.append(f"**Code:** {net.code}")
            lines.append(f"**Connections:** {len(net.pins)} pins")

            if net.pins:
                lines.append("")
                lines.append("| Reference | Pin |")
                lines.append("|-----------|-----|")
                for ref, pin in net.pins[:10]:  # Show first 10
                    lines.append(f"| {ref} | {pin} |")
                if len(net.pins) > 10:
                    lines.append(f"| ... | ({len(net.pins) - 10} more) |")

            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error reading netlist")


@mcp.tool()
async def get_netlist_components(
    netlist_path: str,
    filter_ref: str = "",
) -> str:
    """Get all components from netlist with their network connections.

    Args:
        netlist_path: Path to .xml netlist file
        filter_ref: Optional reference filter (e.g., 'R', 'U', 'C')

    Returns:
        List of components with their net connections
    """
    try:
        parser = NetlistParser(netlist_path)
        components = parser.get_components()

        lines = [
            f"## Components from Netlist: {netlist_path}",
            "",
            f"**Total Components:** {len(components)}",
            "",
        ]

        for ref, comp in sorted(components.items()):
            if filter_ref and not ref.startswith(filter_ref):
                continue

            lines.append(f"### {ref}")
            lines.append(f"**Value:** {comp.value}")
            lines.append(f"**Library:** {comp.library}")
            if comp.footprint:
                lines.append(f"**Footprint:** {comp.footprint}")

            if comp.pins:
                lines.append("")
                lines.append("**Pin Connections:**")
                lines.append("")
                lines.append("| Pin | Net |")
                lines.append("|-----|-----|")
                for pin_num, net_name in sorted(comp.pins.items()):
                    lines.append(f"| {pin_num} | {net_name} |")

            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return error_answer(e, "Error reading netlist")
